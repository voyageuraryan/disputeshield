"""Buyer proof uploads (Phase 9C): validate, clean and fingerprint a photo/video before it's stored.

Checks, in order (the first four can reject; the rest are flags shown to the merchant, never decisions):
1. type from the bytes (magic numbers / ISO-BMFF brand), never the file name or the declared Content-Type
2. size: images <= PROOF_IMAGE_MAX_BYTES (10 MB, PayPal's per-file evidence limit), videos <= 50 MB
3. images must decode (HEIC/HEIF converted to JPEG); videos must have a readable length <= 30 s
4. sha256: the same file again for the same step is a no-op (retry-safe)
5. flags: same file on another step or in another case of the same buyer; perceptual-hash (dHash)
   near-duplicates; capture time vs the dispute's create_time (EXIF / MP4 header; missing = "unknown")
Stored copy: images re-encoded without GPS/XMP (orientation applied), videos with their metadata boxes
(udta/meta, where phones keep the location) neutralised. Only the cleaned copy is ever written to disk.
"""

from __future__ import annotations

import io
import struct
import threading
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageOps

try:  # HEIC/HEIF (iPhone photos) support for Pillow
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_OK = True
except ImportError:  # pragma: no cover - listed in requirements
    HEIF_OK = False

MAX_PIXELS = 50_000_000          # decompression-bomb guard (a 48 MP phone photo still fits)
# At most two full image decodes at once: a 48 MP photo decodes to ~150-200 MB, and the free Render plan has 512 MB.
_DECODE_SLOTS = threading.BoundedSemaphore(2)
MAX_SIDE = 2560                  # stored images are scaled down to this
EVIDENCE_MAX = 10 * 1024 * 1024  # PayPal: 10 MB per evidence file
NEAR_DUP_BITS = 6                # dHash Hamming distance (of 64) treated as "looks like the same picture"
NO_TZ_SLACK = timedelta(hours=14)  # EXIF time without an offset can be any time zone (UTC-12..UTC+14)

HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"3gp4", b"3gp5",
              b"3gp6", b"3g2a", b"dash", b"mmp4", b"MSNV"}
EXT = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4", "video/quicktime": "mov"}
LABEL = {"image/jpeg": "JPEG", "image/png": "PNG", "image/heic": "HEIC", "video/mp4": "MP4", "video/quicktime": "MOV"}


class ProofRejected(ValueError):
    """Shown to the buyer as-is."""


def sniff(data: bytes) -> str | None:
    """The real type of an upload, from its bytes. None = not something we accept."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        size = struct.unpack(">I", data[:4])[0]
        major = data[8:12]
        compat = {data[i:i + 4] for i in range(16, min(max(size, 16), len(data), 256), 4)}
        if major in HEIF_BRANDS or (major not in MP4_BRANDS and major != b"qt  " and compat & {b"heic", b"heix"}):
            return "image/heic"
        if major == b"qt  ":
            return "video/quicktime"
        if major in MP4_BRANDS or compat & MP4_BRANDS:
            return "video/mp4"
    return None


def kind_of(mime: str) -> str:
    return "video" if mime.startswith("video/") else "image"


# ---- images -------------------------------------------------------------------------------------------

@dataclass
class CleanImage:
    data: bytes
    mime: str
    width: int
    height: int
    phash: str
    taken_at: datetime | None       # aware if the camera recorded a UTC offset, else naive (local, zone unknown)
    had_gps: bool
    removed: list[str] = field(default_factory=list)


def dhash(img: Image.Image) -> str:
    """64-bit difference hash: survives re-encoding, resizing and small edits; differs for other pictures."""
    g = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = g.tobytes()  # one byte per pixel (mode L)
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _exif_time(exif: Image.Exif) -> datetime | None:
    sub = exif.get_ifd(0x8769)  # Exif IFD
    raw = sub.get(0x9003) or sub.get(0x9004) or exif.get(0x0132)  # DateTimeOriginal / Digitized / DateTime
    if not isinstance(raw, str):
        return None
    try:
        t = datetime.strptime(raw.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    off = sub.get(0x9011) or sub.get(0x9012)  # OffsetTimeOriginal / Digitized, e.g. "+05:30"
    if isinstance(off, str) and len(off) >= 6 and off[0] in "+-":
        try:
            h, m = int(off[1:3]), int(off[4:6])
            delta = timedelta(hours=h, minutes=m) * (1 if off[0] == "+" else -1)
            return t.replace(tzinfo=timezone(delta))
        except ValueError:
            pass
    return t


def clean_image(data: bytes, mime: str) -> CleanImage:
    if mime == "image/heic" and not HEIF_OK:
        raise ProofRejected("HEIC photos can't be read on this server. Please upload a JPEG or PNG.")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    too_big = ProofRejected("This photo is too large to process. Please upload a smaller one.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))  # reads the header only; nothing is decoded yet
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise too_big from None
    except Exception:  # noqa: BLE001 - any decoder failure means a broken or disguised file
        raise ProofRejected("We couldn't open this photo. Please take it again or upload another one.") from None
    # Pillow only *warns* between MAX_PIXELS and 2x MAX_PIXELS (a warning is not an exception), so check the
    # size from the header ourselves before the full decode, and also turn the warning into an error.
    if img.width * img.height > MAX_PIXELS:
        raise too_big
    with _DECODE_SLOTS:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                img.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise too_big from None
        except Exception:  # noqa: BLE001
            raise ProofRejected("We couldn't open this photo. Please take it again or upload another one.") from None
    exif = img.getexif()
    gps = exif.get_ifd(0x8825)
    taken = _exif_time(exif)
    removed = ["GPS location"] if gps else []
    if img.info.get("xmp") or img.info.get("XML:com.adobe.xmp"):
        removed.append("XMP metadata")
    img = ImageOps.exif_transpose(img)  # bake the rotation in, so no metadata is needed to show it upright
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    ph = dhash(img)
    keep_png = mime == "image/png"
    # Only the capture time survives in the stored copy (it's what the merchant may need); everything else,
    # including GPS, maker notes and XMP, is dropped by writing a fresh EXIF block.
    out_exif = Image.Exif()
    if taken is not None:
        out_exif.get_ifd(0x8769)[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")
        if taken.tzinfo is not None:
            off = taken.utcoffset() or timedelta(0)
            sign = "-" if off < timedelta(0) else "+"
            mins = abs(int(off.total_seconds())) // 60
            out_exif.get_ifd(0x8769)[0x9011] = f"{sign}{mins // 60:02d}:{mins % 60:02d}"
    for quality in (88, 80, 70, 60):
        buf = io.BytesIO()
        if keep_png:
            img.save(buf, "PNG", optimize=True)
        else:
            rgb = img
            if img.mode not in ("RGB", "L"):
                rgb = Image.new("RGB", img.size, "white")
                rgb.paste(img, mask=img.convert("RGBA").split()[-1])
            rgb.save(buf, "JPEG", quality=quality, optimize=True, exif=out_exif.tobytes())
        if buf.tell() <= EVIDENCE_MAX:
            break
        keep_png = False  # a huge screenshot: fall back to JPEG
    out_mime = "image/png" if keep_png else "image/jpeg"
    return CleanImage(buf.getvalue(), out_mime, img.width, img.height, ph, taken, bool(gps), removed)


# ---- videos (MP4 / QuickTime) -----------------------------------------------------------------------------

MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
METADATA = {b"udta", b"meta"}  # user data / metadata: where phones store location, device and owner names


def _boxes(buf: bytes | bytearray, start: int, end: int):
    """(type, box start, payload start, box end) for each ISO-BMFF box in buf[start:end]."""
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", bytes(buf[i:i + 8]))
        head = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", bytes(buf[i + 8:i + 16]))[0]
            head = 16
        elif size == 0:
            size = end - i
        if size < head or i + size > end:
            return
        yield typ, i, i + head, i + size
        i += size


@dataclass
class VideoInfo:
    duration_s: float | None
    created: datetime | None   # mvhd creation_time (UTC by the spec); None when 0/missing
    removed: list[str] = field(default_factory=list)


def video_info(data: bytes) -> VideoInfo:
    for typ, _, p, e in _boxes(data, 0, len(data)):
        if typ != b"moov":
            continue
        for t2, _, p2, e2 in _boxes(data, p, e):
            if t2 != b"mvhd" or e2 - p2 < 20:
                continue
            version = data[p2]
            if version == 1 and e2 - p2 >= 32:
                created, _, scale, duration = struct.unpack(">QQIQ", data[p2 + 4:p2 + 32])
            else:
                created, _, scale, duration = struct.unpack(">IIII", data[p2 + 4:p2 + 20])
            when = MAC_EPOCH + timedelta(seconds=created) if created else None
            return VideoInfo(duration / scale if scale else None, when)
    return VideoInfo(None, None)


def strip_video_metadata(data: bytes) -> tuple[bytes, list[str]]:
    """Rename every udta/meta box under moov/trak to 'free' (players skip 'free' boxes). Sizes and offsets are
    unchanged, so the video still plays; the location, device names and other metadata are gone."""
    buf = bytearray(data)
    found: set[str] = set()

    def walk(start: int, end: int) -> None:
        for typ, b, p, e in _boxes(buf, start, end):
            if typ in METADATA:
                chunk = bytes(buf[p:e])
                if b"\xa9xyz" in chunk or b"location" in chunk:
                    found.add("GPS location")
                found.add("metadata")
                buf[b + 4:b + 8] = b"free"
            elif typ in CONTAINERS:
                walk(p, e)

    walk(0, len(buf))
    return bytes(buf), sorted(found)


# ---- capture time vs the dispute ---------------------------------------------------------------------------

def taken_after(taken: datetime | None, dispute_created: datetime | None) -> tuple[str, str]:
    """('yes' | 'no' | 'unknown', explanation). A time without a zone could be up to 14 h either way."""
    if taken is None:
        return "unknown", "no capture time in the file"
    if dispute_created is None:
        return "unknown", "the dispute's open time isn't known"
    created = dispute_created if dispute_created.tzinfo else dispute_created.replace(tzinfo=timezone.utc)
    if taken.tzinfo is not None:
        return ("yes", "taken after the dispute was opened") if taken >= created else \
            ("no", "taken BEFORE the dispute was opened")
    as_utc = taken.replace(tzinfo=timezone.utc)
    if as_utc - NO_TZ_SLACK >= created:
        return "yes", "taken after the dispute was opened"
    if as_utc + NO_TZ_SLACK < created:
        return "no", "taken BEFORE the dispute was opened"
    return "unknown", "taken around the time the dispute was opened (the photo has no time zone)"


def parse_time(value) -> datetime | None:  # noqa: ANN001
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---- evidence copy of a large video (server-side compression) -------------------------------------------------

_FFMPEG_SLOTS = None  # asyncio.Semaphore, created lazily inside the running event loop

async def compress_video(src: str, dst: str, duration_s: float, *, timeout_s: float = 180.0) -> bool:
    """Re-encode a video over 10 MB to an H.264 MP4 under PayPal's 10 MB evidence limit, without metadata.
    Needs ffmpeg on the PATH; returns False (and leaves no file) when it's missing or fails."""
    import asyncio
    import os
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or duration_s <= 0:
        return False
    audio_kbps = 64
    video_kbps = max(150, int(EVIDENCE_MAX * 0.85 * 8 / 1000 / duration_s) - audio_kbps)
    args = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-map_metadata", "-1",
            "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", dst]
    global _FFMPEG_SLOTS
    if _FFMPEG_SLOTS is None:
        _FFMPEG_SLOTS = asyncio.Semaphore(1)  # one re-encode at a time; extra uploads queue instead of piling up
    async with _FFMPEG_SLOTS:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            ok = await asyncio.wait_for(proc.wait(), timeout_s) == 0
        except asyncio.TimeoutError:
            proc.kill()
            ok = False
    if ok and os.path.getsize(dst) <= EVIDENCE_MAX:
        return True
    try:
        os.remove(dst)
    except OSError:
        pass
    return False

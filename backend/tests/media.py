"""Small but real media files for the proof-upload tests (built in memory; no ffmpeg needed)."""

import io
import struct
from datetime import datetime, timezone

from PIL import Image

MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def jpeg(color="red", size=(64, 48), *, taken: str | None = None, offset: str | None = None, gps=False,
         orientation: int | None = None, pattern: int = 0) -> bytes:
    img = Image.new("RGB", size, color)
    if pattern:  # a picture with structure, so different patterns get different perceptual hashes
        for x in range(size[0]):
            for y in range(size[1]):
                if (x * pattern // size[0] + y * 3 // size[1]) % 2:
                    img.putpixel((x, y), (20, 20, 20))
    ex = Image.Exif()
    if orientation:
        ex[0x0112] = orientation
    if taken:
        ex.get_ifd(0x8769)[0x9003] = taken
    if offset:
        ex.get_ifd(0x8769)[0x9011] = offset
    if gps:
        ex.get_ifd(0x8825)[1] = "N"
        ex.get_ifd(0x8825)[2] = (51.0, 30.0, 0.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=ex.tobytes())
    return buf.getvalue()


def png(size=(40, 30), color="blue") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def heic(size=(32, 32), color="green") -> bytes:
    import pillow_heif  # noqa: F401  (registers the HEIF writer)

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "HEIF")
    return buf.getvalue()


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def mp4(seconds: float = 5, *, created: datetime | None = None, location=True, brand=b"isom",
        extra: bytes = b"") -> bytes:
    """ftyp + moov(mvhd, udta(©xyz)) + mdat: enough structure for duration, creation time and metadata."""
    scale = 1000
    ctime = int((created - MAC_EPOCH).total_seconds()) if created else 0
    mvhd = box(b"mvhd", struct.pack(">B3xIIII", 0, ctime, ctime, scale, int(seconds * scale)) + b"\x00" * 80)
    udta = box(b"udta", box(b"\xa9xyz", b"\x00\x12\x15\xc7+51.5000-000.1200/")) if location else b""
    moov = box(b"moov", mvhd + udta)
    ftyp = box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand + b"mp41")
    return ftyp + moov + box(b"mdat", b"\x00" * 256 + extra)

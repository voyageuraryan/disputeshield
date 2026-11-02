"""Phase 9C: buyer page API (auth on every route, step buttons, 202 chat, SSE) and proof uploads
(type sniffing, size/length limits, HEIC conversion, EXIF/GPS, duplicates, advisory vision check)."""

import asyncio
import io
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from PIL import Image
from sqlmodel import Session, select

from app.agents import troubleshooter as T
from app.agents.llm import LLMError
from app.agents.web_research import INTERACTIONS_URL
from app.api import troubleshooting as api
from app.config import get_settings
from app.models import AgentStep, Dispute, TroubleshootingSession, TSMessage, TSProof, TSStatus
from app.troubleshooting import access
from app.troubleshooting import proofs as P
from tests import media
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)
from tests.test_troubleshooting import make_case

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    access.buyer_limiter.reset()
    api.chat_limiter.reset()
    api._working.clear()
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    access.buyer_limiter.reset()


def env(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()


def open_session(client, engine, *, pid="PP-TS-1", buyer_email="buyer@example.com", created=NOW - timedelta(days=2),
                 plan=True):
    """An opened link (device bound) with the generic 5-step plan (step 5 asks for a video)."""
    make_case(engine, pid=pid, rels=(), buyer_email=buyer_email)
    with Session(engine) as s:
        d = s.exec(select(Dispute).where(Dispute.paypal_dispute_id == pid)).one()
        d.raw = {**d.raw, "create_time": created.isoformat().replace("+00:00", "Z")}
        s.add(d); s.commit()
    link = client.post(f"/api/disputes/{pid}/troubleshooting/invite", json={"via": "email"}).json()["link"]
    sid, token = int(link.split("/help/")[1].split("?")[0]), link.split("t=")[1]
    first = client.get(f"/api/help/{sid}", params={"t": token}).json()
    if plan:
        with Session(engine) as s:
            asyncio.run(T.build_plan(s, s.get(TroubleshootingSession, sid), "won't turn on", llm_down="test"))
    return sid, {"X-Help-Token": token, "X-Help-Device": first["device_key"]}


def view(client, sid, h):
    r = client.get(f"/api/help/{sid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def upload(client, sid, h, step_id, data, name="p.jpg", ctype="image/jpeg"):
    return client.post(f"/api/help/{sid}/steps/{step_id}/proof", files={"file": (name, data, ctype)}, headers=h)


def actions(engine):
    with Session(engine) as s:
        return [a.action for a in s.exec(select(AgentStep).order_by(AgentStep.id))]


# ---- auth on every buyer route ---------------------------------------------------------------------------

ROUTES = [
    ("get", "", None), ("post", "/messages", {"json": {"body": "hi"}}),
    ("post", "/steps/{step}", {"json": {"status": "DONE_NOT_FIXED"}}), ("post", "/confirm", {"json": {"fixed": False}}),
    ("post", "/handoff", None), ("post", "/steps/{step}/proof", {"files": {"file": ("p.png", media.png(), "image/png")}}),
    ("get", "/proofs/{proof}", None), ("get", "/events", {"params": {"max_seconds": 0.2}}),
]


@pytest.mark.parametrize("method,path,kw", ROUTES, ids=[f"{m} {p or '/'}" for m, p, _ in ROUTES])
def test_every_buyer_route_checks_the_link(client, engine, method, path, kw, monkeypatch):
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][0]["id"]
    proof = upload(client, sid, h, step, media.png()).json()["proof"]["id"]
    url = f"/api/help/{sid}" + path.format(step=step, proof=proof)
    call = lambda headers, **extra: getattr(client, method)(url, headers=headers, **(kw or {}), **extra)  # noqa: E731
    tampered = h["X-Help-Token"][:-1] + ("A" if h["X-Help-Token"][-1] != "A" else "B")
    assert call({}).status_code == 404                                               # no token
    assert call({**h, "X-Help-Token": tampered}).status_code == 404                  # tampered token
    assert call({**h, "Authorization": "Bearer merchant"}).status_code == 403        # merchant credentials
    assert call({"X-Help-Token": h["X-Help-Token"], "X-Help-Device": "other"}).status_code == 403  # other device
    assert getattr(client, method)(f"/api/help/{sid + 1}" + path.format(step=step, proof=proof), headers=h,
                                   **(kw or {})).status_code == 404               # this token on another session
    with Session(engine) as s:
        row = s.get(TroubleshootingSession, sid)
        row.expires_at = NOW - timedelta(minutes=1)
        s.add(row); s.commit()
    assert call(h).status_code == 410                                                # expired


def test_actions_need_a_bound_device(client, engine):
    make_case(engine, rels=())
    link = client.post("/api/disputes/PP-TS-1/troubleshooting/invite", json={"via": "email"}).json()["link"]
    sid, token = int(link.split("/help/")[1].split("?")[0]), link.split("t=")[1]
    for path in ("/events", "/steps/1", "/proofs/1"):
        method = client.get if path != "/steps/1" else client.post
        assert method(f"/api/help/{sid}{path}", headers={"X-Help-Token": token},
                      **({"json": {"status": "CANT_DO"}} if path == "/steps/1" else {})).status_code == 409


# ---- header, 202 chat, step buttons ------------------------------------------------------------------------

def test_view_has_the_header_fields(client, engine):
    sid, h = open_session(client, engine)
    v = view(client, sid, h)
    assert v["store_name"] == "Demo store" and v["paypal_case_id"] == "PP-TS-1" and v["order_ref"]
    assert v["product_name"] == "Smart LED desk lamp" and v["product_image"]  # catalog placeholder image
    assert v["agent_working"] is False and all("id" in m for m in v["messages"])


def test_chat_is_accepted_then_answered_in_background(client, engine, monkeypatch):
    sid, h = open_session(client, engine)

    async def llm(*, system, user, output_model):
        return output_model.model_validate({"message": "Thanks, try step 1.", "language": "en", "step_updates": [],
                                            "ask_for_proof_step_id": 0, "outcome": "NONE", "safety_concern": False,
                                            "safety_reason": ""})
    monkeypatch.setattr("app.agents.llm.structured_call", llm)
    r = client.post(f"/api/help/{sid}/messages", json={"body": "ok what now"}, headers=h)
    assert r.status_code == 202 and r.json() == {"accepted": True, "agent_working": True}
    v = view(client, sid, h)
    assert [m["role"] for m in v["messages"][-2:]] == ["buyer", "agent"] and not v["agent_working"]
    # while a turn is running, a second message is refused (one turn at a time)
    api._working.add(sid)
    assert client.post(f"/api/help/{sid}/messages", json={"body": "again"}, headers=h).status_code == 409


def test_background_crash_still_answers_the_buyer(client, engine, monkeypatch):
    sid, h = open_session(client, engine)

    async def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(T, "agent_reply", boom)
    assert client.post(f"/api/help/{sid}/messages", json={"body": "hello"}, headers=h).status_code == 202
    v = view(client, sid, h)
    assert v["messages"][-1]["body"] == T.TROUBLE_REPLY["en"] and not v["agent_working"]
    assert "The troubleshooting assistant failed on a message" in actions(engine)


def test_intake_llm_down_gives_the_generic_plan(client, engine, monkeypatch):
    sid, h = open_session(client, engine, plan=False)

    async def down(**kw):
        raise LLMError("groq: HTTP 429 | gemini: HTTP 503")
    monkeypatch.setattr("app.agents.llm.structured_call", down)
    client.post(f"/api/help/{sid}/messages", json={"body": "it won't turn on"}, headers=h)
    v = view(client, sid, h)
    assert len(v["steps"]) == 5 and v["plan_generic"] is True
    assert v["messages"][-1]["body"].startswith("I can't look up your exact model right now")
    assert "Using the generic safe plan (not product-specific): the plan writer (LLM) was unavailable" in actions(engine)


def test_step_buttons_reply_without_the_llm(client, engine, monkeypatch):
    sid, h = open_session(client, engine)

    async def never(**kw):
        raise AssertionError("step buttons must not call the LLM")
    monkeypatch.setattr("app.agents.llm.structured_call", never)
    steps = view(client, sid, h)["steps"]
    r = client.post(f"/api/help/{sid}/steps/{steps[0]['id']}", json={"status": "DONE_NOT_FIXED", "note": "no light"},
                    headers=h).json()
    assert r["steps"][0]["status"] == "DONE_NOT_FIXED" and r["steps"][0]["buyer_note"] == "no light"
    assert r["reply"] == "Thanks for trying that. Next, step 2: Charge it for 30 minutes."
    assert r["messages"][-2]["body"] == "Step 1: Done, still not working. no light"
    assert r["messages"][-1]["step_id"] == steps[1]["id"]  # source chips can follow the step
    r = client.post(f"/api/help/{sid}/steps/{steps[1]['id']}", json={"status": "DONE_FIXED"}, headers=h).json()
    assert r["awaiting_fixed_confirm"] and r["status"] == "ACTIVE"  # a tap alone never closes the case
    assert client.post(f"/api/help/{sid}/steps/99999", json={"status": "CANT_DO"}, headers=h).status_code == 404
    assert client.post(f"/api/help/{sid}/steps/{steps[2]['id']}", json={"status": "SKIPPED"},
                       headers=h).status_code == 422
    assert "Buyer: step 1 (Restart it): done, still not working" in actions(engine)


def test_safety_words_in_a_step_note_stop(client, engine):
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][0]["id"]
    r = client.post(f"/api/help/{sid}/steps/{step}", json={"status": "DONE_NOT_FIXED", "note": "now it smells of burning"},
                    headers=h).json()
    assert r["status"] == "SAFETY_STOP" and r["ended"]


def test_buttons_and_upload_reach_defect_confirmed(client, engine):
    sid, h = open_session(client, engine)
    steps = view(client, sid, h)["steps"]
    for st in steps[:4]:
        client.post(f"/api/help/{sid}/steps/{st['id']}", json={"status": "DONE_NOT_FIXED"}, headers=h)
    r = client.post(f"/api/help/{sid}/steps/{steps[4]['id']}", json={"status": "DONE_NOT_FIXED"}, headers=h).json()
    assert r["status"] == "AWAITING_PROOF" and r["reply"].startswith("Thanks. Please add the video for step 5")
    r = upload(client, sid, h, steps[4]["id"], media.mp4(8), "fault.mp4", "video/mp4")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "DEFECT_CONFIRMED" and r.json()["ended"]
    assert any(a.startswith("Defect confirmed") for a in actions(engine))


# ---- upload validation table -------------------------------------------------------------------------------

UPLOADS = [  # name, bytes, declared type, expected status, stored mime
    ("jpeg", media.jpeg(), "image/jpeg", 200, "image/jpeg"),
    ("png", media.png(), "image/png", 200, "image/png"),
    ("heic -> jpeg", media.heic(), "image/heic", 200, "image/jpeg"),
    ("heic sent as octet-stream", media.heic(color="navy"), "application/octet-stream", 200, "image/jpeg"),
    ("mp4", media.mp4(10), "video/mp4", 200, "video/mp4"),
    ("mov", media.mp4(10, brand=b"qt  "), "video/quicktime", 200, "video/quicktime"),
    ("text named .jpg", b"hello, this is not an image", "image/jpeg", 422, None),
    ("pdf", b"%PDF-1.7\n1 0 obj", "application/pdf", 422, None),
    ("gif not accepted", b"GIF89a" + b"\x00" * 40, "image/gif", 422, None),
    ("broken jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 50, "image/jpeg", 422, None),
    ("empty", b"", "image/jpeg", 422, None),
    ("video too long", media.mp4(45), "video/mp4", 422, None),
    ("video with no length", media.mp4(0), "video/mp4", 422, None),
]


@pytest.mark.parametrize("name,data,ctype,status,stored", UPLOADS, ids=[u[0] for u in UPLOADS])
def test_upload_validation(client, engine, name, data, ctype, status, stored):
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][4]["id"]
    r = upload(client, sid, h, step, data, "x.bin", ctype)
    assert r.status_code == status, r.text
    if stored:
        assert r.json()["proof"]["mime"] == stored and r.json()["steps"][4]["proofs"] == 1
        with Session(engine) as s:
            p = s.exec(select(TSProof)).one()
            assert p.file_path.split("artifacts/")[1].startswith(f"ts/{sid}/")
            assert p.checks["size_ok"] and p.original_mime


def test_size_limits_and_request_shape(client, engine, monkeypatch):
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][4]["id"]
    env(monkeypatch, PROOF_IMAGE_MAX_BYTES=500)
    r = upload(client, sid, h, step, media.jpeg(size=(300, 300), pattern=7))
    assert r.status_code == 413 and "photo is too large (max 0 MB)" in r.json()["detail"]
    env(monkeypatch, PROOF_IMAGE_MAX_BYTES=10 * 2**20, PROOF_VIDEO_MAX_BYTES=2000)
    r = upload(client, sid, h, step, media.mp4(5, extra=b"\x00" * 200_000), "v.mp4", "video/mp4")
    assert r.status_code == 413  # refused from Content-Length alone, before the body is read
    env(monkeypatch, PROOF_VIDEO_MAX_BYTES=50 * 2**20)
    url = f"/api/help/{sid}/steps/{step}/proof"
    assert client.post(url, content=media.png(), headers={**h, "Content-Type": "image/png"}).status_code == 415
    assert client.post(url, data={"other": "x"}, files={"x": ("a", b"", "text/plain")}, headers=h).status_code == 422
    assert upload(client, sid, h, 999999, media.png()).status_code == 404
    other_sid, other_h = open_session(client, engine, pid="PP-TS-2", buyer_email="other@example.com")
    other_step = view(client, other_sid, other_h)["steps"][0]["id"]
    assert upload(client, sid, h, other_step, media.png()).status_code == 404  # another session's step


# ---- EXIF / GPS / capture time -------------------------------------------------------------------------------

def stored_image(engine):
    with Session(engine) as s:
        p = s.exec(select(TSProof).order_by(TSProof.id.desc())).first()
        return p, Image.open(p.file_path)


def test_gps_stripped_capture_time_kept_and_rotation_applied(client, engine):
    sid, h = open_session(client, engine, created=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    step = view(client, sid, h)["steps"][4]["id"]
    data = media.jpeg(size=(64, 48), taken="2026:11:01 09:15:00", offset="+05:30", gps=True, orientation=6)
    assert dict(Image.open(io.BytesIO(data)).getexif().get_ifd(0x8825))  # the upload had GPS
    assert upload(client, sid, h, step, data).status_code == 200
    p, img = stored_image(engine)
    ex = img.getexif()
    assert dict(ex.get_ifd(0x8825)) == {} and 0x8825 not in ex          # no GPS in the stored copy
    assert ex.get_ifd(0x8769)[0x9003] == "2026:11:01 09:15:00"          # capture time kept
    assert img.size == (48, 64) and ex.get(0x0112) is None              # rotation baked in
    assert p.checks["metadata_removed"] == ["GPS location"]
    assert p.checks["taken_after_dispute"] == "yes"
    assert p.exif_time.replace(tzinfo=None) == datetime(2026, 11, 1, 3, 45)  # 09:15 at +05:30, stored as UTC
    assert p.checks["capture_time"] == "2026-11-01T09:15:00+05:30"


@pytest.mark.parametrize("taken,offset,expected", [
    ("2026:11:01 09:00:00", "+00:00", "yes"),
    ("2026:10:29 09:00:00", "-07:00", "no"),          # before the dispute
    ("2026:10:20 09:00:00", None, "no"),              # no zone, but days before
    ("2026:11:05 09:00:00", None, "yes"),             # no zone, days after
    ("2026:10:30 15:00:00", None, "unknown"),         # no zone, within a day of the dispute: can't tell
    (None, None, "unknown"),                          # no EXIF at all: unknown, not a failure
])
def test_capture_time_vs_dispute(client, engine, taken, offset, expected):
    sid, h = open_session(client, engine, created=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    step = view(client, sid, h)["steps"][4]["id"]
    assert upload(client, sid, h, step, media.jpeg(taken=taken, offset=offset)).status_code == 200
    p, _ = stored_image(engine)
    assert p.checks["taken_after_dispute"] == expected
    if expected == "no":
        assert any("taken before the dispute" in a for a in actions(engine))


def test_video_location_removed_and_creation_time_read():
    data = media.mp4(12, created=datetime(2026, 11, 1, 8, tzinfo=timezone.utc))
    info = P.video_info(data)
    assert info.duration_s == 12 and info.created == datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    clean, removed = P.strip_video_metadata(data)
    assert len(clean) == len(data) and "GPS location" in removed
    assert b"udta" not in clean and b"free" in clean
    assert P.video_info(clean).duration_s == 12  # still a valid file with the same length
    assert P.taken_after(info.created, datetime(2026, 10, 30, tzinfo=timezone.utc))[0] == "yes"


def test_sniff_ignores_names_and_declared_types():
    assert P.sniff(media.jpeg()) == "image/jpeg" and P.sniff(media.png()) == "image/png"
    assert P.sniff(media.heic()) == "image/heic" and P.sniff(media.mp4()) == "video/mp4"
    assert P.sniff(media.mp4(brand=b"qt  ")) == "video/quicktime"
    assert P.sniff(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert P.sniff(b"\x00\x00\x00\x18ftypavif") is None  # AVIF isn't on the list


# ---- duplicates ------------------------------------------------------------------------------------------------

def test_same_file_same_step_is_a_no_op(client, engine):
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][4]["id"]
    data = media.jpeg(pattern=5)
    first = upload(client, sid, h, step, data).json()
    again = upload(client, sid, h, step, data).json()
    assert again["proof"]["already_uploaded"] and again["proof"]["id"] == first["proof"]["id"]
    with Session(engine) as s:
        assert len(s.exec(select(TSProof)).all()) == 1


def test_duplicates_within_and_across_cases_of_the_same_buyer(client, engine):
    sid, h = open_session(client, engine, buyer_email="Same@Example.com")
    steps = view(client, sid, h)["steps"]
    pic = media.jpeg(size=(120, 90), pattern=5)
    upload(client, sid, h, steps[3]["id"], pic)
    upload(client, sid, h, steps[4]["id"], pic)  # same file on another step
    p, _ = stored_image(engine)
    assert p.checks["duplicate_of_steps"] == [4]
    # the same buyer, another dispute: exact copy and a re-saved (near-identical) copy are both flagged
    sid2, h2 = open_session(client, engine, pid="PP-TS-2", buyer_email="same@example.com")
    s2 = view(client, sid2, h2)["steps"]
    upload(client, sid2, h2, s2[4]["id"], pic)
    p, _ = stored_image(engine)
    assert p.checks["reused_from_other_cases"] == ["PP-TS-1"]
    buf = io.BytesIO()
    Image.open(io.BytesIO(pic)).resize((100, 75)).save(buf, "JPEG", quality=60)
    upload(client, sid2, h2, s2[3]["id"], buf.getvalue())
    p, _ = stored_image(engine)
    assert p.checks["reused_from_other_cases"] == [] and p.checks["near_duplicates"]
    assert {d.get("case") for d in p.checks["near_duplicates"]} >= {"PP-TS-1"}
    # a different buyer's identical file is not linked to this buyer
    sid3, h3 = open_session(client, engine, pid="PP-TS-3", buyer_email="stranger@example.com")
    upload(client, sid3, h3, view(client, sid3, h3)["steps"][4]["id"], pic)
    p, _ = stored_image(engine)
    assert p.checks["reused_from_other_cases"] == [] and p.checks["near_duplicates"] == []


def test_different_pictures_are_not_near_duplicates():
    a = P.clean_image(media.jpeg(size=(120, 90), pattern=5), "image/jpeg").phash
    b = P.clean_image(media.jpeg(size=(120, 90), pattern=2, color="yellow"), "image/jpeg").phash
    assert P.hamming(a, b) > P.NEAR_DUP_BITS


def test_buyer_sees_only_own_files(client, engine):
    sid, h = open_session(client, engine)
    pid = upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.png()).json()["proof"]["id"]
    r = client.get(f"/api/help/{sid}/proofs/{pid}", headers=h)
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    sid2, h2 = open_session(client, engine, pid="PP-TS-2", buyer_email="b2@example.com")
    assert client.get(f"/api/help/{sid2}/proofs/{pid}", headers=h2).status_code == 404
    # merchant view shows the checks
    st = client.get("/api/disputes/PP-TS-1/troubleshooting").json()
    assert st["proofs"][0]["checks"]["detected_type"] == "PNG" and st["proofs"][0]["evidence_ready"]


# ---- advisory vision check -------------------------------------------------------------------------------------

def vision_reply(shows="yes"):
    text = json.dumps({"shows_step": shows, "what_is_visible": "A desk lamp with its light off.", "concerns": ""})
    return {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}]}


def test_vision_off_by_default(client, engine):
    sid, h = open_session(client, engine)
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.jpeg())
    p, _ = stored_image(engine)
    assert "off" in p.vision_verdict["skipped"]


@respx.mock
def test_vision_check_is_advisory_only(client, engine, monkeypatch):
    env(monkeypatch, PROOF_VISION_CHECK="true", VISION_MODEL="gemini-test")
    route = respx.post(INTERACTIONS_URL).mock(return_value=httpx.Response(200, json=vision_reply("no")))
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][4]["id"]
    r = upload(client, sid, h, step, media.jpeg(gps=True, taken="2026:11:01 10:00:00"))
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE"  # "no" changes nothing
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "gemini-test" and body["store"] is False
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["input"][1]["type"] == "image" and body["input"][1]["mime_type"] == "image/jpeg"
    assert route.calls[0].request.headers["x-goog-api-key"] == "test-gemini-key"
    sent = Image.open(io.BytesIO(__import__("base64").b64decode(body["input"][1]["data"])))
    assert dict(sent.getexif().get_ifd(0x8825)) == {}  # Gemini gets the GPS-free copy
    p, _ = stored_image(engine)
    assert p.vision_verdict["shows_step"] == "no" and p.vision_verdict["advisory"] is True
    assert p.vision_verdict["model"] == "gemini-test"
    assert any(a.startswith("Advisory photo check for step 5: shows the step = no") for a in actions(engine))
    with Session(engine) as s:
        assert s.get(TroubleshootingSession, sid).status == TSStatus.ACTIVE


@respx.mock
def test_vision_tries_the_next_model_then_records_failure(client, engine, monkeypatch):
    env(monkeypatch, PROOF_VISION_CHECK="true", VISION_MODEL="busy-model,ok-model")
    route = respx.post(INTERACTIONS_URL).mock(side_effect=[httpx.Response(503, json={}),
                                                           httpx.Response(200, json=vision_reply("unclear"))])
    sid, h = open_session(client, engine)
    step = view(client, sid, h)["steps"][4]["id"]
    upload(client, sid, h, step, media.jpeg())
    p, _ = stored_image(engine)
    assert p.vision_verdict["model"] == "ok-model" and p.vision_verdict["shows_step"] == "unclear"
    assert [json.loads(c.request.content)["model"] for c in route.calls] == ["busy-model", "ok-model"]
    route.side_effect = None
    route.return_value = httpx.Response(503, json={})
    assert upload(client, sid, h, step, media.png()).status_code == 200  # never fails the upload
    p, _ = stored_image(engine)
    assert "photo check unavailable" in p.vision_verdict["error"] and "busy-model" in p.vision_verdict["error"]


def test_videos_are_not_sent_to_vision(client, engine, monkeypatch):
    env(monkeypatch, PROOF_VISION_CHECK="true")
    sid, h = open_session(client, engine)
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.mp4(5), "v.mp4", "video/mp4")
    with Session(engine) as s:
        assert "videos" in s.exec(select(TSProof)).one().vision_verdict["skipped"]


# ---- big videos: evidence copy -----------------------------------------------------------------------------------

def test_big_video_gets_a_compressed_evidence_copy(client, engine, monkeypatch):
    calls = []

    async def fake_compress(src, dst, duration):
        calls.append((src, dst, duration))
        open(dst, "wb").write(b"small")
        return True
    monkeypatch.setattr(P, "compress_video", fake_compress)
    monkeypatch.setattr(P, "EVIDENCE_MAX", 1000)
    sid, h = open_session(client, engine)
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.mp4(20, extra=b"\x00" * 5000), "v.mp4",
           "video/mp4")
    with Session(engine) as s:
        p = s.exec(select(TSProof)).one()
        assert calls and calls[0][2] == 20 and p.evidence_path.endswith(".evidence.mp4")
        assert p.checks["evidence_ready"] and p.checks["evidence_copy"] == "compressed to under 10 MB"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_compress_video_with_ffmpeg(tmp_path):
    src, dst = tmp_path / "in.mp4", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-metadata", "location=+51.5000-000.1200/", str(src)], check=True)
    assert asyncio.run(P.compress_video(str(src), str(dst), 2.0))
    out = dst.read_bytes()
    assert 0 < len(out) <= P.EVIDENCE_MAX and b"+51.5000" not in out


# ---- SSE -----------------------------------------------------------------------------------------------------------

def events(text):
    out = []
    for block in text.split("\n\n"):
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln and not ln.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines.get("data", "{}")), lines.get("id")))
    return out


def test_sse_backlog_state_and_resume(client, engine):
    sid, h = open_session(client, engine)
    v = view(client, sid, h)
    last = v["messages"][-1]["id"]
    client.post(f"/api/help/{sid}/steps/{v['steps'][0]['id']}", json={"status": "DONE_NOT_FIXED"}, headers=h)
    with client.stream("GET", f"/api/help/{sid}/events", params={"after": last, "max_seconds": 0.3}, headers=h) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        got = events("".join(r.iter_text()))
    kinds = [k for k, _, _ in got]
    assert kinds[:2] == ["message", "message"] and "state" in kinds and "ready" in kinds and kinds[-1] == "bye"
    msgs = [d for k, d, _ in got if k == "message"]
    assert msgs[0]["role"] == "buyer" and msgs[1]["body"].startswith("Thanks for trying that. Next, step 2")
    assert all(d["role"] != "system" for d in msgs)  # merchant-only notes never reach the buyer
    state = next(d for k, d, _ in got if k == "state")
    assert state["steps"][0]["status"] == "DONE_NOT_FIXED" and state["agent_working"] is False
    # Last-Event-ID resumes after what the client already has: no repeats
    with client.stream("GET", f"/api/help/{sid}/events", params={"max_seconds": 0.3},
                       headers={**h, "Last-Event-ID": str(msgs[-1]["id"])}) as r:
        assert [k for k, _, _ in events("".join(r.iter_text()))].count("message") == 0


def test_sse_pushes_new_messages_live(client, engine):
    sid, h = open_session(client, engine)
    last = view(client, sid, h)["messages"][-1]["id"]

    def later():
        time.sleep(0.4)
        with Session(engine) as s:
            s.add(TSMessage(session_id=sid, role="merchant", body="Hi, it's the seller here."))
            s.commit()
    threading.Thread(target=later).start()
    started = time.monotonic()
    with client.stream("GET", f"/api/help/{sid}/events", params={"after": last, "max_seconds": 2.5}, headers=h) as r:
        for line in r.iter_lines():
            if "Hi, it's the seller here." in line:
                break
    assert time.monotonic() - started < 2.4  # woken by the commit, not by the end of the stream
    assert '"role":"merchant"' in line


def test_sse_ends_when_the_session_ends(client, engine):
    sid, h = open_session(client, engine)
    client.post(f"/api/help/{sid}/handoff", headers=h)
    # the link still opens (to show the outcome) and the stream says bye at once
    with client.stream("GET", f"/api/help/{sid}/events", params={"max_seconds": 5}, headers=h) as r:
        got = events("".join(r.iter_text()))
    state = next(d for k, d, _ in got if k == "state")
    assert state["ended"] and state["status"] == "HANDED_TO_MERCHANT" and got[-1][0] == "bye"

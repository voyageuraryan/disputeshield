"""Troubleshooting portal API.

merchant_router (mounted behind require_merchant):
  GET  /api/disputes/{id}/troubleshooting                      eligibility, session, steps, transcript, sources
  POST /api/disputes/{id}/troubleshooting/invite               create the session and send the buyer the link
  GET  /api/disputes/{id}/troubleshooting/proofs/{proof_id}    a photo/video the buyer uploaded
buyer_router (public, but every request needs the session's link token; merchant credentials are refused):
  GET  /api/help/{sid}                       the buyer's view; the first open binds the link to that browser
  POST /api/help/{sid}/messages              chat turn: 202, the agent answers in the background (see /events)
  POST /api/help/{sid}/steps/{step}          step card button {status, note} (fixed-text reply, no LLM)
  POST /api/help/{sid}/confirm               "Yes, it works now" / "No, still broken" (only way to RESOLVED_FIXED)
  POST /api/help/{sid}/handoff               "Talk to the seller"
  POST /api/help/{sid}/steps/{step}/proof    multipart photo/video (field "file"); checks in troubleshooting/proofs.py
  GET  /api/help/{sid}/proofs/{proof}        the buyer's own upload (thumbnails); never another session's
  GET  /api/help/{sid}/events                SSE: new messages (event: message) and state changes (event: state)
Buyer requests carry the link token (?t= or X-Help-Token) and, after the first open, the device key
(X-Help-Device): a link opened on one browser can't be used from another (G31: the merchant, who can read the
link in the PayPal thread, can't act as the buyer once the buyer has opened it).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agents import troubleshooter as agent
from app.agents.steps import log_step
from app.catalog import PRODUCTS
from app.events_bus import help_key, notify, wait
from app.api.disputes import _find
from app.config import get_settings
from app.db import get_session
from app.models import (TS_OPEN_STATUSES, Dispute, Order, TroubleshootingSession, TSMessage, TSProof, TSSource,
                        TSStatus)
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.troubleshooting.access import RateLimiter, is_expired, new_token, rate_limit, token_matches
from app.troubleshooting.eligibility import dispute_eligibility
from app.troubleshooting import proofs as P
from app.troubleshooting import vision
from app.troubleshooting.service import AGENT, InviteRefused, can_message, invite, session_for

log = logging.getLogger(__name__)

merchant_router = APIRouter(tags=["troubleshooting"])
buyer_router = APIRouter(prefix="/api/help", tags=["troubleshooting-buyer"])

MAX_BUYER_MESSAGES = 60      # per session: bounds LLM/search spend
MESSAGE_MAX_CHARS = 1500
chat_limiter = RateLimiter(capacity=6, refill_per_s=0.1)  # chat turns are LLM calls: stricter than page loads
_working: set[int] = set()   # sessions whose agent turn is running (typing indicator; one turn at a time)


def expire_if_due(session: Session, row: TroubleshootingSession) -> bool:
    """Mark an open session EXPIRED once its link is past expiry (whoever looks first: buyer or merchant)."""
    if row.status in TS_OPEN_STATUSES and is_expired(row.expires_at):
        row.status = TSStatus.EXPIRED
        row.ended_reason = "link expired"
        session.add(row)
        session.commit()
        log_step(session, row.dispute_id, AGENT, "The buyer's troubleshooting link expired", {"session_id": row.id})
    return row.status == TSStatus.EXPIRED


# ---- shared views ---------------------------------------------------------------------------------

class StepView(BaseModel):
    id: int
    ordinal: int
    title: str
    instruction: str
    why: str | None
    safety_level: str
    requires_proof: bool
    proof_kind: str
    status: str
    source_ids: list[int]
    proofs: int
    proof_files: list[dict] = []   # [{id, kind: image|video}] the buyer's uploads for this step (thumbnails)
    buyer_note: str | None = None


class SourceView(BaseModel):
    id: int
    title: str | None
    url: str


class MessageView(BaseModel):
    id: int
    role: str
    body: str
    step_id: int | None
    created_at: datetime


def _steps(db: Session, row: TroubleshootingSession) -> list[StepView]:
    proofs = db.exec(select(TSProof).where(TSProof.session_id == row.id)).all()
    files: dict[int, list[dict]] = {}
    for p in sorted(proofs, key=lambda x: x.id):
        files.setdefault(p.step_id, []).append({"id": p.id, "kind": "video" if p.mime.startswith("video/") else "image"})
    return [StepView(id=s.id, ordinal=s.ordinal, title=s.title, instruction=s.instruction, why=s.why,
                     safety_level=s.safety_level, requires_proof=s.requires_proof, proof_kind=s.proof_kind,
                     status=s.status, source_ids=s.source_ids or [], proofs=len(files.get(s.id, [])),
                     proof_files=files.get(s.id, []), buyer_note=s.buyer_note)
            for s in agent.steps_of(db, row)]


def _sources(db: Session, row: TroubleshootingSession) -> list[SourceView]:
    rows = db.exec(select(TSSource).where(TSSource.session_id == row.id).order_by(TSSource.id)).all()
    return [SourceView(id=r.id, title=r.title, url=r.url) for r in rows]


def _messages(db: Session, row: TroubleshootingSession, *, include_system: bool) -> list[MessageView]:
    return [MessageView(id=m.id, role=m.role, body=m.body, step_id=m.step_id, created_at=m.created_at)
            for m in agent.messages_of(db, row) if include_system or m.role != "system"]


# ---- merchant ---------------------------------------------------------------------------

class SessionBrief(BaseModel):
    id: int
    status: str
    product_name: str
    product_model: str | None
    expires_at: datetime
    invited_at: datetime | None
    invited_via: str | None
    buyer_last_seen_at: datetime | None
    language: str | None = None
    symptom: str | None = None
    plan_generic: bool | None = None
    ended_reason: str | None = None


class ProofView(BaseModel):
    id: int
    step_id: int | None
    mime: str
    size: int
    created_at: datetime
    original_mime: str | None = None
    checks: dict | None = None          # type/size/duplicates/capture time/metadata removed (flags, not decisions)
    vision_verdict: dict | None = None  # advisory Gemini look (or why it was skipped)
    evidence_ready: bool = False        # a copy <= 10 MB exists (PayPal's evidence file limit)


class TroubleshootingOut(BaseModel):
    eligible: bool
    why: str
    product_name: str | None
    product_model: str | None
    can_message: bool        # PayPal currently allows send-message on this dispute
    buyer_email_known: bool  # email invite possible
    session: SessionBrief | None
    steps: list[StepView] = []
    sources: list[SourceView] = []
    messages: list[MessageView] = []
    proofs: list[ProofView] = []


class InviteIn(BaseModel):
    via: Literal["paypal_message", "email"] = "paypal_message"
    resend: bool = False  # issue a new link (the old one stops working) and send it again


class InviteOut(BaseModel):
    sent: bool
    already_invited: bool
    via: str | None
    session: SessionBrief
    link: str | None = None  # email stub only: nothing delivers it, so the merchant passes it on


def _brief(row: TroubleshootingSession) -> SessionBrief:
    return SessionBrief(id=row.id, status=row.status.value, product_name=row.product_name,
                        product_model=row.product_model, expires_at=row.expires_at, invited_at=row.invited_at,
                        invited_via=row.invited_via, buyer_last_seen_at=row.buyer_last_seen_at,
                        language=row.language, symptom=row.symptom, plan_generic=row.plan_generic,
                        ended_reason=row.ended_reason)


@merchant_router.get("/api/disputes/{dispute_id}/troubleshooting", response_model=TroubleshootingOut)
def troubleshooting_status(dispute_id: str, session: Session = Depends(get_session)) -> TroubleshootingOut:
    d = _find(session, dispute_id)
    order = session.get(Order, d.order_id) if d.order_id else None
    elig = dispute_eligibility(d, order)
    row = session_for(session, d.id)
    out = TroubleshootingOut(eligible=elig.eligible, why=elig.why, product_name=elig.product_name,
                             product_model=elig.product_model, can_message=can_message(d),
                             buyer_email_known=bool(order and order.buyer_email), session=None)
    if row:
        expire_if_due(session, row)
        out.session = _brief(row)
        out.steps = _steps(session, row)
        out.sources = _sources(session, row)
        out.messages = _messages(session, row, include_system=True)
        out.proofs = [ProofView(id=p.id, step_id=p.step_id, mime=p.mime, size=p.size, created_at=p.created_at,
                                original_mime=p.original_mime, checks=p.checks, vision_verdict=p.vision_verdict,
                                evidence_ready=bool(p.evidence_path))
                      for p in session.exec(select(TSProof).where(TSProof.session_id == row.id).order_by(TSProof.id))]
    return out


@merchant_router.post("/api/disputes/{dispute_id}/troubleshooting/invite", response_model=InviteOut)
async def invite_buyer(dispute_id: str, body: InviteIn | None = None, session: Session = Depends(get_session),
                       paypal: PayPalClient = Depends(get_paypal_client)) -> InviteOut:
    body = body or InviteIn()
    d = _find(session, dispute_id)
    try:
        result = await invite(session, d, via=body.via, resend=body.resend, client=paypal)
    except InviteRefused as exc:
        raise HTTPException(409, {"reason": exc.reason, "email_available": exc.email_available}) from exc
    except PayPalError as exc:
        raise HTTPException(502, {"reason": "PayPal didn't accept the message; nothing was sent. Try again, or "
                                            "send the link by email.",
                                  "paypal_status": exc.status_code, "debug_id": exc.debug_id,
                                  "paypal": exc.body if isinstance(exc.body, dict) else None}) from exc
    return InviteOut(sent=result.sent, already_invited=result.already_invited, via=result.via,
                     session=_brief(result.session), link=result.link)


@merchant_router.get("/api/disputes/{dispute_id}/troubleshooting/proofs/{proof_id}")
def proof_file(dispute_id: str, proof_id: int, session: Session = Depends(get_session)) -> FileResponse:
    d = _find(session, dispute_id)
    proof = session.get(TSProof, proof_id)
    row = session.get(TroubleshootingSession, proof.session_id) if proof else None
    if proof is None or row is None or row.dispute_id != d.id or not Path(proof.file_path).is_file():
        raise HTTPException(404, "unknown proof")
    return FileResponse(proof.file_path, media_type=proof.mime,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


# ---- buyer ------------------------------------------------------------------------------

class BuyerSessionOut(BaseModel):
    id: int
    status: str
    product_name: str
    product_model: str | None
    product_image: str | None = None   # catalog placeholder image (emoji)
    store_name: str = ""
    order_ref: str | None = None       # "Help with your order #<order_ref>"
    paypal_case_id: str | None = None
    expires_at: datetime
    language: str | None
    plan_generic: bool | None
    awaiting_fixed_confirm: bool
    agent_working: bool = False        # typing indicator
    ended: bool
    steps: list[StepView]
    sources: list[SourceView]
    messages: list[MessageView]
    device_key: str | None = None  # returned once, when this browser becomes the link's device
    reply: str | None = None       # the agent's reply to the action just taken


INVALID_LINK = "This troubleshooting link isn't valid. Please use the latest link from the seller's message."
OTHER_DEVICE = ("This troubleshooting link is already open in another browser or device. Please continue there, or "
                "ask the seller to send you a new link.")


def no_merchant_credentials(authorization: str | None = Header(default=None),
                            token: str | None = Query(default=None)) -> None:
    """Buyer routes never accept the merchant's session (header or ?token=), so a signed-in merchant
    can't act as the buyer and a merchant token can't stand in for the buyer's link."""
    if authorization or token:
        raise HTTPException(403, "Merchant sign-in can't be used on the buyer troubleshooting page.")


def _checked(session_id: int, t: str | None, x_help_token: str | None, session: Session) -> TroubleshootingSession:
    """Every buyer request: rate limit, token (constant-time), dispute still open, expiry."""
    rate_limit(session_id)
    row = session.get(TroubleshootingSession, session_id)
    if row is None or not token_matches(t or x_help_token, row.token_hash):
        raise HTTPException(404, INVALID_LINK)  # same answer for unknown session and wrong token
    dispute = session.get(Dispute, row.dispute_id)
    if dispute is None or dispute.status == "RESOLVED":
        raise HTTPException(410, "This case is closed, so the troubleshooting page is no longer available.")
    if is_expired(row.expires_at):
        expire_if_due(session, row)
        raise HTTPException(410, "This troubleshooting link has expired. Please contact the seller.")
    return row


def buyer_link(session_id: int, t: str | None = Query(default=None, max_length=128),
               x_help_token: str | None = Header(default=None, max_length=128),
               x_help_device: str | None = Header(default=None, max_length=128),
               _: None = Depends(no_merchant_credentials),
               session: Session = Depends(get_session)) -> TroubleshootingSession:
    """For the page load: a valid link, and (once bound) the same device."""
    row = _checked(session_id, t, x_help_token, session)
    if row.device_hash and not token_matches(x_help_device, row.device_hash):
        raise HTTPException(403, OTHER_DEVICE)
    return row


def buyer_device(row: TroubleshootingSession = Depends(buyer_link)) -> TroubleshootingSession:
    """For actions: the link must already be bound to this device (opened first via GET)."""
    if not row.device_hash:
        raise HTTPException(409, "Please reload the page and try again.")
    return row


def _header(db: Session, row: TroubleshootingSession) -> dict:
    order = db.get(Order, row.order_id) if row.order_id else None
    dispute = db.get(Dispute, row.dispute_id)
    image = None
    for item in (order.items or []) if order else []:
        product = PRODUCTS.get((item or {}).get("sku") or "")
        if product and (item.get("name") == row.product_name or product.name == row.product_name):
            image = product.image
            break
    ref = (order.invoice_id or str(order.id)) if order else None
    return {"product_image": image, "store_name": get_settings().store_name, "order_ref": ref,
            "paypal_case_id": dispute.paypal_dispute_id if dispute else None}


def _view(db: Session, row: TroubleshootingSession, **extra) -> BuyerSessionOut:
    db.refresh(row)
    return BuyerSessionOut(id=row.id, status=row.status.value, product_name=row.product_name,
                           product_model=row.product_model, expires_at=row.expires_at, language=row.language,
                           plan_generic=row.plan_generic, awaiting_fixed_confirm=bool(row.awaiting_fixed_confirm),
                           agent_working=row.id in _working, ended=row.status not in TS_OPEN_STATUSES,
                           steps=_steps(db, row), sources=_sources(db, row),
                           messages=_messages(db, row, include_system=False), **_header(db, row), **extra)


@buyer_router.get("/{session_id}", response_model=BuyerSessionOut)
def buyer_view(row: TroubleshootingSession = Depends(buyer_link),
               session: Session = Depends(get_session)) -> BuyerSessionOut:
    first_open = row.status == TSStatus.INVITED
    device_key = None
    if not row.device_hash:
        device_key, row.device_hash = new_token()
    row.buyer_last_seen_at = datetime.now(timezone.utc)
    if first_open:
        row.status = TSStatus.ACTIVE
    session.add(row)
    session.commit()
    if first_open:
        log_step(session, row.dispute_id, AGENT, "The buyer opened the troubleshooting page", {"session_id": row.id})
    if row.status in TS_OPEN_STATUSES:
        agent.ensure_opening(session, row)
    return _view(session, row, device_key=device_key)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)
    step_id: int | None = None
    step_status: Literal["DONE_FIXED", "DONE_NOT_FIXED", "SKIPPED", "CANT_DO"] | None = None


class Accepted(BaseModel):
    accepted: bool = True
    agent_working: bool = True


def _ended(exc: agent.SessionEnded) -> HTTPException:
    return HTTPException(409, f"This troubleshooting session has ended ({exc}).")


def _set_working(sid: int, on: bool) -> None:
    (_working.add if on else _working.discard)(sid)
    notify(help_key(sid))


async def _agent_turn(engine, sid: int, text: str, step_id: int | None, step_status: str | None) -> None:  # noqa: ANN001
    """Background: one agent turn with its own DB session. Never leaves the buyer without an answer."""
    try:
        with Session(engine) as db:
            row = db.get(TroubleshootingSession, sid)
            try:
                await agent.agent_reply(db, row, text, step_id=step_id, step_status=step_status)
            except agent.SessionEnded:
                pass
            except Exception as exc:  # noqa: BLE001 - a bug must not leave the typing indicator on forever
                log.exception("troubleshooting turn failed for session %s", sid)
                db.rollback()
                row = db.get(TroubleshootingSession, sid)
                agent._say(db, row, "agent", agent._t(agent.TROUBLE_REPLY, row.language))
                log_step(db, row.dispute_id, AGENT, "The troubleshooting assistant failed on a message",
                         {"session_id": sid, "error": str(exc)[:300]})
    finally:
        _set_working(sid, False)


@buyer_router.post("/{session_id}/messages", response_model=Accepted, status_code=202)
async def buyer_message(body: MessageIn, background: BackgroundTasks,
                        row: TroubleshootingSession = Depends(buyer_device),
                        session: Session = Depends(get_session)) -> Accepted:
    """Accepted at once; the buyer's message and the agent's reply arrive on /events (and in GET)."""
    if not body.body.strip():
        raise HTTPException(422, "Please type a message.")
    if row.status not in TS_OPEN_STATUSES:
        raise _ended(agent.SessionEnded(row.status.value))
    if row.id in _working:
        raise HTTPException(409, "Still working on your last message; please wait.")
    wait_s = chat_limiter.hit(f"chat:{row.id}")
    if wait_s:
        raise HTTPException(429, "You're sending messages very fast; please wait a moment.",
                            headers={"Retry-After": str(max(1, int(wait_s + 0.999)))})
    sent = len(session.exec(select(TSMessage).where(TSMessage.session_id == row.id, TSMessage.role == "buyer")).all())
    if sent >= MAX_BUYER_MESSAGES:
        raise HTTPException(429, "This chat has reached its message limit. Please tap \"Talk to the seller\".")
    _set_working(row.id, True)  # set before returning, so a second message can't start a parallel turn
    background.add_task(_agent_turn, session.get_bind(), row.id, body.body.strip(), body.step_id, body.step_status)
    return Accepted()


class StepIn(BaseModel):
    status: Literal["DONE_FIXED", "DONE_NOT_FIXED", "CANT_DO"]
    note: str = Field(default="", max_length=500)


@buyer_router.post("/{session_id}/steps/{step_id}", response_model=BuyerSessionOut)
def buyer_step(step_id: int, body: StepIn, row: TroubleshootingSession = Depends(buyer_device),
               session: Session = Depends(get_session)) -> BuyerSessionOut:
    try:
        reply = agent.step_button(session, row, step_id, body.status, body.note)
    except agent.SessionEnded as exc:
        raise _ended(exc) from exc
    except agent.StepUnknown as exc:
        raise HTTPException(404, "That step isn't part of this troubleshooting session.") from exc
    return _view(session, row, reply=reply)


class ConfirmIn(BaseModel):
    fixed: bool


@buyer_router.post("/{session_id}/confirm", response_model=BuyerSessionOut)
def buyer_confirm(body: ConfirmIn, row: TroubleshootingSession = Depends(buyer_device),
                  session: Session = Depends(get_session)) -> BuyerSessionOut:
    try:
        msg = agent.confirm_fixed(session, row, body.fixed)
    except agent.SessionEnded as exc:
        raise _ended(exc) from exc
    return _view(session, row, reply=msg)


@buyer_router.post("/{session_id}/handoff", response_model=BuyerSessionOut)
def buyer_handoff(row: TroubleshootingSession = Depends(buyer_device),
                  session: Session = Depends(get_session)) -> BuyerSessionOut:
    try:
        agent.handoff(session, row)
    except agent.SessionEnded as exc:
        raise _ended(exc) from exc
    return _view(session, row)


MULTIPART_OVERHEAD = 64 * 1024


class ProofOut(BaseModel):
    id: int
    step_id: int | None
    mime: str
    size: int
    already_uploaded: bool


class ProofUploadOut(BuyerSessionOut):
    proof: ProofOut


async def _after_upload(engine, proof_id: int, compress: tuple[str, str, float] | None) -> None:  # noqa: ANN001
    """Background: evidence-size copy of a big video (ffmpeg), then the advisory photo check."""
    if compress:
        src, dst, duration = compress
        ok = await P.compress_video(src, dst, duration)
        with Session(engine) as db:
            proof = db.get(TSProof, proof_id)
            if proof:
                proof.checks = {**(proof.checks or {}), "evidence_ready": ok,
                                "evidence_copy": "compressed to under 10 MB" if ok else
                                "not made (ffmpeg missing or failed); the original is over 10 MB"}
                proof.evidence_path = dst if ok else None
                db.add(proof)
                db.commit()
    await vision.check_proof(engine, proof_id)


@buyer_router.post("/{session_id}/steps/{step_id}/proof", response_model=ProofUploadOut)
async def buyer_proof(step_id: int, request: Request, background: BackgroundTasks,
                      row: TroubleshootingSession = Depends(buyer_device),
                      session: Session = Depends(get_session)) -> ProofUploadOut:
    """multipart/form-data with one file field "file". The body is only read after the link and device checks,
    and never beyond the declared Content-Length (the server enforces it), so a huge upload is refused unread."""
    s = get_settings()
    limit = max(s.proof_image_max_bytes, s.proof_video_max_bytes)
    declared = request.headers.get("content-length")
    if not declared or not declared.isdigit():
        raise HTTPException(411, "Please upload the file again.")
    if int(declared) > limit + MULTIPART_OVERHEAD:
        raise HTTPException(413, f"The file is too large (photos up to {s.proof_image_max_bytes // 2**20} MB, "
                                 f"videos up to {s.proof_video_max_bytes // 2**20} MB).")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "Upload the file as multipart/form-data (field \"file\").")
    form = await request.form(max_files=1, max_fields=1)
    try:
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(422, "No file was attached.")
        data = await upload.read()
        declared_type = upload.content_type or ""
    finally:
        await form.close()
    try:
        # image decoding / re-encoding is CPU work: keep it off the event loop (other buyers' live streams)
        result = await run_in_threadpool(agent.record_proof, session, row, step_id, data, declared_type)
    except agent.SessionEnded as exc:
        raise _ended(exc) from exc
    except agent.StepUnknown as exc:
        raise HTTPException(404, "That step isn't part of this troubleshooting session.") from exc
    except agent.ProofRejected as exc:
        status = 413 if "too large" in str(exc) else 422
        raise HTTPException(status, str(exc)) from exc
    p = result.proof
    if not result.already:
        compress = None
        if p.mime.startswith("video/") and not p.evidence_path:
            compress = (p.file_path, p.file_path.rsplit(".", 1)[0] + ".evidence.mp4",
                        float((p.checks or {}).get("duration_s") or 0))
        background.add_task(_after_upload, session.get_bind(), p.id, compress)
    view = _view(session, row, reply=result.reply)
    return ProofUploadOut(**view.model_dump(), proof=ProofOut(id=p.id, step_id=p.step_id, mime=p.mime, size=p.size,
                                                             already_uploaded=result.already))


@buyer_router.get("/{session_id}/proofs/{proof_id}")
def buyer_proof_file(proof_id: int, row: TroubleshootingSession = Depends(buyer_device),
                     session: Session = Depends(get_session)) -> FileResponse:
    """The buyer's own upload (thumbnail/preview). Another session's proof id is simply 'not found'."""
    proof = session.get(TSProof, proof_id)
    if proof is None or proof.session_id != row.id or not Path(proof.file_path).is_file():
        raise HTTPException(404, "unknown file")
    return FileResponse(proof.file_path, media_type=proof.mime,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


# ---- buyer live updates (SSE) ---------------------------------------------------------------------------

SSE_POLL_S = 3.0
SSE_HEARTBEAT_S = 15.0
SSE_MAX_SECONDS = 300


def _state(db: Session, row: TroubleshootingSession) -> dict:
    return {"status": row.status.value, "ended": row.status not in TS_OPEN_STATUSES,
            "awaiting_fixed_confirm": bool(row.awaiting_fixed_confirm), "agent_working": row.id in _working,
            "plan_generic": row.plan_generic, "language": row.language,
            "steps": [s.model_dump() for s in _steps(db, row)],
            "sources": [s.model_dump() for s in _sources(db, row)]}


@buyer_router.get("/{session_id}/events")
async def buyer_events(request: Request, row: TroubleshootingSession = Depends(buyer_device),
                       session: Session = Depends(get_session), after: int = Query(default=0, ge=0),
                       max_seconds: float = Query(default=SSE_MAX_SECONDS, gt=0, le=SSE_MAX_SECONDS),
                       last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    """`event: message` per new chat message (id = message id; resume with ?after= or Last-Event-ID),
    `event: state` whenever status / steps / proofs / typing change, `event: ready` after the backlog,
    `event: bye` when the session ends or the stream times out (the page reconnects). The buyer page uses
    fetch() (not EventSource) so the link token and device key stay in headers, out of URLs and logs."""
    engine, sid = session.get_bind(), row.id
    cursor = max(after, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)
    session.close()  # don't hold a DB connection (SQLite read lock) open for the life of the stream

    async def stream():
        nonlocal cursor
        started = last_beat = time.monotonic()
        last_state = None
        first = True
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            with Session(engine) as db:
                r = db.get(TroubleshootingSession, sid)
                expire_if_due(db, r)
                msgs = db.exec(select(TSMessage).where(TSMessage.session_id == sid, TSMessage.id > cursor,
                                                       TSMessage.role != "system").order_by(TSMessage.id)).all()
                out = []
                for m in msgs:
                    cursor = m.id
                    view = MessageView(id=m.id, role=m.role, body=m.body, step_id=m.step_id, created_at=m.created_at)
                    out.append(f"id: {m.id}\nevent: message\ndata: {view.model_dump_json()}\n\n")
                state = _state(db, r)
            for chunk in out:
                yield chunk
            encoded = json.dumps(state, sort_keys=True, default=str)
            if encoded != last_state:
                last_state = encoded
                yield f"event: state\ndata: {encoded}\n\n"
            if first:
                first = False
                yield f"event: ready\ndata: {json.dumps({'last_id': cursor})}\n\n"
            now = time.monotonic()
            if state["ended"] or now - started >= max_seconds:
                yield "event: bye\ndata: {}\n\n"
                return
            if now - last_beat >= SSE_HEARTBEAT_S:
                last_beat = now
                yield ": ping\n\n"
            await wait(help_key(sid), min(SSE_POLL_S, max(0.01, max_seconds - (now - started))))

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

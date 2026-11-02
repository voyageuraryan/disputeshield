"""Troubleshooting portal API.

merchant_router (mounted behind require_merchant):
  GET  /api/disputes/{id}/troubleshooting                      eligibility, session, steps, transcript, sources
  POST /api/disputes/{id}/troubleshooting/invite               create the session and send the buyer the link
  GET  /api/disputes/{id}/troubleshooting/proofs/{proof_id}    a photo/video the buyer uploaded
buyer_router (public, but every request needs the session's link token; merchant credentials are refused):
  GET  /api/help/{sid}                       the buyer's view; the first open binds the link to that browser
  POST /api/help/{sid}/messages              one chat turn with the troubleshooting agent
  POST /api/help/{sid}/confirm               "Yes, it works now" / "No, still broken" (only way to RESOLVED_FIXED)
  POST /api/help/{sid}/handoff               "Talk to the seller"
  POST /api/help/{sid}/steps/{step}/proof    raw photo/video body (Content-Type = its type)
Buyer requests carry the link token (?t= or X-Help-Token) and, after the first open, the device key
(X-Help-Device): a link opened on one browser can't be used from another (G31: the merchant, who can read the
link in the PayPal thread, can't act as the buyer once the buyer has opened it).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agents import troubleshooter as agent
from app.agents.steps import log_step
from app.api.disputes import _find
from app.config import get_settings
from app.db import get_session
from app.models import (TS_OPEN_STATUSES, Dispute, Order, TroubleshootingSession, TSMessage, TSProof, TSSource,
                        TSStatus)
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.troubleshooting.access import RateLimiter, is_expired, new_token, rate_limit, token_matches
from app.troubleshooting.eligibility import dispute_eligibility
from app.troubleshooting.service import AGENT, InviteRefused, can_message, invite, session_for

merchant_router = APIRouter(tags=["troubleshooting"])
buyer_router = APIRouter(prefix="/api/help", tags=["troubleshooting-buyer"])

MAX_BUYER_MESSAGES = 60      # per session: bounds LLM/search spend
MESSAGE_MAX_CHARS = 1500
chat_limiter = RateLimiter(capacity=6, refill_per_s=0.1)  # chat turns are LLM calls: stricter than page loads
_locks: dict[int, asyncio.Lock] = {}


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


class SourceView(BaseModel):
    id: int
    title: str | None
    url: str


class MessageView(BaseModel):
    role: str
    body: str
    step_id: int | None
    created_at: datetime


def _steps(db: Session, row: TroubleshootingSession) -> list[StepView]:
    proofs = db.exec(select(TSProof).where(TSProof.session_id == row.id)).all()
    count: dict[int, int] = {}
    for p in proofs:
        count[p.step_id] = count.get(p.step_id, 0) + 1
    return [StepView(id=s.id, ordinal=s.ordinal, title=s.title, instruction=s.instruction, why=s.why,
                     safety_level=s.safety_level, requires_proof=s.requires_proof, proof_kind=s.proof_kind,
                     status=s.status, source_ids=s.source_ids or [], proofs=count.get(s.id, 0))
            for s in agent.steps_of(db, row)]


def _sources(db: Session, row: TroubleshootingSession) -> list[SourceView]:
    rows = db.exec(select(TSSource).where(TSSource.session_id == row.id).order_by(TSSource.id)).all()
    return [SourceView(id=r.id, title=r.title, url=r.url) for r in rows]


def _messages(db: Session, row: TroubleshootingSession, *, include_system: bool) -> list[MessageView]:
    return [MessageView(role=m.role, body=m.body, step_id=m.step_id, created_at=m.created_at)
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
        out.proofs = [ProofView(id=p.id, step_id=p.step_id, mime=p.mime, size=p.size, created_at=p.created_at)
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
    expires_at: datetime
    language: str | None
    plan_generic: bool | None
    awaiting_fixed_confirm: bool
    ended: bool
    steps: list[StepView]
    sources: list[SourceView]
    messages: list[MessageView]
    device_key: str | None = None  # returned once, when this browser becomes the link's device
    reply: str | None = None       # the agent's reply to the message just sent


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


def _view(db: Session, row: TroubleshootingSession, **extra) -> BuyerSessionOut:
    db.refresh(row)
    return BuyerSessionOut(id=row.id, status=row.status.value, product_name=row.product_name,
                           product_model=row.product_model, expires_at=row.expires_at, language=row.language,
                           plan_generic=row.plan_generic, awaiting_fixed_confirm=bool(row.awaiting_fixed_confirm),
                           ended=row.status not in TS_OPEN_STATUSES, steps=_steps(db, row),
                           sources=_sources(db, row), messages=_messages(db, row, include_system=False), **extra)


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


def _ended(exc: agent.SessionEnded) -> HTTPException:
    return HTTPException(409, f"This troubleshooting session has ended ({exc}).")


@buyer_router.post("/{session_id}/messages", response_model=BuyerSessionOut)
async def buyer_message(body: MessageIn, row: TroubleshootingSession = Depends(buyer_device),
                        session: Session = Depends(get_session)) -> BuyerSessionOut:
    if not body.body.strip():
        raise HTTPException(422, "Please type a message.")
    wait = chat_limiter.hit(f"chat:{row.id}")
    if wait:
        raise HTTPException(429, "You're sending messages very fast; please wait a moment.",
                            headers={"Retry-After": str(max(1, int(wait + 0.999)))})
    sent = len(session.exec(select(TSMessage).where(TSMessage.session_id == row.id, TSMessage.role == "buyer")).all())
    if sent >= MAX_BUYER_MESSAGES:
        raise HTTPException(429, "This chat has reached its message limit. Please tap \"Talk to the seller\".")
    lock = _locks.setdefault(row.id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "Still working on your last message; please wait.")
    async with lock:
        try:
            result = await agent.agent_reply(session, row, body.body.strip(), step_id=body.step_id,
                                             step_status=body.step_status)
        except agent.SessionEnded as exc:
            raise _ended(exc) from exc
    return _view(session, row, reply=result.message)


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


@buyer_router.post("/{session_id}/steps/{step_id}/proof", response_model=BuyerSessionOut)
async def buyer_proof(step_id: int, request: Request, row: TroubleshootingSession = Depends(buyer_device),
                      session: Session = Depends(get_session)) -> BuyerSessionOut:
    limit = get_settings().proof_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"The file is too large (max {limit // (1024 * 1024)} MB).")
    data = bytearray()
    async for chunk in request.stream():  # stop reading as soon as the limit is passed
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"The file is too large (max {limit // (1024 * 1024)} MB).")
    try:
        agent.record_proof(session, row, step_id, bytes(data), request.headers.get("content-type", ""))
    except agent.SessionEnded as exc:
        raise _ended(exc) from exc
    except agent.ProofRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    return _view(session, row)

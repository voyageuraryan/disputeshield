"""Troubleshooting portal API.

merchant_router (mounted behind require_merchant):
  GET  /api/disputes/{id}/troubleshooting          eligibility + session status for the case page
  POST /api/disputes/{id}/troubleshooting/invite   create the session and send the buyer the link
buyer_router (public, but every request needs the session's token; merchant credentials are refused):
  GET  /api/help/{session_id}?t=<token>            the buyer's view of their session
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agents.steps import log_step
from app.api.disputes import _find
from app.db import get_session
from app.models import (TS_OPEN_STATUSES, Dispute, Order, TroubleshootingSession, TSMessage, TSStatus, TSStep)
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.troubleshooting.access import is_expired, rate_limit, token_matches
from app.troubleshooting.eligibility import dispute_eligibility
from app.troubleshooting.service import AGENT, InviteRefused, can_message, invite, session_for

merchant_router = APIRouter(tags=["troubleshooting"])
buyer_router = APIRouter(prefix="/api/help", tags=["troubleshooting-buyer"])


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


class TroubleshootingOut(BaseModel):
    eligible: bool
    why: str
    product_name: str | None
    product_model: str | None
    can_message: bool        # PayPal currently allows send-message on this dispute
    buyer_email_known: bool  # email invite possible
    session: SessionBrief | None


class InviteIn(BaseModel):
    via: Literal["paypal_message", "email"] = "paypal_message"
    resend: bool = False  # issue a new link (the old one stops working) and send it again


class InviteOut(BaseModel):
    sent: bool
    already_invited: bool
    via: str | None
    session: SessionBrief
    link: str | None = None  # email stub only: nothing delivers it, so the merchant passes it on


def expire_if_due(session: Session, row: TroubleshootingSession) -> bool:
    """Mark an open session EXPIRED once its link is past expiry (whoever looks first: buyer or merchant)."""
    if row.status in TS_OPEN_STATUSES and is_expired(row.expires_at):
        row.status = TSStatus.EXPIRED
        session.add(row)
        session.commit()
        log_step(session, row.dispute_id, AGENT, "The buyer's troubleshooting link expired", {"session_id": row.id})
    return row.status == TSStatus.EXPIRED


def _brief(row: TroubleshootingSession) -> SessionBrief:
    return SessionBrief(id=row.id, status=row.status.value, product_name=row.product_name,
                        product_model=row.product_model, expires_at=row.expires_at, invited_at=row.invited_at,
                        invited_via=row.invited_via, buyer_last_seen_at=row.buyer_last_seen_at)


@merchant_router.get("/api/disputes/{dispute_id}/troubleshooting", response_model=TroubleshootingOut)
def troubleshooting_status(dispute_id: str, session: Session = Depends(get_session)) -> TroubleshootingOut:
    d = _find(session, dispute_id)
    order = session.get(Order, d.order_id) if d.order_id else None
    elig = dispute_eligibility(d, order)
    row = session_for(session, d.id)
    if row:
        expire_if_due(session, row)
    return TroubleshootingOut(eligible=elig.eligible, why=elig.why, product_name=elig.product_name,
                              product_model=elig.product_model, can_message=can_message(d),
                              buyer_email_known=bool(order and order.buyer_email),
                              session=_brief(row) if row else None)


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


# ---- buyer ------------------------------------------------------------------------------

class BuyerStep(BaseModel):
    id: int
    ordinal: int
    title: str
    instruction: str
    why: str | None
    safety_level: str
    requires_proof: bool
    proof_kind: str
    status: str


class BuyerMessage(BaseModel):
    role: str
    body: str
    created_at: datetime


class BuyerSessionOut(BaseModel):
    id: int
    status: str
    product_name: str
    product_model: str | None
    expires_at: datetime
    steps: list[BuyerStep]
    messages: list[BuyerMessage]


INVALID_LINK = "This troubleshooting link isn't valid. Please use the latest link from the seller's message."


def no_merchant_credentials(authorization: str | None = Header(default=None),
                            token: str | None = Query(default=None)) -> None:
    """Buyer routes never accept the merchant's session (header or ?token=), so a signed-in merchant
    can't act as the buyer and a merchant token can't stand in for the buyer's link."""
    if authorization or token:
        raise HTTPException(403, "Merchant sign-in can't be used on the buyer troubleshooting page.")


def buyer_session(session_id: int, t: str | None = Query(default=None, max_length=128),
                  x_help_token: str | None = Header(default=None, max_length=128),
                  _: None = Depends(no_merchant_credentials),
                  session: Session = Depends(get_session)) -> TroubleshootingSession:
    """Every buyer request: rate limit, token (constant-time), expiry, dispute still open."""
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


@buyer_router.get("/{session_id}", response_model=BuyerSessionOut)
def buyer_view(row: TroubleshootingSession = Depends(buyer_session),
               session: Session = Depends(get_session)) -> BuyerSessionOut:
    first_open = row.status == TSStatus.INVITED
    row.buyer_last_seen_at = datetime.now(timezone.utc)
    if first_open:
        row.status = TSStatus.ACTIVE
    session.add(row)
    session.commit()
    if first_open:
        log_step(session, row.dispute_id, AGENT, "The buyer opened the troubleshooting page", {"session_id": row.id})
    steps = session.exec(select(TSStep).where(TSStep.session_id == row.id).order_by(TSStep.ordinal)).all()
    msgs = session.exec(select(TSMessage).where(TSMessage.session_id == row.id, TSMessage.role != "system")
                        .order_by(TSMessage.id)).all()
    return BuyerSessionOut(id=row.id, status=row.status.value, product_name=row.product_name,
                           product_model=row.product_model, expires_at=row.expires_at,
                           steps=[BuyerStep(**st.model_dump(include=set(BuyerStep.model_fields))) for st in steps],
                           messages=[BuyerMessage(role=m.role, body=m.body, created_at=m.created_at) for m in msgs])

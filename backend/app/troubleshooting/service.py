"""Create troubleshooting sessions and invite the buyer (merchant button or automatic after investigation).

Invites go through PayPal's dispute thread (send-message) only when PayPal allows messaging on this
dispute; otherwise the merchant may choose email (a dev stub that writes to the timeline). The access
token is never stored or logged: only its hash is saved, and the link exists only in the message sent
(and, for the email stub, in the API response so the merchant can pass it on while email is a stub).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.agents.steps import log_step
from app.merchant import get_merchant_settings
from app.models import (TS_OPEN_STATUSES, Dispute, Order, TroubleshootingSession, TSMessage, TSStatus)
from app.paypal.client import PayPalClient, PayPalError
from app.paypal.disputes import MESSAGE_MAX, allowed_actions, send_message
from app.troubleshooting.access import build_link, compute_expiry, is_expired, new_token
from app.troubleshooting.eligibility import dispute_eligibility

log = logging.getLogger(__name__)
AGENT = "troubleshooter"
VIA = ("paypal_message", "email")


class InviteRefused(Exception):
    def __init__(self, reason: str, *, email_available: bool = False):
        super().__init__(reason)
        self.reason, self.email_available = reason, email_available


@dataclass
class InviteResult:
    session: TroubleshootingSession
    sent: bool
    already_invited: bool
    via: str | None
    link: str | None = None  # only for the email stub (nothing actually delivers it)


def session_for(s: Session, dispute_id: int) -> TroubleshootingSession | None:
    return s.exec(select(TroubleshootingSession).where(TroubleshootingSession.dispute_id == dispute_id)).first()


def _day(dt: datetime) -> str:
    return f"{dt.day} {dt:%b %Y}, {dt:%H:%M} UTC"


def invite_text(product: str, model: str | None, link: str, expires_at: datetime) -> str:
    what = f"{product} ({model})" if model else product
    text = (f"Hi, we're sorry your {what} isn't working as it should. We've set up a short, guided "
            f"troubleshooting page for it: {link}\n"
            f"The link is private to you and works until {_day(expires_at)}. If the steps don't fix it, the page "
            f"lets you show us what's happening so we can put it right quickly.")
    return text[:MESSAGE_MAX]


def can_message(dispute: Dispute) -> bool:
    return "send_message" in allowed_actions(dispute.raw)


async def invite(s: Session, dispute: Dispute, *, via: str = "paypal_message", resend: bool = False,
                 client: PayPalClient | None = None, actor: str = "merchant",
                 now: datetime | None = None) -> InviteResult:
    """Create the dispute's session if needed and send the buyer a private link. Idempotent: a buyer who
    already has a working link isn't messaged again unless resend=True (which issues a new link and
    invalidates the old one)."""
    if via not in VIA:
        raise ValueError(f"via must be one of {VIA}")
    now = now or datetime.now(timezone.utc)
    order = s.get(Order, dispute.order_id) if dispute.order_id else None
    elig = dispute_eligibility(dispute, order)
    if not elig.eligible:
        raise InviteRefused(elig.why)

    existing = session_for(s, dispute.id)
    if existing and existing.status in TS_OPEN_STATUSES and not is_expired(existing.expires_at, now) and not resend:
        return InviteResult(existing, sent=False, already_invited=True, via=existing.invited_via)
    if existing and existing.status not in TS_OPEN_STATUSES and existing.status != TSStatus.EXPIRED:
        raise InviteRefused(f"The troubleshooting session has already finished ({existing.status.value}).")

    expires_at = compute_expiry(dispute.seller_response_due, now)
    if is_expired(expires_at, now):
        raise InviteRefused("Too close to PayPal's response deadline: the buyer's link must expire 48 hours "
                            "before it, and that time has passed.")
    buyer_email = order.buyer_email if order else None
    if via == "paypal_message" and not can_message(dispute):
        raise InviteRefused("PayPal doesn't allow messages on this dispute right now (messages are only possible "
                            "while it is an inquiry). You can send the link by email instead.",
                            email_available=bool(buyer_email))
    if via == "email" and not buyer_email:
        raise InviteRefused("There is no buyer email on this order, so the link can't be emailed.")

    token, token_hash = new_token()
    created = existing is None
    row = existing or TroubleshootingSession(dispute_id=dispute.id, order_id=dispute.order_id,
                                             product_name=elig.product_name or "your item", token_hash=token_hash,
                                             expires_at=expires_at)
    row.product_name, row.product_model = elig.product_name or row.product_name, elig.product_model
    row.token_hash, row.expires_at, row.status = token_hash, expires_at, TSStatus.INVITED
    s.add(row)
    s.flush()  # need the id for the link; nothing is committed until the invite is sent
    link = build_link(row.id, token)

    if via == "paypal_message":
        try:
            await send_message(dispute.paypal_dispute_id, invite_text(row.product_name, row.product_model, link,
                                                                      expires_at), client=client)
        except PayPalError as exc:
            s.rollback()
            log_step(s, dispute.id, AGENT, "Couldn't send the troubleshooting invite through PayPal",
                     {"paypal_status": exc.status_code, "debug_id": exc.debug_id,
                      "paypal": exc.body if isinstance(exc.body, dict) else None, "actor": actor})
            raise
        what = "Sent the buyer a private troubleshooting link in the PayPal dispute thread"
    else:
        what = f"Email invite to {buyer_email} recorded (dev stub: no email is actually sent)"

    row.invited_at, row.invited_via = now, via
    s.add(row)
    s.add(TSMessage(session_id=row.id, role="system",
                    body=f"Invite {'re-sent' if resend and not created else 'sent'} via "
                         f"{'PayPal message' if via == 'paypal_message' else 'email'}"))
    s.commit()
    if created:
        log_step(s, dispute.id, AGENT, f"Created a troubleshooting session for the {row.product_name}",
                 {"session_id": row.id, "model": row.product_model})
    log_step(s, dispute.id, AGENT, what + (" (new link; the old one no longer works)" if resend and not created else ""),
             {"session_id": row.id, "via": via, "expires_at": expires_at.isoformat(), "actor": actor})
    s.refresh(row)
    return InviteResult(row, sent=True, already_invited=False, via=via, link=link if via == "email" else None)


async def auto_invite(s: Session, dispute: Dispute, client: PayPalClient | None = None) -> None:
    """After investigation: invite automatically when the merchant turned it on. Never raises."""
    try:
        if not get_merchant_settings(s).auto_invite_troubleshooting:
            return
        order = s.get(Order, dispute.order_id) if dispute.order_id else None
        if not dispute_eligibility(dispute, order).eligible or session_for(s, dispute.id) is not None:
            return
        await invite(s, dispute, via="paypal_message", client=client, actor="auto")
    except InviteRefused as exc:
        log_step(s, dispute.id, AGENT, f"Auto-invite skipped: {exc.reason}")
    except Exception as exc:  # PayPal down, etc.: the investigation must still go on
        log.exception("auto-invite failed for dispute %s", dispute.id)
        s.rollback()
        log_step(s, dispute.id, AGENT, "Auto-invite failed; invite the buyer from the case page",
                 {"error": str(exc)[:300]})

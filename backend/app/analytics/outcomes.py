"""Outcome capture (Phase 14): when a dispute resolves, one DisputeOutcome row (idempotent per dispute).

PayPal fields (Disputes v1 OpenAPI, schema dispute_outcome): outcome_code enum RESOLVED_BUYER_FAVOUR,
RESOLVED_SELLER_FAVOUR, RESOLVED_WITH_PAYOUT, CANCELED_BY_BUYER, ACCEPTED (deprecated: "PayPal accepted the
dispute"), DENIED (deprecated: "PayPal denied the dispute"), NONE ("closed without any decision" because a new
dispute was opened for the same transaction); amount_refunded (money). An accepted merchant offer is read from
offer.history (BUYER ACCEPTED), since PayPal has no outcome code for it.

Normalised outcome:
  OFFER_ACCEPTED  the buyer accepted our offer (offer.history), whatever the outcome code says
  SELLER_FAVOUR   RESOLVED_SELLER_FAVOUR, or the deprecated DENIED
  BUYER_FAVOUR    RESOLVED_BUYER_FAVOUR, or the deprecated ACCEPTED (also when the merchant refunded in full)
  CANCELLED       CANCELED_BY_BUYER (the merchant keeps the money)
  PAYOUT          RESOLVED_WITH_PAYOUT (PayPal protection paid out; who was covered isn't stated: not a win or a loss)
  NONE            NONE or no outcome code
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlmodel import Session, select

from app.models import Decision, DecisionStatus, Dispute, DisputeOutcome
from app.paypal.disputes import offer_events

CODE_MAP = {"RESOLVED_SELLER_FAVOUR": "SELLER_FAVOUR", "DENIED": "SELLER_FAVOUR",
            "RESOLVED_BUYER_FAVOUR": "BUYER_FAVOUR", "ACCEPTED": "BUYER_FAVOUR",
            "CANCELED_BY_BUYER": "CANCELLED", "RESOLVED_WITH_PAYOUT": "PAYOUT", "NONE": "NONE"}
OUTCOMES = ("SELLER_FAVOUR", "BUYER_FAVOUR", "CANCELLED", "OFFER_ACCEPTED", "PAYOUT", "NONE")


def _ts(value) -> datetime | None:  # noqa: ANN001
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalise(raw: dict | None) -> tuple[str, str | None, Decimal | None, Decimal | None]:
    """(outcome, outcome_code, amount_refunded, accepted offer amount) from PayPal's resolved dispute."""
    raw = raw or {}
    out = raw.get("dispute_outcome") or {}
    code = out.get("outcome_code")
    refunded = (out.get("amount_refunded") or {}).get("value")
    events = offer_events(raw)
    accepted = [e for e in events if e["actor"] == "BUYER" and e["event_type"] == "ACCEPTED"]
    if accepted:
        amt = accepted[-1]["amount"]
        return "OFFER_ACCEPTED", code, Decimal(refunded) if refunded else None, Decimal(amt) if amt else None
    return CODE_MAP.get(code or "NONE", "NONE"), code, Decimal(refunded) if refunded else None, None


def record_outcome(session: Session, dispute: Dispute) -> DisputeOutcome:
    """Insert or update this dispute's outcome row (no commit: the caller commits)."""
    outcome, code, refunded, accepted_offer = normalise(dispute.raw)
    decisions = session.exec(select(Decision).where(Decision.dispute_id == dispute.id)
                             .order_by(Decision.id)).all()
    executed = [d for d in decisions if d.status == DecisionStatus.EXECUTED]
    acted = executed[-1] if executed else None
    basis = acted or (decisions[-1] if decisions else None)
    final = (acted.approved_action or acted.recommendation) if acted else "NONE"
    rec = basis.recommendation if basis else None
    row = session.exec(select(DisputeOutcome).where(DisputeOutcome.dispute_id == dispute.id)).first() \
        or DisputeOutcome(dispute_id=dispute.id, ref=dispute.paypal_dispute_id, reason=dispute.reason,
                          amount=dispute.amount, currency=dispute.currency, outcome=outcome, final_action=final)
    raw = dispute.raw or {}
    row.reason, row.stage, row.amount, row.currency = dispute.reason, dispute.stage, dispute.amount, dispute.currency
    row.outcome, row.outcome_code, row.amount_refunded = outcome, code, refunded
    row.offer_amount = accepted_offer if accepted_offer is not None else (acted.offer_amount if acted else None)
    row.final_action, row.recommended_action = final, rec
    row.followed = None if rec in (None, "GATHER_MORE") else final == rec
    row.rule_score = basis.rule_score if basis else None
    row.created_at = _ts(raw.get("create_time"))
    row.due_at = _ts(dispute.seller_response_due)
    row.response_at = _ts(min((d.executed_at for d in executed if d.executed_at), default=None))
    row.resolved_at = _ts(raw.get("update_time")) or datetime.now(timezone.utc)
    session.add(row)
    return row

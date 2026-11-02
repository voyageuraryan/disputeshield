"""Dispute ingestion shared by the webhook endpoint and the poller.

ingest_dispute(): fetch full details with get_dispute, upsert the disputes row,
match it to a store order, and claim an agent run for this dispute version.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.troubleshooting.service import close_for_resolved_dispute
from app.models import AgentStep, Decision, DecisionStatus, Dispute, Order, ProcessedEvent
from app.paypal.client import PayPalClient
from app.paypal.disputes import Dispute as PayPalDispute
from app.paypal.disputes import allowed_actions, get_dispute

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    dispute_row_id: int
    paypal_dispute_id: str
    order_id: int | None
    run_key: str | None  # set when this call claimed a new agent run


def match_order(session: Session, pp: PayPalDispute) -> int | None:
    """Link a dispute to a store order.

    Order of preference: capture id (PayPal's seller_transaction_id is the capture id),
    then invoice_number == our invoice_id, then custom == our order id. Only orders that
    went through PayPal checkout are eligible, so seeded store-only rows never match.
    """
    paid = Order.paypal_order_id.is_not(None)
    for tx in pp.disputed_transactions:
        if tx.seller_transaction_id:
            o = session.exec(select(Order).where(Order.capture_id == tx.seller_transaction_id)).first()
            if o:
                return o.id
    for tx in pp.disputed_transactions:
        if tx.invoice_number:
            o = session.exec(select(Order).where(Order.invoice_id == tx.invoice_number, paid)).first()
            if o:
                return o.id
    for tx in pp.disputed_transactions:
        if tx.custom and tx.custom.isdigit():
            o = session.exec(select(Order).where(Order.id == int(tx.custom), paid)).first()
            if o:
                return o.id
    return None


def upsert_dispute(session: Session, pp: PayPalDispute) -> Dispute:
    row = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == pp.dispute_id)).first()
    if row is None:
        row = Dispute(paypal_dispute_id=pp.dispute_id, reason="", status="", amount=Decimal("0"), currency="")
    tx = pp.disputed_transactions[0] if pp.disputed_transactions else None
    row.transaction_id = (tx.seller_transaction_id if tx else None) or row.transaction_id
    row.reason = pp.reason or row.reason or "OTHER"
    row.stage = pp.dispute_life_cycle_stage or row.stage
    row.status = pp.status or row.status or "OTHER"
    if pp.dispute_amount:
        row.amount = pp.dispute_amount.value
        row.currency = pp.dispute_amount.currency_code
    row.seller_response_due = pp.seller_response_due_date
    row.raw = pp.raw  # PayPal's original JSON
    row.refreshed_at = datetime.now(timezone.utc)
    matched = match_order(session, pp)
    if matched is not None:
        row.order_id = matched
    session.add(row)
    session.flush()
    if row.status == "RESOLVED":
        close_pending_decisions(session, row, "Dispute resolved at PayPal")
    return row


def close_pending_decisions(session: Session, dispute: Dispute, why: str) -> list[int]:
    """A resolved dispute can't be answered any more: retire PROPOSED decisions so nobody approves them."""
    pending = session.exec(select(Decision).where(Decision.dispute_id == dispute.id,
                                                  Decision.status == DecisionStatus.PROPOSED)).all()
    for d in pending:
        d.status = DecisionStatus.SUPERSEDED
        session.add(d)
    outcome = ((dispute.raw or {}).get("dispute_outcome") or {}).get("outcome_code")
    dispute.agent_status = "RESOLVED"
    session.add(dispute)
    close_for_resolved_dispute(session, dispute)
    session.add(AgentStep(dispute_id=dispute.id, agent="orchestrator",
                          action=f"{why}" + (f" ({outcome})" if outcome else "")
                          + (f"; closed pending decision(s) {[d.id for d in pending]}" if pending else ""),
                          detail={"outcome": outcome, "closed_decision_ids": [d.id for d in pending]}))
    return [d.id for d in pending]


def material_state(pp: PayPalDispute, ts_status: str | None = None) -> dict:
    """What changed that matters to the merchant. update_time alone is not enough:
    PayPal bumps it (and fires CUSTOMER.DISPUTE.UPDATED) when *we* respond, which must not
    start a new analysis and put the case back to "awaiting your approval".
    ts_status: the buyer troubleshooting session's FINAL status (Phase 9D), new material for the analysis.
    Added only when there is one, so run keys of disputes without a finished session are unchanged."""
    raw = pp.raw or {}
    due = pp.seller_response_due_date
    state = {
        "status": pp.status, "stage": pp.dispute_life_cycle_stage, "reason": pp.reason,
        "state": raw.get("dispute_state"),
        "amount": str(pp.dispute_amount.value) if pp.dispute_amount else None,
        "due": due.isoformat() if isinstance(due, datetime) else None,
        "actions": allowed_actions(raw),
        "messages": len(raw.get("messages") or []),
    }
    if ts_status:
        state["troubleshooting"] = ts_status
    return state


def run_key(pp: PayPalDispute, ts_status: str | None = None) -> str:
    """One agent run per material version of the dispute (stable across PayPal's own re-saves)."""
    m = material_state(pp, ts_status)
    version = "|".join(f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in m.items())
    return f"run:{pp.dispute_id}:{hashlib.sha1(version.encode()).hexdigest()[:16]}"


def seller_turn_again(prev_status: str | None, prev_stage: str | None, pp: PayPalDispute) -> bool:
    """After the merchant has responded, only a new request for the seller deserves a new analysis."""
    if pp.dispute_life_cycle_stage and prev_stage and pp.dispute_life_cycle_stage != prev_stage:
        return True  # escalated, e.g. INQUIRY -> CHARGEBACK
    return pp.status == "WAITING_FOR_SELLER_RESPONSE" and prev_status != "WAITING_FOR_SELLER_RESPONSE"


def claim(session: Session, key: str, kind: str, dispute_row_id: int | None, detail: dict | None = None) -> bool:
    """Insert an idempotency key. False if it was already claimed."""
    if session.exec(select(ProcessedEvent).where(ProcessedEvent.key == key)).first():
        return False
    try:
        with session.begin_nested():
            session.add(ProcessedEvent(key=key, kind=kind, dispute_id=dispute_row_id, detail=detail))
    except IntegrityError:  # concurrent claim
        return False
    return True


def responded_since_last_run(session: Session, dispute_row_id: int) -> bool:
    """True when the newest decision on this dispute was sent to PayPal (or is being sent)."""
    latest = session.exec(select(Decision).where(Decision.dispute_id == dispute_row_id)
                          .order_by(Decision.id.desc())).first()
    return latest is not None and latest.status in (DecisionStatus.EXECUTED, DecisionStatus.APPROVED)


def already_processed(session: Session, key: str) -> bool:
    return session.exec(select(ProcessedEvent).where(ProcessedEvent.key == key)).first() is not None


async def ingest_dispute(
    session: Session,
    paypal_dispute_id: str,
    *,
    client: PayPalClient | None = None,
    event_key: str | None = None,
    event_detail: dict | None = None,
) -> IngestResult:
    """Fetch, upsert, match, and claim an agent run. Commits once at the end, so a
    failure leaves nothing half-recorded and the webhook/poll can safely retry."""
    pp = await get_dispute(paypal_dispute_id, client=client)
    before = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == pp.dispute_id)).first()
    prev_status, prev_stage = (before.status, before.stage) if before else (None, None)
    row = upsert_dispute(session, pp)
    claimed = None
    from app.troubleshooting.outcome import is_final, session_of

    ts = session_of(session, row.id)
    key = run_key(pp, ts.status.value if is_final(ts) else None)
    if claim(session, key, "agent_run", row.id, {"status": pp.status}):
        claimed = key
        if responded_since_last_run(session, row.id) and not seller_turn_again(prev_status, prev_stage, pp):
            claimed = None  # PayPal is processing our response; keep the case as "response sent"
            session.add(AgentStep(dispute_id=row.id, agent="orchestrator",
                                  action=f"PayPal updated the case after your response (status {pp.status}); "
                                         "nothing new is asked of you, so no new analysis",
                                  detail={"status": pp.status, "stage": pp.dispute_life_cycle_stage,
                                          "allowed_actions": allowed_actions(pp.raw)}))
    if event_key:
        claim(session, event_key, "webhook", row.id, event_detail)
    session.commit()
    log.info("ingested dispute %s (order=%s, new_run=%s)", pp.dispute_id, row.order_id, bool(claimed))
    return IngestResult(row.id, pp.dispute_id, row.order_id, claimed)

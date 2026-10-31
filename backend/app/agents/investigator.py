"""Investigator: pure data gathering, no LLM.

Looks up, in order:
  PayPal: the dispute, the disputed capture, the order's shipment trackers
  Store:  the order, shipment, messages, product listing, buyer's prior orders
Each finding becomes an evidence_items row (source + source_ref, upserted by
source_ref so re-runs don't duplicate) and each lookup logs one agent_steps row
with a human-readable action. Returns the CaseFile built from those rows.

Facts only: summaries state what the record says. Judging what the evidence is
worth is the rules' and analyst's job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.agents.steps import log_step
from app.models import Dispute, EvidenceItemRow, EvidenceSource, Message, MessageSender, Order, Shipment
from app.paypal.client import PayPalClient, PayPalError
from app.paypal.disputes import allowed_actions, get_dispute
from app.paypal.orders import get_order
from app.paypal.payments import get_capture
from app.paypal.tracking import trackers_from_order
from app.schemas import CaseFile, EvidenceItem

log = logging.getLogger(__name__)
AGENT = "investigator"
FRESH_S = 60  # reuse a dispute fetched this recently instead of fetching it again


# ---- formatting helpers -------------------------------------------------------

def _day(value: datetime | str | None) -> str:
    """'3 Nov' style date for timeline text."""
    if value is None:
        return "unknown date"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return f"{value.day} {value:%b}"


def _addr(a: dict | None) -> str:
    if not a:
        return "no address"
    parts = [a.get("address_line_1"), a.get("admin_area_2"), a.get("admin_area_1"), a.get("postal_code")]
    return ", ".join(p for p in parts if p)


def _same_address(a: dict | None, b: dict | None) -> bool | None:
    if not a or not b:
        return None
    key = ("address_line_1", "postal_code", "country_code")
    norm = lambda d: tuple((d.get(k) or "").strip().lower() for k in key)  # noqa: E731
    return norm(a) == norm(b)


def _money(m: dict | None) -> str:
    return f"{m['value']} {m['currency_code']}" if m else "unknown amount"


# ---- evidence writing --------------------------------------------------------

def _upsert_evidence(session: Session, dispute: Dispute, *, type: str, source: str, source_ref: str,
                     summary: str, flags: list[str] | None = None) -> EvidenceItemRow:
    row = session.exec(select(EvidenceItemRow).where(
        EvidenceItemRow.dispute_id == dispute.id, EvidenceItemRow.source_ref == source_ref)).first()
    if row is None:
        row = EvidenceItemRow(dispute_id=dispute.id, type=type, source=EvidenceSource(source),
                              source_ref=source_ref, summary=summary, flags=flags or [])
    else:
        row.type, row.summary, row.flags = type, summary, flags or []
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def build_case_file(session: Session, dispute: Dispute) -> CaseFile:
    rows = session.exec(select(EvidenceItemRow).where(EvidenceItemRow.dispute_id == dispute.id)
                        .order_by(EvidenceItemRow.id)).all()
    return CaseFile(
        dispute_id=dispute.paypal_dispute_id,
        reason=dispute.reason,
        amount=dispute.amount,
        currency=dispute.currency,
        due_date=dispute.seller_response_due,
        order_id=dispute.order_id,
        status=dispute.status,
        stage=dispute.stage,
        dispute_state=(dispute.raw or {}).get("dispute_state"),
        allowed_actions=allowed_actions(dispute.raw),
        evidence=[EvidenceItem(id=r.id, type=r.type, source=r.source.value, source_ref=r.source_ref,
                               summary=r.summary, flags=r.flags or []) for r in rows],
    )


# ---- the investigation ---------------------------------------------------------

class Investigator:
    def __init__(self, session: Session, dispute: Dispute, client: PayPalClient | None = None):
        self.s, self.d, self.client = session, dispute, client

    def step(self, action: str, detail: dict | None = None) -> None:
        log_step(self.s, self.d.id, AGENT, action, detail)

    def evidence(self, **kw: Any) -> EvidenceItemRow:
        return _upsert_evidence(self.s, self.d, **kw)

    async def run(self) -> CaseFile:
        d = self.d
        pp = await self._paypal_dispute()
        capture_id = d.transaction_id
        order = self.s.get(Order, d.order_id) if d.order_id else None

        await self._capture(capture_id)
        await self._paypal_trackers(order)
        if order is None:
            self.step("No store order matched this dispute", {"transaction_id": capture_id})
        else:
            self._order(order)
            self._shipment(order)
            self._messages(order)
            self._listing(order)
            self._buyer_history(order)
        self._refund_policy()
        self._paypal_dispute_messages(pp)
        case = build_case_file(self.s, d)
        self.step(f"Case file ready: {len(case.evidence)} evidence items",
                  {"evidence_ids": [e.id for e in case.evidence]})
        return case

    # PayPal lookups (failures are logged and the investigation continues)

    async def _paypal_dispute(self) -> dict:
        d = self.d
        fetched = d.refreshed_at.replace(tzinfo=d.refreshed_at.tzinfo or timezone.utc) if d.refreshed_at else None
        if fetched and d.raw and (datetime.now(timezone.utc) - fetched).total_seconds() < FRESH_S:
            raw = d.raw  # ingest fetched it moments ago; skip a second identical GET
            self.step(f"Using the dispute as fetched from PayPal {int((datetime.now(timezone.utc) - fetched).total_seconds())}s "
                      f"ago: {raw.get('reason')}, {raw.get('dispute_life_cycle_stage')}, {raw.get('status')}, "
                      f"{_money(raw.get('dispute_amount'))}", {"source_ref": f"paypal:dispute/{d.paypal_dispute_id}"})
            acts = allowed_actions(raw)
            self.step("PayPal currently allows: " + (", ".join(acts) if acts else "no merchant actions"),
                      {"allowed_actions": acts, "dispute_state": raw.get("dispute_state")})
            return raw
        try:
            fresh = await get_dispute(d.paypal_dispute_id, client=self.client)
            raw = fresh.raw
            d.raw, d.status, d.refreshed_at = raw, fresh.status or d.status, datetime.now(timezone.utc)
            d.stage = fresh.dispute_life_cycle_stage or d.stage
            self.s.add(d)
            self.s.commit()
        except PayPalError as exc:
            raw = d.raw or {}
            self.step("Could not refresh dispute from PayPal; using last stored copy",
                      {"debug_id": exc.debug_id, "status": exc.status_code})
            return raw
        due = raw.get("seller_response_due_date")
        self.step(
            f"Fetched dispute {d.paypal_dispute_id}: {raw.get('reason')}, {raw.get('dispute_life_cycle_stage')}, "
            f"{raw.get('status')}, {_money(raw.get('dispute_amount'))}"
            + (f", seller response due {_day(due)}" if due else ""),
            {"source_ref": f"paypal:dispute/{d.paypal_dispute_id}"})
        acts = allowed_actions(raw)
        self.step("PayPal currently allows: " + (", ".join(acts) if acts else "no merchant actions"),
                  {"allowed_actions": acts, "dispute_state": raw.get("dispute_state")})
        return raw

    async def _capture(self, capture_id: str | None) -> None:
        if not capture_id:
            self.step("No disputed transaction id on the dispute")
            return
        try:
            cap = await get_capture(capture_id, client=self.client)
        except (PayPalError, ValueError) as exc:
            self.step(f"Could not fetch transaction {capture_id} from PayPal",
                      {"error": str(exc), "debug_id": getattr(exc, "debug_id", None)})
            return
        sp = cap.get("seller_protection") or {}
        reason = (cap.get("status_details") or {}).get("reason")
        summary = (f"PayPal capture {capture_id}: {_money(cap.get('amount'))}, status {cap.get('status')}"
                   + (f" ({reason})" if reason else "")
                   + f", captured {_day(cap.get('create_time'))}, invoice {cap.get('invoice_id') or '-'}"
                   + f"; seller protection {sp.get('status', 'unknown')}"
                   + (f" for {', '.join(sp['dispute_categories'])}" if sp.get("dispute_categories") else ""))
        flags = [f"seller_protection:{sp.get('status', 'UNKNOWN')}"]
        flags += [f"protected:{c}" for c in sp.get("dispute_categories") or []]
        row = self.evidence(type="invoice", source="paypal", source_ref=f"paypal:capture/{capture_id}",
                            summary=summary, flags=flags)
        self.step(f"Found transaction {capture_id}: {_money(cap.get('amount'))}, "
                  f"seller protection {sp.get('status', 'unknown')}", {"evidence_id": row.id})

    async def _paypal_trackers(self, order: Order | None) -> None:
        if order is None or not order.paypal_order_id:
            self.step("No PayPal order to check for tracking")
            return
        try:
            pp_order = await get_order(order.paypal_order_id, client=self.client)
        except (PayPalError, ValueError) as exc:
            self.step(f"Could not fetch PayPal order {order.paypal_order_id} for tracking",
                      {"error": str(exc), "debug_id": getattr(exc, "debug_id", None)})
            return
        trackers = trackers_from_order(pp_order)
        if not trackers:
            self.step("No tracking uploaded to PayPal for this order")
            return
        for t in trackers:
            carrier = self._carrier_for(order, t["tracking_number"])
            summary = (f"Tracking {t['tracking_number']}" + (f" ({carrier})" if carrier else "")
                       + f" registered with PayPal for capture {t['capture_id']}, status {t['status']}")
            row = self.evidence(type="tracking", source="paypal",
                                source_ref=f"paypal:order/{order.paypal_order_id}/tracker/{t['id']}", summary=summary,
                                flags=["tracker_cancelled"] if t["status"] == "CANCELLED" else [])
            self.step(f"Found tracking {t['tracking_number']}" + (f" ({carrier})" if carrier else "")
                      + f" on PayPal, {t['status'].lower() if t['status'] else 'unknown status'}",
                      {"evidence_id": row.id})

    def _carrier_for(self, order: Order, tracking_number: str | None) -> str | None:
        ship = self.s.exec(select(Shipment).where(Shipment.order_id == order.id,
                                                  Shipment.tracking_number == tracking_number)).first()
        return ship.carrier if ship else None

    def _paypal_dispute_messages(self, raw: dict) -> None:
        msgs = [m for m in raw.get("messages") or [] if (m.get("posted_by") or "").upper() == "BUYER"]
        for i, m in enumerate(msgs):
            row = self.evidence(type="buyer_message", source="paypal",
                                source_ref=f"paypal:dispute/{self.d.paypal_dispute_id}/message/{i}",
                                summary=f"Buyer wrote in the PayPal case on {_day(m.get('time_posted'))}: "
                                        f"\"{(m.get('content') or '').strip()}\"")
            self.step(f"Found buyer message in PayPal case ({_day(m.get('time_posted'))})", {"evidence_id": row.id})

    # Store lookups

    def _order(self, order: Order) -> None:
        items = ", ".join(f"{i.get('quantity', 1)}x {i.get('name')}" for i in order.items or []) or "no items"
        summary = (f"Store order #{order.id} on {_day(order.created_at)}: {items}, {order.amount} {order.currency}; "
                   f"buyer {order.buyer_email or 'unknown'}; ship to {_addr(order.shipping_address)}")
        row = self.evidence(type="order", source="store", source_ref=f"store:order/{order.id}", summary=summary)
        self.step(f"Found order #{order.id} ({items}, {order.amount} {order.currency})", {"evidence_id": row.id})

    def _shipment(self, order: Order) -> None:
        ships = self.s.exec(select(Shipment).where(Shipment.order_id == order.id).order_by(Shipment.id)).all()
        if not ships:
            self.step(f"No shipment or tracking recorded for order #{order.id}")
            return
        for sh in ships:
            label = f"{sh.tracking_number or 'untracked'}" + (f" ({sh.carrier})" if sh.carrier else "")
            summary = f"Shipment {label} for order #{order.id}, status {sh.status or 'unknown'}"
            if sh.delivered_at:
                summary += f", delivered {_day(sh.delivered_at)}"
            row = self.evidence(type="tracking", source="store", source_ref=f"store:shipment/{sh.id}", summary=summary,
                                flags=(["delivered"] if sh.delivered_at else [])
                                + ([] if sh.tracking_number else ["untracked"]))
            action = f"Found tracking {label}, " + (f"delivered {_day(sh.delivered_at)}" if sh.delivered_at
                                                    else (sh.status or "unknown status").lower().replace("_", " "))
            self.step(action, {"evidence_id": row.id})
            if sh.delivered_at:
                same = _same_address(sh.delivered_address, order.shipping_address)
                where = _addr(sh.delivered_address)
                match = {True: "matches the order's shipping address",
                         False: f"does NOT match the order's shipping address ({_addr(order.shipping_address)})",
                         None: "address not recorded"}[same]
                row = self.evidence(type="delivery_confirmation", source="store",
                                    source_ref=f"store:shipment/{sh.id}/delivery",
                                    summary=f"Carrier delivered {_day(sh.delivered_at)} to {where}; {match}",
                                    flags=[{True: "address_match", False: "address_mismatch",
                                            None: "address_unknown"}[same]])
                self.step(f"Delivery address {'matches' if same else 'differs from' if same is False else 'unknown vs'} "
                          f"the shipping address", {"evidence_id": row.id, "address_match": same})

    def _messages(self, order: Order) -> None:
        msgs = self.s.exec(select(Message).where(Message.order_id == order.id).order_by(Message.sent_at)).all()
        buyer = [m for m in msgs if m.sender == MessageSender.buyer]
        for m in buyer:
            self.evidence(type="buyer_message", source="store", source_ref=f"store:message/{m.id}",
                          summary=f"Buyer wrote on {_day(m.sent_at)}: \"{m.body}\"")
        self.step(f"Found {len(buyer)} buyer message(s) and {len(msgs) - len(buyer)} seller message(s)"
                  f" for order #{order.id}", {"message_ids": [m.id for m in msgs]})

    def _listing(self, order: Order) -> None:
        if not order.product_listing:
            self.step(f"No product listing stored for order #{order.id}")
            return
        row = self.evidence(type="listing", source="store", source_ref=f"store:order/{order.id}/listing",
                            summary=f"Listing at time of sale: \"{order.product_listing}\"")
        self.step("Found the product listing shown at time of sale", {"evidence_id": row.id})

    def _refund_policy(self) -> None:
        from app.merchant import get_merchant_settings

        policy = get_merchant_settings(self.s).refund_policy
        if not policy:
            self.step("No refund policy on file (add it in Settings)")
            return
        row = self.evidence(type="refund_policy", source="store", source_ref="store:settings/refund_policy",
                            summary=f"Store refund policy: \"{policy[:600]}\"")
        self.step("Found the store's refund policy", {"evidence_id": row.id})

    def _buyer_history(self, order: Order) -> None:
        if not order.buyer_email:
            self.step("Buyer email unknown; skipped order history")
            return
        prior = self.s.exec(select(Order).where(Order.buyer_email == order.buyer_email, Order.id != order.id,
                                                Order.created_at < order.created_at)
                            .order_by(Order.created_at)).all()
        prior_ids = [o.id for o in prior]
        disputes = self.s.exec(select(Dispute).where(Dispute.order_id.in_(prior_ids))).all() if prior_ids else []
        summary = (f"Buyer {order.buyer_email} has {len(prior)} earlier order(s) with this store"
                   + (f" (#{', #'.join(map(str, prior_ids))})" if prior_ids else "")
                   + f" and {len(disputes)} previous dispute(s)")
        row = self.evidence(type="buyer_history", source="store", source_ref=f"store:buyer/{order.buyer_email}",
                            summary=summary, flags=["buyer_prior_disputes"] if disputes else [])
        self.step(f"Buyer has {len(prior)} prior order(s), {len(disputes)} prior dispute(s)", {"evidence_id": row.id})


async def investigate(session: Session, dispute: Dispute, client: PayPalClient | None = None) -> CaseFile:
    return await Investigator(session, dispute, client).run()

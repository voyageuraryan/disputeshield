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
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlmodel import Session, select

from app.tracing import observe
from app.agents.steps import log_step
from app.models import Dispute, EvidenceItemRow, EvidenceSource, Message, MessageSender, Order, Shipment
from app.paypal.client import PayPalClient, PayPalError
from app.paypal.disputes import allowed_actions, get_dispute, offer_types
from app.paypal.orders import get_order
from app.paypal.payments import get_capture
from app.paypal.tracking import trackers_from_order
from app.schemas import CaseFile, EvidenceItem
from app.troubleshooting.eligibility import dispute_eligibility

log = logging.getLogger(__name__)
AGENT = "investigator"
FRESH_S = 60  # reuse a dispute fetched this recently instead of fetching it again


# ---- formatting helpers -------------------------------------------------------

def _carrier_guess(number: str | None) -> str | None:
    """PayPal's order trackers don't carry the carrier; when an order is rebuilt, name it from the number's
    well-known shape (UPS 1Z..., USPS 20-22 digits starting 9, FedEx 12 or 15 digits), else leave it unknown."""
    n = (number or "").replace(" ", "").upper()
    if n.startswith("1Z") and len(n) == 18:
        return "UPS"
    if n.isdigit() and n.startswith("9") and 20 <= len(n) <= 22:
        return "USPS"
    if n.isdigit() and len(n) in (12, 15):
        return "FedEx"
    return None

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
                     summary: str, flags: list[str] | None = None, run_key: str | None = None,
                     file_path: str | None = None) -> EvidenceItemRow:
    row = session.exec(select(EvidenceItemRow).where(
        EvidenceItemRow.dispute_id == dispute.id, EvidenceItemRow.source_ref == source_ref)).first()
    if row is None:
        row = EvidenceItemRow(dispute_id=dispute.id, type=type, source=EvidenceSource(source),
                              source_ref=source_ref, summary=summary, flags=flags or [])
    else:
        row.type, row.summary, row.flags = type, summary, flags or []  # fresh flags: no "stale:" left over
    row.file_path = file_path
    row.last_seen_run, row.last_seen_at, row.active = run_key, datetime.now(timezone.utc), True
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


STALE = "stale:"


def source_of(source_ref: str) -> str:
    """The lookup an evidence item comes from (a failed lookup keeps its own items, flagged stale)."""
    if source_ref.startswith("paypal:capture/"):
        return "paypal:capture"
    if source_ref.startswith("paypal:order/") and "/tracker/" in source_ref:
        return "paypal:trackers"
    if source_ref.startswith("paypal:dispute/"):
        return "paypal:dispute"
    return source_ref.split("/", 1)[0]


def is_stale(item) -> bool:  # noqa: ANN001  (EvidenceItem or EvidenceItemRow)
    return any(f.startswith(STALE) for f in item.flags or [])


def transient(exc: Exception) -> bool:
    """A lookup that may work next time (keep the previous data, flagged stale) vs data that is really gone."""
    if isinstance(exc, httpx.HTTPError):
        return True  # timeout / connection error (the client raises these after its retries)
    return isinstance(exc, PayPalError) and (exc.status_code >= 500 or exc.status_code == 429)


def build_case_file(session: Session, dispute: Dispute) -> CaseFile:
    """Only ACTIVE evidence (Phase 11): what the latest investigation saw, plus stale items of failed lookups."""
    rows = session.exec(select(EvidenceItemRow).where(EvidenceItemRow.dispute_id == dispute.id,
                                                      EvidenceItemRow.active.is_not(False))  # None = pre-Phase 11
                        .order_by(EvidenceItemRow.id)).all()
    stale = sorted({source_of(r.source_ref) for r in rows if is_stale(r)})
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
        offer_types=offer_types(dispute.raw),
        evidence=[EvidenceItem(id=r.id, type=r.type, source=r.source.value, source_ref=r.source_ref,
                               summary=r.summary, flags=r.flags or []) for r in rows],
        data_as_of=dispute.refreshed_at, stale_sources=stale,
    )


# ---- the investigation ---------------------------------------------------------

class Investigator:
    """run_key: stamped on every evidence item written (Phase 11); fresh: always fetch the dispute from PayPal
    (manual re-analyse), instead of reusing a copy fetched in the last FRESH_S seconds (webhook/poller runs)."""

    def __init__(self, session: Session, dispute: Dispute, client: PayPalClient | None = None, *,
                 run_key: str | None = None, fresh: bool = False):
        self.s, self.d, self.client = session, dispute, client
        self.run_key = run_key or f"adhoc:{uuid.uuid4().hex[:12]}"
        self.fresh = fresh
        self.failed: set[str] = set()  # sources whose lookup failed transiently this run

    def step(self, action: str, detail: dict | None = None) -> None:
        log_step(self.s, self.d.id, AGENT, action, detail)

    def evidence(self, **kw: Any) -> EvidenceItemRow:
        return _upsert_evidence(self.s, self.d, run_key=self.run_key, **kw)

    def _retire(self) -> None:
        """Items this run didn't see: inactive (kept for history), unless their source failed transiently, in which
        case they stay active flagged stale:<time of last success>. Logs what dropped out and what is stale."""
        rows = self.s.exec(select(EvidenceItemRow).where(EvidenceItemRow.dispute_id == self.d.id,
                                                         EvidenceItemRow.active.is_not(False))).all()
        dropped, stale = [], []
        for r in rows:
            if r.last_seen_run == self.run_key:
                continue
            if source_of(r.source_ref) in self.failed:
                when = r.last_seen_at
                stamp = (when.replace(tzinfo=when.tzinfo or timezone.utc).isoformat() if when else "unknown")
                r.flags = [f for f in r.flags or [] if not f.startswith(STALE)] + [f"{STALE}{stamp}"]
                stale.append(r)
            else:
                r.active = False
                dropped.append(r)
            self.s.add(r)
        self.s.commit()
        if dropped:
            self.step("No longer found this run (kept for history, not used): "
                      + "; ".join(f"{r.type}: {r.summary[:90]}" for r in dropped),
                      {"evidence_ids": [r.id for r in dropped]})
        if stale:
            self.step("Couldn't refresh " + ", ".join(sorted(self.failed)) + "; using the last good copy (stale): "
                      + "; ".join(f"{r.type}: {r.summary[:60]}" for r in stale),
                      {"evidence_ids": [r.id for r in stale], "stale_sources": sorted(self.failed)})

    async def run(self) -> CaseFile:
        d = self.d
        pp = await self._paypal_dispute()
        capture_id = d.transaction_id
        order = self.s.get(Order, d.order_id) if d.order_id else None

        cap = await self._capture(capture_id)
        if order is None and cap:
            order = await self._recover_order(cap)
        await self._paypal_trackers(order)
        if order is None:
            self.step("No store order matched this dispute", {"transaction_id": capture_id})
        else:
            self._order(order)
            self._shipment(order)
            self._messages(order)
            self._listing(order)
            self._buyer_history(order)
            self._prior_undisputed(order)
        self._troubleshooting_eligibility(order)
        self._troubleshooting_record()
        self._refund_policy()
        self._paypal_dispute_messages(pp)
        self._merchant_uploads()
        self._retire()
        case = build_case_file(self.s, d)
        self.step(f"Case file ready: {len(case.evidence)} evidence items",
                  {"evidence_ids": [e.id for e in case.evidence]})
        return case

    # PayPal lookups (failures are logged and the investigation continues)

    async def _paypal_dispute(self) -> dict:
        d = self.d
        fetched = d.refreshed_at.replace(tzinfo=d.refreshed_at.tzinfo or timezone.utc) if d.refreshed_at else None
        if not self.fresh and fetched and d.raw and (datetime.now(timezone.utc) - fetched).total_seconds() < FRESH_S:
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
            # Phase 11: one full refresh (status, stage, reason, amount, due date, transaction id, raw; order
            # re-matched when it wasn't before), the same function ingest uses.
            from app.ingest import upsert_dispute

            was_unmatched = d.order_id is None
            upsert_dispute(self.s, fresh)
            self.s.commit()
            self.s.refresh(d)
            raw = d.raw or {}
            if was_unmatched and d.order_id:
                self.step(f"Matched the dispute to store order #{d.order_id} on this refresh", {"order_id": d.order_id})
        except (PayPalError, httpx.HTTPError) as exc:
            raw = d.raw or {}
            # every PayPal-side source depends on this copy: keep their previous items, flagged stale
            self.failed |= {"paypal:dispute", "paypal:capture", "paypal:trackers"} if transient(exc) else set()
            self.step("Could not refresh the dispute from PayPal; using the last stored copy"
                      + (f" (fetched {_day(d.refreshed_at)})" if d.refreshed_at else ""),
                      {"debug_id": getattr(exc, "debug_id", None), "status": getattr(exc, "status_code", None),
                       "error": type(exc).__name__, "stale": True})
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

    async def _capture(self, capture_id: str | None) -> dict | None:
        if not capture_id:
            self.step("No disputed transaction id on the dispute")
            return None
        try:
            cap = await get_capture(capture_id, client=self.client)
        except (PayPalError, ValueError, httpx.HTTPError) as exc:
            if transient(exc):
                self.failed.add("paypal:capture")  # keep the last good copy, flagged stale
            self.step(f"Could not fetch transaction {capture_id} from PayPal"
                      + (" (temporary error: keeping the last good copy, marked stale)" if transient(exc) else ""),
                      {"error": str(exc)[:300], "debug_id": getattr(exc, "debug_id", None)})
            return None
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
        return cap

    async def _recover_order(self, cap: dict) -> Order | None:
        """The store order row is missing (e.g. Render's free plan starts with an empty database after a sleep or
        deploy), but the disputed capture points at a PayPal order this store created (invoice DS-...). Rebuild the
        order row from PayPal's copy (items, amount, shipping address, payer) so tracking and the order itself
        count as evidence again. Merchant-recorded delivery isn't on PayPal, so it can't come back."""
        from app.catalog import PRODUCTS, order_item
        from app.signals import signal_hash

        pp_order_id = (((cap.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id"))
        if not pp_order_id or not str(pp_order_id).isalnum():
            return None
        existing = self.s.exec(select(Order).where(Order.paypal_order_id == pp_order_id)).first()
        if existing is None:
            try:
                pp = await get_order(pp_order_id, client=self.client)
            except (PayPalError, ValueError, httpx.HTTPError) as exc:
                self.step(f"Could not fetch PayPal order {pp_order_id} to rebuild the store order",
                          {"error": str(exc)[:300], "debug_id": getattr(exc, "debug_id", None)})
                return None
            pu = (pp.get("purchase_units") or [{}])[0]
            invoice = pu.get("invoice_id") or cap.get("invoice_id")
            if not (invoice or "").startswith("DS-"):
                return None  # not an order this store created: never import someone else's data
            if self.s.exec(select(Order).where(Order.invoice_id == invoice)).first():
                return None
            items = []
            for it in pu.get("items") or []:
                qty = int(it.get("quantity") or 1)
                product = PRODUCTS.get(it.get("sku") or "")
                items.append(order_item(product, qty) if product else
                             {"sku": it.get("sku"), "name": it.get("name"), "quantity": qty,
                              "unit_amount": (it.get("unit_amount") or {}).get("value"),
                              "paypal_category": it.get("category")})
            amount = (pu.get("amount") or cap.get("amount") or {})
            listing = next((PRODUCTS[i["sku"]].listing for i in items if i.get("sku") in PRODUCTS), None)
            created = pp.get("create_time") or cap.get("create_time")
            try:
                created_at = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
            except ValueError:
                created_at = None
            payer = pp.get("payer") or {}
            existing = Order(paypal_order_id=pp_order_id, capture_id=cap.get("id"), invoice_id=invoice,
                             buyer_email=payer.get("email_address"), items=items,
                             amount=amount.get("value") or "0", currency=amount.get("currency_code") or "USD",
                             shipping_address=(pu.get("shipping") or {}).get("address"), product_listing=listing,
                             payer_hash=signal_hash("payer", payer.get("payer_id")),
                             **({"created_at": created_at} if created_at else {}))
            self.s.add(existing)
            self.s.commit()
            self.s.refresh(existing)
            for t in trackers_from_order(pp):  # so the merchant can record delivery on them again
                if t["tracking_number"] and t["status"] != "CANCELLED":
                    self.s.add(Shipment(order_id=existing.id, tracking_number=t["tracking_number"], status="SHIPPED",
                                        carrier=_carrier_guess(t["tracking_number"])))
            self.s.commit()
            self.step(f"Rebuilt store order #{existing.id} from PayPal order {pp_order_id} (invoice {invoice}); "
                      "the local copy was missing. Delivery you recorded earlier isn't stored at PayPal: record it "
                      "again if you have it", {"order_id": existing.id, "source_ref": f"paypal:order/{pp_order_id}"})
        self.d.order_id = existing.id
        self.s.add(self.d)
        self.s.commit()
        return existing

    async def _paypal_trackers(self, order: Order | None) -> None:
        if order is None or not order.paypal_order_id:
            self.step("No PayPal order to check for tracking")
            return
        try:
            pp_order = await get_order(order.paypal_order_id, client=self.client)
        except (PayPalError, ValueError, httpx.HTTPError) as exc:
            if transient(exc):
                self.failed.add("paypal:trackers")
            self.step(f"Could not fetch PayPal order {order.paypal_order_id} for tracking"
                      + (" (temporary error: keeping the last good copy, marked stale)" if transient(exc) else ""),
                      {"error": str(exc)[:300], "debug_id": getattr(exc, "debug_id", None)})
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
        # read from the stored copy because the refresh failed: still stale, as of that copy's fetch time
        at = self.d.refreshed_at
        flags = ([f"{STALE}{at.replace(tzinfo=at.tzinfo or timezone.utc).isoformat() if at else 'unknown'}"]
                 if "paypal:dispute" in self.failed else [])
        for i, m in enumerate(msgs):
            row = self.evidence(type="buyer_message", source="paypal",
                                source_ref=f"paypal:dispute/{self.d.paypal_dispute_id}/message/{i}",
                                summary=f"Buyer wrote in the PayPal case on {_day(m.get('time_posted'))}: "
                                        f"\"{(m.get('content') or '').strip()}\"", flags=flags)
            self.step(f"Found buyer message in PayPal case ({_day(m.get('time_posted'))})", {"evidence_id": row.id})

    def _merchant_uploads(self) -> None:
        """Phase 12: evidence the merchant added and confirmed (summary + flags from their confirmed fields)."""
        from app.merchant_uploads import confirmed

        for up in confirmed(self.s, self.d.id):
            row = self.evidence(type=up.evidence_type, source="merchant_upload", source_ref=f"upload:{up.id}",
                                summary=up.summary or "Merchant-provided evidence", flags=list(up.flags or []),
                                file_path=up.file_path)
            self.step(f"Using evidence the merchant added: {up.evidence_type.replace('_', ' ')}",
                      {"evidence_id": row.id, "upload_id": up.id})

    # Store lookups

    @observe(type="tool", name="store_order")
    def _order(self, order: Order) -> None:
        items = ", ".join(f"{i.get('quantity', 1)}x {i.get('name')}" for i in order.items or []) or "no items"
        summary = (f"Store order #{order.id} on {_day(order.created_at)}: {items}, {order.amount} {order.currency}; "
                   f"buyer {order.buyer_email or 'unknown'}; ship to {_addr(order.shipping_address)}")
        row = self.evidence(type="order", source="store", source_ref=f"store:order/{order.id}", summary=summary)
        self.step(f"Found order #{order.id} ({items}, {order.amount} {order.currency})", {"evidence_id": row.id})

    @observe(type="tool", name="store_shipments")
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
                by = (" (recorded by the merchant from the carrier's confirmation)"
                      if sh.delivery_recorded_by == "merchant" else "")
                row = self.evidence(type="delivery_confirmation", source="store",
                                    source_ref=f"store:shipment/{sh.id}/delivery",
                                    summary=f"Carrier delivered {_day(sh.delivered_at)} to {where}; {match}{by}",
                                    flags=[{True: "address_match", False: "address_mismatch",
                                            None: "address_unknown"}[same]])
                self.step(f"Delivery address {'matches' if same else 'differs from' if same is False else 'unknown vs'} "
                          f"the shipping address", {"evidence_id": row.id, "address_match": same})

    @observe(type="tool", name="store_messages")
    def _messages(self, order: Order) -> None:
        msgs = self.s.exec(select(Message).where(Message.order_id == order.id).order_by(Message.sent_at)).all()
        buyer = [m for m in msgs if m.sender == MessageSender.buyer]
        for m in buyer:
            self.evidence(type="buyer_message", source="store", source_ref=f"store:message/{m.id}",
                          summary=f"Buyer wrote on {_day(m.sent_at)}: \"{m.body}\"")
        self.step(f"Found {len(buyer)} buyer message(s) and {len(msgs) - len(buyer)} seller message(s)"
                  f" for order #{order.id}", {"message_ids": [m.id for m in msgs]})

    @observe(type="tool", name="store_listing")
    def _listing(self, order: Order) -> None:
        if not order.product_listing:
            self.step(f"No product listing stored for order #{order.id}")
            return
        row = self.evidence(type="listing", source="store", source_ref=f"store:order/{order.id}/listing",
                            summary=f"Listing at time of sale: \"{order.product_listing}\"")
        self.step("Found the product listing shown at time of sale", {"evidence_id": row.id})

    def _troubleshooting_eligibility(self, order: Order) -> None:
        elig = dispute_eligibility(self.d, order)
        log_step(self.s, self.d.id, "troubleshooter", elig.why,
                 {"eligible": elig.eligible, "product": elig.product_name, "model": elig.product_model})

    @observe(type="tool", name="troubleshooting_record")
    def _troubleshooting_record(self) -> None:
        """Phase 9D: a finished buyer troubleshooting session becomes one evidence item (facts + flags)."""
        from app.troubleshooting.outcome import record, session_of

        ts = session_of(self.s, self.d.id)
        rec = record(self.s, ts) if ts else None
        if rec is None:
            if ts is not None:
                self.step(f"Buyer troubleshooting still in progress ({ts.status.value.lower().replace('_', ' ')}); "
                          "not used as evidence yet")
            return
        row = self.evidence(type="troubleshooting_record", source="buyer_portal", source_ref=f"ts:session/{ts.id}",
                            summary=rec.summary, flags=rec.flags)
        outcome_flag = rec.flags[0]
        self.step(f"Found the buyer's troubleshooting result: {outcome_flag.removeprefix('ts_').replace('_', ' ')}",
                  {"evidence_id": row.id, "flags": rec.flags})
        if rec.suspect:  # merchant only: never a penalty, never sent to the LLM
            log_step(self.s, self.d.id, "troubleshooter",
                     "For you to check: " + "; ".join(rec.suspect), {"merchant_only": True, "evidence_id": row.id})

    @observe(type="tool", name="refund_policy")
    def _refund_policy(self) -> None:
        from app.merchant import get_merchant_settings

        policy = get_merchant_settings(self.s).refund_policy
        if not policy:
            self.step("No refund policy on file (add it in Settings)")
            return
        row = self.evidence(type="refund_policy", source="store", source_ref="store:settings/refund_policy",
                            summary=f"Store refund policy: \"{policy[:600]}\"")
        self.step("Found the store's refund policy", {"evidence_id": row.id})

    @observe(type="tool", name="buyer_history")
    def _buyer_history(self, order: Order) -> None:
        if not order.buyer_email:
            self.step("Buyer email unknown; skipped order history")
            return
        prior = self.s.exec(select(Order).where(Order.buyer_email == order.buyer_email, Order.id != order.id,
                                                Order.created_at < order.created_at)
                            .order_by(Order.created_at)).all()
        prior_ids = [o.id for o in prior]
        disputes = self.s.exec(select(Dispute).where(Dispute.order_id.in_(prior_ids))).all() if prior_ids else []
        from app.signals import SERIAL_MIN, serial_disputes

        serial = serial_disputes(self.s, order, self.d)  # Phase 15: same buyer (email / payer id), 12 months
        summary = (f"Buyer {order.buyer_email} has {len(prior)} earlier order(s) with this store"
                   + (f" (#{', #'.join(map(str, prior_ids))})" if prior_ids else "")
                   + f" and {len(disputes)} previous dispute(s)"
                   + (f"; {len(serial)} other dispute(s) by this buyer in the last 12 months"
                      if len(serial) >= SERIAL_MIN else ""))  # neutral wording: this text can reach PayPal (PDF)
        flags = (["buyer_prior_disputes"] if disputes else []) + (["buyer_serial_disputes"]
                                                                  if len(serial) >= SERIAL_MIN else [])
        row = self.evidence(type="buyer_history", source="store", source_ref=f"store:buyer/{order.buyer_email}",
                            summary=summary, flags=flags)
        self.step(f"Buyer has {len(prior)} prior order(s), {len(disputes)} prior dispute(s)"
                  + (f", {len(serial)} other dispute(s) in 12 months" if serial else ""),
                  {"evidence_id": row.id, "serial_dispute_ids": serial})

    def _prior_undisputed(self, order: Order) -> None:
        """Phase 15: earlier undisputed orders sharing device / IP / address / account (Visa CE 3.0-LIKE, not CE)."""
        from app.signals import prior_undisputed

        hist = prior_undisputed(self.s, order, self.d)
        if not hist.priors:
            self.step("No earlier paid, undisputed orders by this buyer to compare with")
            return
        row = self.evidence(type="prior_undisputed_history", source="store",
                            source_ref=f"store:signals/order/{order.id}", summary=hist.summary, flags=hist.flags)
        self.step(("Earlier undisputed orders match this one like Visa CE 3.0 evidence (similar, not compliance)"
                   if "ce3_like_match" in hist.flags else "Earlier undisputed orders partly match this one")
                  + f": {', '.join(hist.flags) or 'account only'}",
                  {"evidence_id": row.id, "priors": hist.priors})  # ids, day gaps and match kinds; never hashes


@observe(type="agent")
async def investigate(session: Session, dispute: Dispute, client: PayPalClient | None = None, *,
                      run_key: str | None = None, fresh: bool = False) -> CaseFile:
    return await Investigator(session, dispute, client, run_key=run_key, fresh=fresh).run()

"""Dispute read API for the merchant UI: queue, case detail, case file, evidence sources."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.engine import Engine
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agents.investigator import build_case_file
from app.catalog import digital_only
from app.api.decisions import DecisionOut, _out
from app.agents.runner import run_agents
from app.db import get_engine, get_session
from app.models import (AgentStep, Decision, DecisionStatus, Dispute, EvidenceItemRow, MerchantUpload, Message, Order,
                        Shipment, TroubleshootingSession, UploadStatus)
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.paypal.orders import get_order
from app.paypal.payments import get_capture
from app.paypal.tracking import trackers_from_order
from app.rules.coach import as_dicts as coach_dicts
from app.rules.coach import coach
from app.rules.evidence_rules import RULES, defect_confirmed, economic_baseline, evaluate, red_flag_description, rule_for
from app.merchant import settle_threshold
from app.schemas import CaseFile

router = APIRouter(tags=["disputes"])


class DecisionBrief(BaseModel):
    id: int
    recommendation: str
    confidence: str
    status: str


class DisputeRow(BaseModel):
    id: int
    paypal_dispute_id: str
    reason: str
    stage: str | None
    status: str
    amount: Decimal
    currency: str
    seller_response_due: datetime | None
    agent_status: str | None
    order_id: int | None
    buyer_email: str | None
    updated_at: str | None
    latest_decision: DecisionBrief | None
    troubleshooting: str | None = None  # buyer troubleshooting session status (Phase 9D), if there is one
    opened_at: str | None = None        # Phase 18: PayPal create_time (Deadlines view)
    responded_at: datetime | None = None  # first response sent to PayPal (an executed decision)


class StepOut(BaseModel):
    id: int
    agent: str
    action: str
    detail: dict | None
    created_at: datetime


class ShipmentOut(BaseModel):
    id: int
    carrier: str | None
    tracking_number: str | None
    status: str | None
    delivered_at: datetime | None
    delivered_address: dict | None
    delivery_recorded_by: str | None = None


class OrderOut(BaseModel):
    id: int
    created_at: datetime
    buyer_email: str | None
    amount: Decimal
    currency: str
    items: list[dict] | None
    shipping_address: dict | None
    product_listing: str | None
    paypal_order_id: str | None
    capture_id: str | None
    invoice_id: str | None
    digital_only: bool = False
    shipments: list[ShipmentOut]


class ChecklistItem(BaseModel):
    type: str
    required: bool
    present: bool
    evidence_ids: list[int]


class Checklist(BaseModel):
    reason_label: str | None
    supported: bool
    rule_score: float
    items: list[ChecklistItem]
    red_flags: list[dict]  # {"code", "description"}
    baseline: str | None
    baseline_why: str | None
    coach: list[dict] = []   # Phase 12: "What would win this?" (rules/coach.py)


class DisputeDetail(BaseModel):
    settle_threshold: Decimal  # current merchant setting (decisions record the one they used)
    dispute: DisputeRow
    order: OrderOut | None
    case: CaseFile
    checklist: Checklist
    decisions: list[DecisionOut]
    timeline: list[StepOut]
    uploads: list[dict] = []          # Phase 12: the merchant's uploads (pending and confirmed)
    offer_bounds: dict | None = None  # Phase 13: {min, max} for a partial offer when PayPal lists REFUND offers
    upload_types: list[str] = []      # evidence types the merchant can add


def _latest(session: Session, dispute_id: int) -> Decision | None:
    return session.exec(select(Decision).where(Decision.dispute_id == dispute_id)
                        .order_by(Decision.id.desc())).first()


def _row(session: Session, d: Dispute) -> DisputeRow:
    dec = _latest(session, d.id)
    order = session.get(Order, d.order_id) if d.order_id else None
    return DisputeRow(
        id=d.id, paypal_dispute_id=d.paypal_dispute_id, reason=d.reason, stage=d.stage, status=d.status,
        amount=d.amount, currency=d.currency, seller_response_due=d.seller_response_due,
        agent_status=d.agent_status, order_id=d.order_id, buyer_email=order.buyer_email if order else None,
        updated_at=(d.raw or {}).get("update_time"),
        latest_decision=DecisionBrief(id=dec.id, recommendation=dec.recommendation, confidence=dec.confidence,
                                      status=dec.status.value) if dec else None,
        troubleshooting=ts.status.value if (ts := session.exec(select(TroubleshootingSession).where(
            TroubleshootingSession.dispute_id == d.id)).first()) else None,
        opened_at=(d.raw or {}).get("create_time"),
        responded_at=session.exec(select(Decision.executed_at).where(
            Decision.dispute_id == d.id, Decision.status == DecisionStatus.EXECUTED)
            .order_by(Decision.executed_at)).first())


def _find(session: Session, dispute_id: str) -> Dispute:
    d = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == dispute_id)).first()
    if d is None:
        raise HTTPException(404, "unknown dispute")
    return d


def _order(session: Session, order_id: int | None) -> OrderOut | None:
    o = session.get(Order, order_id) if order_id else None
    return order_out(session, o) if o else None


def order_out(session: Session, o: Order) -> OrderOut:
    ships = session.exec(select(Shipment).where(Shipment.order_id == o.id).order_by(Shipment.id)).all()
    return OrderOut(**o.model_dump(include=set(OrderOut.model_fields) - {"shipments", "digital_only"}),
                    digital_only=digital_only(o.items),
                    shipments=[ShipmentOut(**s.model_dump(include=set(ShipmentOut.model_fields))) for s in ships])


def _checklist(case: CaseFile, threshold, carrier: str | None = None) -> Checklist:
    result = evaluate(case)
    rule = rule_for(case)
    by_type: dict[str, list[int]] = {}
    for e in case.evidence:
        by_type.setdefault(e.type, []).append(e.id)
    items = []
    if rule:
        items = [ChecklistItem(type=t, required=t in rule.required, present=t in by_type, evidence_ids=by_type.get(t, []))
                 for t in rule.required + rule.supporting]
    base = None
    if result.reason_supported:
        base = economic_baseline(result, case.amount, threshold, can_fight=case.can_fight,
                                 can_settle=case.can_settle, defect=defect_confirmed(case))
    return Checklist(reason_label=rule.label if rule else None, supported=result.reason_supported,
                     rule_score=result.rule_score, items=items,
                     red_flags=[{"code": c, "description": red_flag_description(case.reason, c)} for c in result.red_flags],
                     baseline=base.action if base else None, baseline_why=base.why if base else None,
                     coach=coach_dicts(coach(case, threshold, carrier=carrier)))


class ReanalyzeOut(BaseModel):
    queued: bool
    run_key: str


@router.post("/api/disputes/{dispute_id}/reanalyze", response_model=ReanalyzeOut, status_code=202)
def reanalyze(dispute_id: str, background: BackgroundTasks, session: Session = Depends(get_session),
              engine: Engine = Depends(get_engine)) -> ReanalyzeOut:
    """Run the agents again on the current evidence and settings (e.g. after changing the settle threshold).
    The new proposal supersedes the old PROPOSED one; nothing is sent to PayPal without approval."""
    d = _find(session, dispute_id)
    key = start_fresh_run(session, d, background, engine, "Asked the agents to re-analyse the case")
    return ReanalyzeOut(queued=True, run_key=key)


def check_can_change(session: Session, d: Dispute) -> None:
    """409 when the case can't take new analysis or evidence (resolved, or a response is being sent)."""
    if d.status == "RESOLVED":
        raise HTTPException(409, "the dispute is resolved; there is nothing to analyse")
    pending = session.exec(select(Decision).where(Decision.dispute_id == d.id,
                                                  Decision.status == DecisionStatus.APPROVED)).first()
    if pending:
        raise HTTPException(409, f"decision #{pending.id} is being sent to PayPal; wait for it to finish")


def start_fresh_run(session: Session, d: Dispute, background: BackgroundTasks, engine: Engine, why: str) -> str:
    """Queue a fresh agent run (Phase 11: re-fetches everything); its proposal supersedes the PROPOSED one.
    409 if the case can't change or a run is already going."""
    check_can_change(session, d)
    from app.agents import runner

    # Phase 11: one run at a time (double clicks, a webhook run already going): claimed before it's scheduled
    if runner.in_progress(session, d.id) or not runner.claim(d.id):
        raise HTTPException(409, "The agents are already analysing this case; wait for that run to finish.")
    key = f"run:{d.paypal_dispute_id}:manual:{datetime.now(timezone.utc).isoformat()}"
    session.add(AgentStep(dispute_id=d.id, agent="merchant", action=why))
    session.commit()
    # fresh: everything is fetched from PayPal now, even if a webhook fetched it seconds ago
    background.add_task(run_agents, d.id, key, engine, fresh=True, claimed=True)
    return key


@router.get("/api/disputes", response_model=list[DisputeRow])
def list_disputes(session: Session = Depends(get_session)) -> list[DisputeRow]:
    """The merchant's queue: open disputes first, then by response deadline."""
    rows = [_row(session, d) for d in session.exec(select(Dispute)).all()]
    far = datetime.max
    rows.sort(key=lambda r: (r.status == "RESOLVED", (r.seller_response_due or far).replace(tzinfo=None), -r.id))
    return rows


@router.get("/api/disputes/{dispute_id}", response_model=DisputeDetail)
def get_dispute_detail(dispute_id: str, session: Session = Depends(get_session)) -> DisputeDetail:
    d = _find(session, dispute_id)
    # first: on a fresh database this creates the settings row and commits, which would expire rows loaded before it
    threshold = settle_threshold(session)
    case = build_case_file(session, d)
    decisions = session.exec(select(Decision).where(Decision.dispute_id == d.id).order_by(Decision.id.desc())).all()
    steps = session.exec(select(AgentStep).where(AgentStep.dispute_id == d.id).order_by(AgentStep.id)).all()
    from app.api.uploads import upload_out
    from app.merchant_uploads import UPLOAD_TYPES

    ship = session.exec(select(Shipment).where(Shipment.order_id == d.order_id, Shipment.carrier.is_not(None))
                        ).first() if d.order_id else None
    ups = session.exec(select(MerchantUpload).where(MerchantUpload.dispute_id == d.id,
                                                    MerchantUpload.status != UploadStatus.DISCARDED)
                       .order_by(MerchantUpload.id)).all()
    return DisputeDetail(settle_threshold=threshold,
                         dispute=_row(session, d), order=_order(session, d.order_id), case=case,
                         checklist=_checklist(case, threshold, ship.carrier if ship else None),
                         decisions=[_out(x, session) for x in decisions],
                         timeline=[StepOut(**s.model_dump(include=set(StepOut.model_fields))) for s in steps],
                         uploads=[upload_out(u).model_dump(mode="json") for u in ups], upload_types=list(UPLOAD_TYPES),
                         offer_bounds=_offer_bounds(session, case))


def _offer_bounds(session: Session, case: CaseFile) -> dict | None:
    if not case.can_offer:
        return None
    from app.merchant import money_settings
    from app.rules.money import offer_bounds

    lo, hi = offer_bounds(case.amount, money_settings(session))
    return {"min": str(lo), "max": str(hi)}


# Served at the spec's path (/disputes/...) and under /api for the frontend's Vite proxy.
@router.get("/disputes/{dispute_id}/case", response_model=CaseFile)
@router.get("/api/disputes/{dispute_id}/case", response_model=CaseFile, include_in_schema=False)
def get_case(dispute_id: str, session: Session = Depends(get_session)) -> CaseFile:
    """Case file for a dispute, by PayPal dispute id (e.g. PP-R-ZWL-10195422)."""
    return build_case_file(session, _find(session, dispute_id))


# ---- evidence source records ---------------------------------------------------------------

class SourceOut(BaseModel):
    evidence_id: int
    source: str
    source_ref: str
    title: str
    origin: str  # "PayPal (live)" / "PayPal (stored copy)" / "Store database"
    record: Any


def _dump(obj) -> dict:
    return obj.model_dump(mode="json")


@router.get("/api/evidence/{evidence_id}/source", response_model=SourceOut)
async def evidence_source(evidence_id: int, session: Session = Depends(get_session),
                          paypal: PayPalClient = Depends(get_paypal_client)) -> SourceOut:
    """The record an evidence item was taken from, so the merchant can check it."""
    ev = session.get(EvidenceItemRow, evidence_id)
    if ev is None:
        raise HTTPException(404, "unknown evidence item")
    ref = ev.source_ref
    kind, _, path = ref.partition(":")
    parts = path.split("/")
    out = lambda title, origin, record: SourceOut(evidence_id=ev.id, source=kind, source_ref=ref,  # noqa: E731
                                                  title=title, origin=origin, record=record)
    try:
        if kind == "store":
            if parts[0] == "order" and len(parts) == 2:
                return out(f"Store order #{parts[1]}", "Store database", _dump(session.get(Order, int(parts[1]))))
            if parts[0] == "order" and parts[-1] == "listing":
                o = session.get(Order, int(parts[1]))
                return out(f"Listing for order #{o.id}", "Store database", {"product_listing": o.product_listing,
                                                                             "items": o.items})
            if parts[0] == "shipment":
                return out(f"Shipment #{parts[1]}", "Store database", _dump(session.get(Shipment, int(parts[1]))))
            if parts[0] == "message":
                return out(f"Message #{parts[1]}", "Store database", _dump(session.get(Message, int(parts[1]))))
            if parts[0] == "settings" and parts[-1] == "refund_policy":
                from app.merchant import get_merchant_settings
                return out("Store refund policy", "Store settings",
                           {"refund_policy": get_merchant_settings(session).refund_policy})
            if parts[0] == "buyer":
                email = path.split("/", 1)[1]
                orders = session.exec(select(Order).where(Order.buyer_email == email).order_by(Order.created_at)).all()
                return out(f"Orders by {email}", "Store database",
                           [{"id": o.id, "created_at": o.created_at.isoformat(), "amount": str(o.amount),
                             "items": o.items} for o in orders])
        if kind == "ts" and parts[0] == "session":  # Phase 9D: the buyer troubleshooting record
            from app.models import TSProof, TSStep

            ts = session.get(TroubleshootingSession, int(parts[1]))
            if ts is not None and ts.dispute_id == ev.dispute_id:
                steps = session.exec(select(TSStep).where(TSStep.session_id == ts.id).order_by(TSStep.ordinal)).all()
                proofs = session.exec(select(TSProof).where(TSProof.session_id == ts.id).order_by(TSProof.id)).all()
                return out(f"Buyer troubleshooting session #{ts.id}", "Buyer troubleshooting page", {
                    # never the link token / device hashes
                    **ts.model_dump(include={"status", "product_name", "product_model", "invited_at", "invited_via",
                                             "buyer_last_seen_at", "finished_at", "ended_reason", "symptom",
                                             "language", "plan_generic"}, mode="json"),
                    "steps": [{"ordinal": s.ordinal, "title": s.title, "status": s.status, "buyer_note": s.buyer_note}
                              for s in steps],
                    "proofs": [{"id": p.id, "step_id": p.step_id, "mime": p.mime, "size": p.size,
                                "checks": p.checks, "advisory_photo_check": p.vision_verdict} for p in proofs]})
        if kind == "upload" and parts[0].isdigit():  # Phase 12: what the merchant uploaded and confirmed
            up = session.get(MerchantUpload, int(parts[0]))
            if up is not None and up.dispute_id == ev.dispute_id:
                return out(f"Your upload #{up.id}", "Added by you", {
                    "evidence_type": up.evidence_type, "file": up.original_name, "mime": up.mime, "size": up.size,
                    "fact": up.fact, "confirmed_fields": up.fields, "flags": up.flags,
                    "read_automatically": up.extraction, "confirmed_at": up.confirmed_at and up.confirmed_at.isoformat()})
        if kind == "paypal":
            if parts[0] == "capture":
                return out(f"PayPal capture {parts[1]}", "PayPal (live)", await get_capture(parts[1], client=paypal))
            if parts[0] == "order" and "tracker" in parts:
                trackers = trackers_from_order(await get_order(parts[1], client=paypal))
                tid = parts[parts.index("tracker") + 1]
                return out(f"PayPal tracker on order {parts[1]}", "PayPal (live)",
                           [t for t in trackers if t["id"] == tid] or trackers)
            if parts[0] == "dispute":
                d = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == parts[1])).first()
                return out(f"PayPal dispute {parts[1]}", "PayPal (stored copy)", (d.raw or {}) if d else None)
    except PayPalError as exc:
        raise HTTPException(502, f"PayPal did not return the record (debug id {exc.debug_id})") from exc
    except (AttributeError, ValueError, IndexError) as exc:
        raise HTTPException(404, f"source record for {ref} not found") from exc
    raise HTTPException(404, f"no viewer for source {ref}")

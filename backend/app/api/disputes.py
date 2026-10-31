"""Dispute read API (queue, case detail, case file)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agents.investigator import build_case_file
from app.api.decisions import DecisionOut, _out
from app.db import get_session
from app.models import AgentStep, Decision, Dispute
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
    updated_at: str | None
    latest_decision: DecisionBrief | None


class StepOut(BaseModel):
    id: int
    agent: str
    action: str
    detail: dict | None
    created_at: datetime


class DisputeDetail(BaseModel):
    dispute: DisputeRow
    case: CaseFile
    decisions: list[DecisionOut]
    timeline: list[StepOut]


def _latest(session: Session, dispute_id: int) -> Decision | None:
    return session.exec(select(Decision).where(Decision.dispute_id == dispute_id)
                        .order_by(Decision.id.desc())).first()


def _row(session: Session, d: Dispute) -> DisputeRow:
    dec = _latest(session, d.id)
    return DisputeRow(
        id=d.id, paypal_dispute_id=d.paypal_dispute_id, reason=d.reason, stage=d.stage, status=d.status,
        amount=d.amount, currency=d.currency, seller_response_due=d.seller_response_due,
        agent_status=d.agent_status, order_id=d.order_id, updated_at=(d.raw or {}).get("update_time"),
        latest_decision=DecisionBrief(id=dec.id, recommendation=dec.recommendation, confidence=dec.confidence,
                                      status=dec.status.value) if dec else None)


def _find(session: Session, dispute_id: str) -> Dispute:
    d = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == dispute_id)).first()
    if d is None:
        raise HTTPException(404, "unknown dispute")
    return d


@router.get("/api/disputes", response_model=list[DisputeRow])
def list_disputes(session: Session = Depends(get_session)) -> list[DisputeRow]:
    """The merchant's queue: open disputes first, then by response deadline."""
    rows = [_row(session, d) for d in session.exec(select(Dispute)).all()]
    far = datetime.max
    rows.sort(key=lambda r: (r.status == "RESOLVED",
                             (r.seller_response_due or far).replace(tzinfo=None), -r.id))
    return rows


@router.get("/api/disputes/{dispute_id}", response_model=DisputeDetail)
def get_dispute_detail(dispute_id: str, session: Session = Depends(get_session)) -> DisputeDetail:
    d = _find(session, dispute_id)
    decisions = session.exec(select(Decision).where(Decision.dispute_id == d.id).order_by(Decision.id.desc())).all()
    steps = session.exec(select(AgentStep).where(AgentStep.dispute_id == d.id).order_by(AgentStep.id)).all()
    return DisputeDetail(dispute=_row(session, d), case=build_case_file(session, d),
                         decisions=[_out(x, session) for x in decisions],
                         timeline=[StepOut(**s.model_dump(include=set(StepOut.model_fields))) for s in steps])


# Served at the spec's path (/disputes/...) and under /api for the frontend's Vite proxy.
@router.get("/disputes/{dispute_id}/case", response_model=CaseFile)
@router.get("/api/disputes/{dispute_id}/case", response_model=CaseFile, include_in_schema=False)
def get_case(dispute_id: str, session: Session = Depends(get_session)) -> CaseFile:
    """Case file for a dispute, by PayPal dispute id (e.g. PP-R-ZWL-10195422)."""
    return build_case_file(session, _find(session, dispute_id))

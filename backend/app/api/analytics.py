"""Analytics API (Phase 14). Merchant-only (main.py).

  GET  /api/analytics?dataset=real|demo     KPIs, monthly series, calibration report, the fact rows
  POST /api/analytics/query                 {dataset, query: AskQuery} -> grid/chart data (whitelisted, no SQL)
  POST /api/analytics/ask                   {dataset, question} -> LLM -> AskQuery (validated) -> result
dataset: "real" (sandbox/live cases) or "demo" (the labelled demo history); never mixed. Default: real if there are
real outcomes, else demo if there is demo history.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import ratelimit
from app.agents.llm import LLMError
from app.analytics import calibration, kpis as K
from app.analytics.query import AskQuery, QueryError, ask, run
from app.db import get_session
from app.models import DisputeOutcome

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
Dataset = Literal["real", "demo"]


def _facts(session: Session, dataset: str) -> list[dict]:
    rows = session.exec(select(DisputeOutcome).where(DisputeOutcome.is_demo == (dataset == "demo"))
                        .order_by(DisputeOutcome.resolved_at)).all()
    return [K.fact(o) for o in rows]


def _pick(session: Session, dataset: str | None) -> tuple[str, bool, bool]:
    has_real = session.exec(select(DisputeOutcome.id).where(DisputeOutcome.is_demo == False)).first() is not None  # noqa: E712
    has_demo = session.exec(select(DisputeOutcome.id).where(DisputeOutcome.is_demo == True)).first() is not None  # noqa: E712
    return dataset or ("real" if has_real or not has_demo else "demo"), has_real, has_demo


@router.get("")
def summary(dataset: Dataset | None = Query(default=None), session: Session = Depends(get_session)) -> dict:
    ds, has_real, has_demo = _pick(session, dataset)
    rows = _facts(session, ds)
    return {"dataset": ds, "demo": ds == "demo", "has_real": has_real, "has_demo": has_demo,
            "kpis": K.kpis(rows), "by_month": K.by_month(rows),
            "calibration": {"min_outcomes": calibration.MIN_OUTCOMES,
                            "applies_to_decisions": ds == "real",
                            "reasons": calibration.report(rows)},
            "facts": rows}


class QueryIn(BaseModel):
    dataset: Dataset | None = None
    query: AskQuery


class AskIn(BaseModel):
    dataset: Dataset | None = None
    question: str = Field(min_length=3, max_length=500)


@router.post("/query")
def query(body: QueryIn, session: Session = Depends(get_session)) -> dict:
    ds, _, _ = _pick(session, body.dataset)
    try:
        return {"dataset": ds, **run(body.query, _facts(session, ds))}
    except QueryError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/ask", dependencies=[Depends(ratelimit.limit("ask"))])
async def ask_disputes(body: AskIn, session: Session = Depends(get_session)) -> dict:
    """The LLM fills an AskQuery; it's validated against the whitelist. Invalid -> asked again once with the error
    (rule 7) -> still invalid: 422 with the reason (the merchant can build the query with the chips instead)."""
    ds, _, _ = _pick(session, body.dataset)
    facts = _facts(session, ds)
    question, problem = body.question, None
    for _ in range(2):
        try:
            q = await ask(question)
        except LLMError as exc:
            raise HTTPException(503, f"The assistant is unavailable right now ({str(exc)[:120]}); build the query "
                                     "with the controls instead.") from exc
        try:
            return {"dataset": ds, "question": body.question, **run(q, facts)}
        except QueryError as exc:
            problem = str(exc)
            question = f"{body.question}\n\nYour previous query was rejected: {problem}. Fix it."
    raise HTTPException(422, f"Couldn't turn that into an allowed query: {problem}")

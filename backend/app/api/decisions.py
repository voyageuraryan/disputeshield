"""Merchant approval API. Approving resumes the paused agent run, whose execute step calls PayPal.

Safety (rule 5 + real-world races):
- Only PROPOSED decisions can be approved/rejected; the PROPOSED -> APPROVED/REJECTED switch is a single
  conditional UPDATE, so two simultaneous clicks cannot both win (no double submission).
- Before approving, the dispute is re-read from PayPal: a resolved dispute, or an action PayPal no
  longer allows, is refused instead of sending a request that would fail.
- If the paused run can't be resumed (e.g. checkpoint lost on redeploy, or the run ended), the approved
  decision is executed directly; execute_decision still refuses anything not APPROVED.
- A FAILED decision can be retried only when PayPal definitely processed nothing (4xx); the retry is a
  new decision approved by that same call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.agents.investigator import build_case_file
from app.agents.resolution import (
    NOTE_MAX_CHARS,
    build_action_request,
    build_evidence_pdf,
    definitely_not_processed,
    execute_decision,
    SENDABLE,
    default_note,
    pdf_path_for,
    settle_note,
)
from app.agents.runner import resume_run
from app.agents.steps import log_step
from app.db import get_engine, get_session
from app.ingest import upsert_dispute
from app.models import Decision, DecisionStatus, Dispute, Order
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.paypal.disputes import get_dispute

log = logging.getLogger(__name__)
router = APIRouter(prefix="/decisions", tags=["decisions"])


class ApproveIn(BaseModel):
    action: Literal["FIGHT", "SETTLE", "SETTLE_PARTIAL", "REPLACEMENT"] | None = None  # override the action
    # Phase 13: SETTLE_PARTIAL: the merchant may edit the offer (checked: 10%-90% and their max-offer setting)
    offer_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    note: str | None = Field(default=None, min_length=1, max_length=NOTE_MAX_CHARS)  # edited note
    approved_by: str | None = Field(default=None, max_length=200)


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class DecisionOut(BaseModel):
    id: int
    dispute_id: int
    recommendation: str
    confidence: str
    reasoning: str
    cited_evidence_ids: list[int]
    draft_note: str | None
    final_note: str | None
    approved_action: str | None
    status: str
    approved_at: datetime | None
    executed_at: datetime | None
    action_request: dict | None
    paypal_response: dict | None
    retry_of: int | None
    economic_note: str | None
    missing_items: list[str] | None
    analysed_threshold: Decimal | None
    data_as_of: datetime | None = None       # Phase 11: when the PayPal data behind it was fetched
    stale_sources: list[str] | None = None   # sources that couldn't be refreshed (last good copy used)
    offer_amount: Decimal | None = None      # Phase 13: SETTLE_PARTIAL offer (proposed, or as approved)
    options: list[dict] | None = None        # calculator output: Fight / Full refund / Partial offer / Replacement
    can_retry: bool


def _already_retried(session: Session, d: Decision) -> bool:
    return session.exec(select(Decision.id).where(Decision.retry_of == d.id)).first() is not None


def retryable(session: Session, d: Decision) -> bool:
    """Only a FAILED decision PayPal definitely didn't process, and only once (no parallel retries)."""
    return (d.status == DecisionStatus.FAILED and definitely_not_processed(d.paypal_response)
            and not _already_retried(session, d))


def _out(d: Decision, session: Session | None = None) -> DecisionOut:
    fields = set(DecisionOut.model_fields) - {"status", "can_retry"}
    can = retryable(session, d) if session is not None else (
        d.status == DecisionStatus.FAILED and definitely_not_processed(d.paypal_response))
    return DecisionOut(**d.model_dump(include=fields), status=d.status.value, can_retry=can)


def _get(session: Session, decision_id: int) -> tuple[Decision, Dispute]:
    d = session.get(Decision, decision_id)
    if d is None:
        raise HTTPException(404, "unknown decision")
    return d, session.get(Dispute, d.dispute_id)


def _claim(session: Session, d: Decision, new: DecisionStatus, **values) -> None:
    """Atomically move PROPOSED -> new. Raises 409 if someone else got there first."""
    res = session.execute(update(Decision).where(Decision.id == d.id, Decision.status == DecisionStatus.PROPOSED)
                          .values(status=new, **values))
    session.commit()
    if res.rowcount != 1:
        session.refresh(d)
        raise HTTPException(409, f"decision is {d.status.value}; only PROPOSED decisions can be approved or rejected")
    session.refresh(d)


async def _refresh_from_paypal(session: Session, dispute: Dispute, paypal: PayPalClient) -> None:
    """Re-read the dispute so approval is checked against PayPal's current state (not a stale copy)."""
    try:
        pp = await get_dispute(dispute.paypal_dispute_id, client=paypal)
    except PayPalError as exc:
        raise HTTPException(502, f"could not re-check the dispute with PayPal (debug id {exc.debug_id}); "
                                 f"nothing was sent") from exc
    upsert_dispute(session, pp)  # also retires PROPOSED decisions if PayPal resolved the dispute
    session.commit()
    session.refresh(dispute)


def _offer_for(session: Session, d: Decision, action: str, body_offer: Decimal | None) -> Decimal | None:
    """The offer to send for SETTLE_PARTIAL: the merchant's edit, else the proposed one. 422 if out of bounds."""
    if action != "SETTLE_PARTIAL":
        return None
    offer = body_offer if body_offer is not None else d.offer_amount
    if offer is None:
        raise HTTPException(422, "a partial refund offer needs offer_amount")
    return offer


def _request_for(session: Session, d: Decision, dispute: Dispute, action: str, note: str,
                 offer: Decimal | None = None) -> dict:
    case = build_case_file(session, dispute)
    pdf = None
    if action == "FIGHT":
        order = session.get(Order, case.order_id) if case.order_id else None
        from app.troubleshooting.outcome import pdf_section

        from app.agents.resolution import attached_labels

        pdf = str(build_evidence_pdf(pdf_path_for(d.id), case, note, order=order,
                                     troubleshooting=pdf_section(session, dispute.id),
                                     attached=attached_labels(session, case)))
    try:
        return build_action_request(session, dispute, case, action, note, pdf, offer)
    except ValueError as exc:
        out_of_bounds = action == "SETTLE_PARTIAL" and case.can_offer
        raise HTTPException(422 if out_of_bounds else 409, str(exc)) from exc


async def _execute(session: Session, engine: Engine, paypal: PayPalClient, d: Decision) -> None:
    """Resume the paused run; if there is none to resume, execute directly (still APPROVED-guarded)."""
    if d.run_key:
        try:
            await resume_run(d.run_key, {"approved": True, "decision_id": d.id}, engine, client=paypal)
            session.refresh(d)
            if d.status != DecisionStatus.APPROVED:
                return  # the run's execute step handled it
        except ValueError as exc:
            log.warning("decision %s: run %s not resumable (%s); executing directly", d.id, d.run_key, exc)
    log_step(session, d.dispute_id, "orchestrator", "No paused run to resume; executing the approved decision directly",
             {"decision_id": d.id})
    await execute_decision(session, d.id, client=paypal)
    session.refresh(d)


@router.get("/{decision_id}", response_model=DecisionOut)
def get_decision(decision_id: int, session: Session = Depends(get_session)) -> DecisionOut:
    d, _ = _get(session, decision_id)
    return _out(d, session)


@router.get("/{decision_id}/evidence.pdf")
def evidence_pdf(decision_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """The evidence summary PDF this decision sends (or sent) to PayPal."""
    d, _ = _get(session, decision_id)
    paths = [a["path"] for a in (d.action_request or {}).get("attachments", [])]
    if not paths or not Path(paths[0]).is_file():
        raise HTTPException(404, "this decision has no evidence PDF")
    return FileResponse(paths[0], media_type="application/pdf", filename=f"evidence-decision-{d.id}.pdf")


@router.post("/{decision_id}/approve", response_model=DecisionOut)
async def approve(decision_id: int, body: ApproveIn | None = None, session: Session = Depends(get_session),
                  engine: Engine = Depends(get_engine),
                  paypal: PayPalClient = Depends(get_paypal_client)) -> DecisionOut:
    return await approve_core(session, engine, paypal, decision_id, body or ApproveIn())


async def approve_core(session: Session, engine: Engine, paypal: PayPalClient, decision_id: int,
                       body: ApproveIn) -> DecisionOut:
    """THE approval path (dashboard and MCP alike): PROPOSED -> APPROVED atomically after re-checking the dispute with
    PayPal, then execution (which checks APPROVED again, rule 5). Raises HTTPException on refusal."""
    d, dispute = _get(session, decision_id)
    if d.status != DecisionStatus.PROPOSED:
        raise HTTPException(409, f"decision is {d.status.value}; only PROPOSED decisions can be approved or rejected")
    action = body.action or d.recommendation
    if action not in SENDABLE:
        raise HTTPException(409, f"{action} has nothing to send to PayPal; approve with action FIGHT, SETTLE, "
                                 f"SETTLE_PARTIAL or REPLACEMENT, or reject")
    offer = _offer_for(session, d, action, body.offer_amount)
    await _refresh_from_paypal(session, dispute, paypal)
    session.refresh(d)
    if dispute.status == "RESOLVED" or d.status != DecisionStatus.PROPOSED:
        raise HTTPException(409, "the dispute is already resolved at PayPal; nothing can be sent")

    same_offer = offer is None or offer == d.offer_amount
    note = body.note or (d.draft_note if action == d.recommendation and same_offer else None)
    if note is None:
        if action == "FIGHT":
            raise HTTPException(422, "approving FIGHT for a decision without a drafted seller note needs a note")
        note = default_note(build_case_file(session, dispute), action, offer)
    # Always rebuild from PayPal's current state (requested evidence types, allowed actions, offer types).
    request = _request_for(session, d, dispute, action, note, offer)

    proposed_offer = d.offer_amount
    _claim(session, d, DecisionStatus.APPROVED, approved_at=datetime.now(timezone.utc), approved_action=action,
           final_note=note, action_request=request, **({"offer_amount": offer} if offer is not None else {}))
    log_step(session, dispute.id, "merchant",
             f"Approved {action}" + (f" of {offer} {dispute.currency}" if offer is not None else "")
             + (f" (changed from {d.recommendation})" if action != d.recommendation else "")
             + (f" (edited from {proposed_offer})" if offer is not None and proposed_offer not in (None, offer)
                else "")
             + (" with an edited note" if body.note and body.note != d.draft_note else ""),
             {"decision_id": d.id, "approved_by": body.approved_by, "endpoint": request["endpoint"]})
    await _execute(session, engine, paypal, d)
    return _out(d, session)


@router.post("/{decision_id}/reject", response_model=DecisionOut)
async def reject(decision_id: int, body: RejectIn | None = None, session: Session = Depends(get_session),
                 engine: Engine = Depends(get_engine),
                 paypal: PayPalClient = Depends(get_paypal_client)) -> DecisionOut:
    return await reject_core(session, engine, paypal, decision_id, body)


async def reject_core(session: Session, engine: Engine, paypal: PayPalClient, decision_id: int,
                      body: RejectIn | None) -> DecisionOut:
    d, dispute = _get(session, decision_id)
    _claim(session, d, DecisionStatus.REJECTED)
    dispute.agent_status = "REJECTED"
    session.add(dispute)
    session.commit()
    log_step(session, dispute.id, "merchant", "Rejected the recommendation",
             {"decision_id": d.id, "reason": body.reason if body else None})
    if d.run_key:
        try:
            await resume_run(d.run_key, {"approved": False, "decision_id": d.id}, engine, client=paypal)
        except ValueError:
            pass  # nothing paused to close; the rejection itself is recorded
    session.refresh(d)
    return _out(d, session)


@router.post("/{decision_id}/retry", response_model=DecisionOut)
async def retry(decision_id: int, body: ApproveIn | None = None, session: Session = Depends(get_session),
                engine: Engine = Depends(get_engine),
                paypal: PayPalClient = Depends(get_paypal_client)) -> DecisionOut:
    """Retry a FAILED decision that PayPal definitely did not process. This call is the merchant's
    approval of the new attempt (optionally with a different action or note)."""
    body = body or ApproveIn()
    old, dispute = _get(session, decision_id)
    if old.status != DecisionStatus.FAILED:
        raise HTTPException(409, f"only FAILED decisions can be retried (this one is {old.status.value})")
    if _already_retried(session, old):
        raise HTTPException(409, f"decision #{old.id} was already retried; retry the newest failed decision instead")
    if not definitely_not_processed(old.paypal_response):
        raise HTTPException(409, "PayPal may have processed the failed request (server error or timeout); "
                                 "check the dispute in PayPal instead of retrying")
    await _refresh_from_paypal(session, dispute, paypal)
    if dispute.status == "RESOLVED":
        raise HTTPException(409, "the dispute is already resolved at PayPal; nothing can be sent")
    previous_action = old.approved_action or old.recommendation
    action = body.action or previous_action
    # Reuse the earlier note only for the same action: a FIGHT seller note must never become a refund note.
    note = body.note or ((old.final_note or old.draft_note) if action == previous_action else None)
    offer = _offer_for(session, old, action, body.offer_amount) if action in SENDABLE else None
    if action in SENDABLE and action != "FIGHT" and not note:
        note = default_note(build_case_file(session, dispute), action, offer)
    if action not in SENDABLE or not note:
        raise HTTPException(422, "retry needs action FIGHT, SETTLE, SETTLE_PARTIAL or REPLACEMENT and a note")
    new = Decision(dispute_id=old.dispute_id, recommendation=old.recommendation, confidence=old.confidence,
                   reasoning=old.reasoning, cited_evidence_ids=old.cited_evidence_ids, draft_note=old.draft_note,
                   economic_note=old.economic_note, missing_items=old.missing_items,
                   analysed_threshold=old.analysed_threshold, offer_amount=offer, options=old.options,
                   status=DecisionStatus.PROPOSED, retry_of=old.id)
    session.add(new)
    session.commit()
    session.refresh(new)
    request = _request_for(session, new, dispute, action, note, offer)
    _claim(session, new, DecisionStatus.APPROVED, approved_at=datetime.now(timezone.utc), approved_action=action,
           final_note=note, action_request=request)
    log_step(session, dispute.id, "merchant", f"Retried failed decision #{old.id} as {action} (decision #{new.id})",
             {"decision_id": new.id, "retry_of": old.id, "approved_by": body.approved_by,
              "endpoint": request["endpoint"]})
    await execute_decision(session, new.id, client=paypal)
    session.refresh(new)
    return _out(new, session)

"""Admin-only sandbox helpers so a demo can finish a case (PayPal sandbox simulators).

Requires header X-Admin-Token == ADMIN_TOKEN (helpers are disabled when ADMIN_TOKEN is empty).
PayPal base URL is sandbox-only by config, and these endpoints exist only in the sandbox anyway.
After the PayPal call the dispute is re-ingested so the app reflects the new state (which may
start a new agent run for the new dispute version).
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.agents.resolution import paypal_issues
from app.agents.runner import run_agents
from app import ratelimit
from app.config import get_settings
from app.db import get_engine, get_session
from app.ingest import ingest_dispute
from app.models import Dispute
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.paypal.disputes import get_dispute, sandbox_adjudicate, sandbox_require_evidence


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token.get_secret_value()
    if not expected:
        raise HTTPException(404, "not found")  # helpers disabled
    ratelimit.blocked(request, "admin_fail")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        ratelimit.failed(request, "admin_fail")  # only wrong tokens count: 5 guesses a minute per IP
        raise HTTPException(403, "admin token required")


router = APIRouter(prefix="/admin/sandbox", tags=["admin"], dependencies=[Depends(require_admin)])


class RequireEvidenceIn(BaseModel):
    action: Literal["SELLER_EVIDENCE", "BUYER_EVIDENCE"] = "SELLER_EVIDENCE"


class AdjudicateIn(BaseModel):
    outcome: Literal["SELLER_FAVOR", "BUYER_FAVOR"]


SETTLE_WAIT_S = (1.0, 2.0, 3.0)  # PayPal applies these changes asynchronously


async def _wait_for_change(dispute_id: str, before: str | None, paypal: PayPalClient) -> None:
    """Seen live: the dispute read right after require-evidence still showed UNDER_REVIEW.
    Wait briefly until PayPal reports a newer version, so we ingest the real new state."""
    for delay in SETTLE_WAIT_S:
        await asyncio.sleep(delay)
        try:
            now = (await get_dispute(dispute_id, client=paypal)).update_time
        except PayPalError:
            return
        if now and (before is None or now.isoformat() != before):
            return


async def _after(session: Session, engine: Engine, background: BackgroundTasks, paypal: PayPalClient,
                 dispute_id: str, resp: dict, before: str | None) -> dict:
    await _wait_for_change(dispute_id, before, paypal)
    result = await ingest_dispute(session, dispute_id, client=paypal)
    if result.run_key:
        background.add_task(run_agents, result.dispute_row_id, result.run_key, engine)
    d = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == dispute_id)).one()
    return {"paypal_response": resp, "dispute": {"status": d.status, "stage": d.stage,
                                                 "agent_run_queued": bool(result.run_key)}}


def _version(session: Session, dispute_id: str) -> str | None:
    d = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == dispute_id)).first()
    ts = (d.raw or {}).get("update_time") if d else None
    if not ts:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


STATE_HINT = ("PayPal's sandbox refused this in the case's current state. Seen live: adjudicate is refused on a "
              "chargeback that PayPal is reviewing, even when the case lists it. Try a case that was escalated "
              "from an inquiry to a claim, or let PayPal resolve the case on its own schedule.")


def _paypal_error(exc: PayPalError) -> HTTPException:
    body = exc.body if isinstance(exc.body, dict) else None
    detail = {"paypal_status": exc.status_code, "debug_id": exc.debug_id, "paypal": body}
    if "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE" in paypal_issues(body):
        detail["hint"] = STATE_HINT
    return HTTPException(502, detail)


@router.post("/disputes/{dispute_id}/require-evidence")
async def require_evidence(dispute_id: str, body: RequireEvidenceIn, background: BackgroundTasks,
                           session: Session = Depends(get_session), engine: Engine = Depends(get_engine),
                           paypal: PayPalClient = Depends(get_paypal_client)) -> dict:
    before = _version(session, dispute_id)
    try:
        resp = await sandbox_require_evidence(dispute_id, body.action, client=paypal)
    except PayPalError as exc:
        raise _paypal_error(exc) from exc
    return await _after(session, engine, background, paypal, dispute_id, resp, before)


@router.post("/disputes/{dispute_id}/adjudicate")
async def adjudicate(dispute_id: str, body: AdjudicateIn, background: BackgroundTasks,
                     session: Session = Depends(get_session), engine: Engine = Depends(get_engine),
                     paypal: PayPalClient = Depends(get_paypal_client)) -> dict:
    before = _version(session, dispute_id)
    try:
        resp = await sandbox_adjudicate(dispute_id, body.outcome, client=paypal)
    except PayPalError as exc:
        raise _paypal_error(exc) from exc
    return await _after(session, engine, background, paypal, dispute_id, resp, before)

"""Start and resume agent runs (one LangGraph thread per dispute version / run key)."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.agents.graph import build_graph, thread_config
from app.agents.llm import StructuredLLM
from app.agents.steps import log_step
from app.config import get_settings
from app.models import AgentStep, Dispute
from app.tracing import observe, update_trace
from app.paypal.client import PayPalClient

log = logging.getLogger(__name__)

__all__ = ["run_agents", "resume_run", "log_step"]


def _checkpoint_path(path: str | None) -> str:
    return path or get_settings().checkpoint_db


@observe(type="agent")
async def _invoke(graph, dispute_row_id: int, run_key: str, fresh: bool = False) -> dict[str, Any]:  # noqa: ANN001
    """One trace per agent run when DEEPEVAL_TRACING=1 (investigate / analyze / resolve spans nest under it).
    DeepEval's LangGraph CallbackHandler was tried first: with deepeval 4.3.2 + our graph every node start raised
    TypeError inside the handler and the nodes came out as separate traces (PROGRESS G70), so it isn't used."""
    result = await graph.ainvoke({"dispute_row_id": dispute_row_id, "run_key": run_key, "fresh": fresh},
                                 thread_config(run_key))
    update_trace(name=f"agent run {run_key}", input={"dispute_row_id": dispute_row_id, "run_key": run_key},
                 output="waiting for approval" if _pending(result) is not None else "finished")
    return result


def _pending(result: dict[str, Any]) -> Any:
    interrupts = result.get("__interrupt__") or []
    return interrupts[0].value if interrupts else None


# ---- one run per dispute at a time (Phase 11) ----------------------------------------------------------------
RUNNING: set[int] = set()                 # disputes with a run in this process (claimed before it is scheduled)
_RUNNING_LOCK = threading.Lock()
STARTED_STALE = timedelta(minutes=15)     # a run_started with no end marker older than this is treated as dead
END_MARKERS = ("run_finished", "run_failed", "run_skipped")


def claim(dispute_row_id: int) -> bool:
    with _RUNNING_LOCK:
        if dispute_row_id in RUNNING:
            return False
        RUNNING.add(dispute_row_id)
        return True


def release(dispute_row_id: int) -> None:
    with _RUNNING_LOCK:
        RUNNING.discard(dispute_row_id)


def in_progress(session: Session, dispute_row_id: int) -> bool:
    """A run is going on: claimed in this process, or (another process / before a crash) a recent run_started
    on the timeline with no end marker after it."""
    if dispute_row_id in RUNNING:
        return True
    last = session.exec(select(AgentStep).where(AgentStep.dispute_id == dispute_row_id,
                                                AgentStep.agent == "orchestrator",
                                                AgentStep.action.in_(("run_started", *END_MARKERS)))
                        .order_by(AgentStep.id.desc())).first()
    if last is None or last.action != "run_started":
        return False
    started = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < STARTED_STALE


async def run_agents(dispute_row_id: int, run_key: str, engine: Engine, *,
                     client: PayPalClient | None = None, checkpoint_path: str | None = None,
                     llm: StructuredLLM | None = None, fresh: bool = False,
                     claimed: bool = False) -> dict[str, Any] | None:
    """fresh: fetch everything from PayPal now (manual re-analyse) instead of reusing a copy fetched in the last
    60 s. claimed: the caller already claimed the dispute (the re-analyse endpoint does, so double clicks get 409)."""
    if not claimed and not claim(dispute_row_id):
        with Session(engine) as session:
            log_step(session, dispute_row_id, "orchestrator", "run_skipped",
                     {"run_key": run_key, "reason": "another run for this dispute is in progress"})
        return None
    try:
        return await _run(dispute_row_id, run_key, engine, client=client, checkpoint_path=checkpoint_path, llm=llm,
                          fresh=fresh)
    finally:
        release(dispute_row_id)


async def _run(dispute_row_id: int, run_key: str, engine: Engine, *, client, checkpoint_path, llm,  # noqa: ANN001
               fresh: bool) -> dict[str, Any] | None:
    with Session(engine) as session:
        dispute = session.get(Dispute, dispute_row_id)
        if dispute is None:
            log.error("agent run for missing dispute %s", dispute_row_id)
            return None
        log_step(session, dispute.id, "orchestrator", "run_started", {"run_key": run_key, "fresh": fresh})
        if dispute.status == "RESOLVED":
            log_step(session, dispute.id, "orchestrator", "run_skipped", {"reason": "dispute resolved"})
            return None

    try:
        async with AsyncSqliteSaver.from_conn_string(_checkpoint_path(checkpoint_path)) as saver:
            graph = build_graph(engine, saver, client, llm)
            result = await _invoke(graph, dispute_row_id, run_key, fresh)
    except Exception as exc:  # background task: record the failure on the timeline, don't crash
        log.exception("agent run %s failed", run_key)
        with Session(engine) as session:
            log_step(session, dispute_row_id, "orchestrator", "run_failed", {"run_key": run_key, "error": str(exc)})
        return None

    with Session(engine) as session:
        if _pending(result) is not None:
            log_step(session, dispute_row_id, "orchestrator", "Waiting for merchant approval",
                     {"run_key": run_key, "thread_id": run_key})
        log_step(session, dispute_row_id, "orchestrator", "run_finished", {"run_key": run_key})
    return result


async def resume_run(run_key: str, approval: dict[str, Any], engine: Engine, *,
                     client: PayPalClient | None = None, checkpoint_path: str | None = None,
                     llm: StructuredLLM | None = None) -> dict[str, Any]:
    """Resume a run paused at approval. `approval` becomes the interrupt()'s return value."""
    async with AsyncSqliteSaver.from_conn_string(_checkpoint_path(checkpoint_path)) as saver:
        graph = build_graph(engine, saver, client, llm)
        config = thread_config(run_key)
        state = await graph.aget_state(config)
        if "approve" not in (state.next or ()):
            raise ValueError(f"run {run_key} is not waiting for approval (next={state.next})")
        return await graph.ainvoke(Command(resume=approval), config)

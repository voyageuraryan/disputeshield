"""Start and resume agent runs (one LangGraph thread per dispute version / run key)."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.agents.graph import build_graph, thread_config
from app.agents.llm import StructuredLLM
from app.agents.steps import log_step
from app.config import get_settings
from app.models import Dispute
from app.paypal.client import PayPalClient

log = logging.getLogger(__name__)

__all__ = ["run_agents", "resume_run", "log_step"]


def _checkpoint_path(path: str | None) -> str:
    return path or get_settings().checkpoint_db


def _pending(result: dict[str, Any]) -> Any:
    interrupts = result.get("__interrupt__") or []
    return interrupts[0].value if interrupts else None


async def run_agents(dispute_row_id: int, run_key: str, engine: Engine, *,
                     client: PayPalClient | None = None, checkpoint_path: str | None = None,
                     llm: StructuredLLM | None = None) -> dict[str, Any] | None:
    with Session(engine) as session:
        dispute = session.get(Dispute, dispute_row_id)
        if dispute is None:
            log.error("agent run for missing dispute %s", dispute_row_id)
            return None
        log_step(session, dispute.id, "orchestrator", "run_started", {"run_key": run_key})
        if dispute.status == "RESOLVED":
            log_step(session, dispute.id, "orchestrator", "run_skipped", {"reason": "dispute resolved"})
            return None

    try:
        async with AsyncSqliteSaver.from_conn_string(_checkpoint_path(checkpoint_path)) as saver:
            graph = build_graph(engine, saver, client, llm)
            result = await graph.ainvoke({"dispute_row_id": dispute_row_id, "run_key": run_key},
                                         thread_config(run_key))
    except Exception as exc:  # background task: record the failure on the timeline, don't crash
        log.exception("agent run %s failed", run_key)
        with Session(engine) as session:
            log_step(session, dispute_row_id, "orchestrator", "run_failed", {"run_key": run_key, "error": str(exc)})
        return None

    if _pending(result) is not None:
        with Session(engine) as session:
            log_step(session, dispute_row_id, "orchestrator", "Waiting for merchant approval",
                     {"run_key": run_key, "thread_id": run_key})
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

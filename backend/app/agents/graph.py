"""Agent pipeline as a LangGraph StateGraph:

    investigate -> analyze -> resolve -> approve (interrupt) -> execute

A SQLite checkpointer (AsyncSqliteSaver, CHECKPOINT_DB) persists every step, so a
run paused at `approve` survives restarts and is resumed with
Command(resume={...}) on the same thread_id (we use the run key).

Implemented: investigate, analyze (rules + LLM -> PROPOSED decision). If the analyst
marks the case NEEDS_HUMAN the run ends there (nothing to approve). resolve passes
through; execute never calls PayPal yet (rule 5: needs an approved Decision row).
Phase 6: resolve drafts the response + PayPal request; execute calls PayPal only for an
APPROVED decision.

Dependencies (DB engine, PayPal client) are bound when the graph is built rather
than passed through config, so nothing unserialisable reaches the checkpointer.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.agents.analyst import analyze
from app.agents.investigator import investigate
from app.agents.resolution import execute_decision, resolve
from app.agents import llm as llm_module
from app.agents.llm import StructuredLLM
from app.agents.steps import log_step
from app.models import Dispute
from app.paypal.client import PayPalClient
from app.paypal.disputes import NotApprovedError
from app.schemas import CaseFile


class CaseState(TypedDict, total=False):
    dispute_row_id: int
    run_key: str
    case_file: dict[str, Any]          # CaseFile.model_dump(mode="json")
    recommendation: dict[str, Any] | None
    decision_id: int | None
    needs_human: bool
    action_request: dict[str, Any] | None  # the PayPal call execute will make
    approval: dict[str, Any] | None    # value passed to Command(resume=...)
    execution: dict[str, Any] | None


def build_graph(engine: Engine, checkpointer: BaseCheckpointSaver | None = None,
                client: PayPalClient | None = None, llm: StructuredLLM | None = None):
    def _session() -> Session:
        return Session(engine)

    async def investigate_node(state: CaseState) -> CaseState:
        with _session() as s:
            dispute = s.get(Dispute, state["dispute_row_id"])
            case = await investigate(s, dispute, client)
        return {"case_file": case.model_dump(mode="json")}

    async def analyze_node(state: CaseState) -> CaseState:
        with _session() as s:
            dispute = s.get(Dispute, state["dispute_row_id"])
            case = CaseFile.model_validate(state["case_file"])
            result = await analyze(s, dispute, case, run_key=state.get("run_key"),
                                   llm=llm or llm_module.structured_call)
        return {"recommendation": result.recommendation.model_dump(mode="json") if result.recommendation else None,
                "decision_id": result.decision_id, "needs_human": result.needs_human}

    async def resolve_node(state: CaseState) -> CaseState:
        with _session() as s:
            dispute = s.get(Dispute, state["dispute_row_id"])
            case = CaseFile.model_validate(state["case_file"])
            out = await resolve(s, dispute, case, state["decision_id"], llm=llm)
        return {"action_request": out["action_request"], "needs_human": out["needs_human"]}

    async def approve_node(state: CaseState) -> CaseState:
        # Pauses the run. On resume LangGraph re-runs this node from the top, so
        # nothing with side effects goes before interrupt().
        decision = interrupt({"dispute_row_id": state["dispute_row_id"], "run_key": state["run_key"],
                              "decision_id": state.get("decision_id"),
                              "recommendation": state.get("recommendation"),
                              "action_request": state.get("action_request")})
        return {"approval": decision}

    async def execute_node(state: CaseState) -> CaseState:
        approval = state.get("approval") or {}
        with _session() as s:
            if not approval.get("approved"):
                log_step(s, state["dispute_row_id"], "executor", "Merchant rejected the decision; nothing sent to PayPal",
                         {"approval": approval})
                return {"execution": {"status": "SKIPPED"}}
            try:
                # Rule 5: execute_decision refuses unless the Decision row is APPROVED.
                decision = await execute_decision(s, state["decision_id"], client=client)
            except NotApprovedError as exc:
                return {"execution": {"status": "REFUSED", "reason": str(exc)}}
            return {"execution": {"status": decision.status.value, "paypal_response": decision.paypal_response}}

    g = StateGraph(CaseState)
    g.add_node("investigate", investigate_node)
    g.add_node("analyze", analyze_node)
    g.add_node("resolve", resolve_node)
    g.add_node("approve", approve_node)
    g.add_node("execute", execute_node)
    g.add_edge(START, "investigate")
    g.add_edge("investigate", "analyze")
    g.add_conditional_edges("analyze", lambda st: END if st.get("needs_human") else "resolve", ["resolve", END])
    g.add_conditional_edges("resolve", lambda st: END if st.get("needs_human") else "approve", ["approve", END])
    g.add_edge("approve", "execute")
    g.add_edge("execute", END)
    return g.compile(checkpointer=checkpointer)


def thread_config(run_key: str) -> dict:
    return {"configurable": {"thread_id": run_key}}

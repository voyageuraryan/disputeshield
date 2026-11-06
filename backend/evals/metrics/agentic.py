"""Agentic metrics over the traced run (needs DEEPEVAL_TRACING=1; the eval conftest turns it on).

- ToolCorrectness (deterministic here: no available_tools, exact ordered match): the shadow investigation's tool
  spans vs the tools a correct investigator must call for this golden (expected_tools below), e.g. no PayPal
  tracker lookup when there is no PayPal order. Runs on every golden (no judge calls).
- StepEfficiency, TaskCompletion (judge): on the full traced run (investigation + analysis + resolution); sampled.
"""

from __future__ import annotations

from deepeval.metrics import StepEfficiencyMetric, TaskCompletionMetric, ToolCorrectnessMetric
from deepeval.test_case import LLMTestCase, ToolCall

from evals.fake_paypal import capture_id, paypal_order_id
from evals.goldens.schema import Golden

THRESHOLD = 0.7
STORE_TOOLS = ["store_order", "store_shipments", "store_messages", "store_listing", "buyer_history"]
TASK = ("Investigate a PayPal dispute completely: fetch it from PayPal (and the capture / PayPal order when they "
        "exist) and gather the full case file from the store (order, shipments, buyer messages, listing, buyer "
        "history, troubleshooting result, refund policy) so nothing relevant is missed; then recommend FIGHT, SETTLE "
        "or GATHER_MORE citing that evidence, and prepare (not send) the matching PayPal response for the merchant. "
        "How the trace works: each store lookup span writes what it found into the case file as evidence items, so "
        "those spans have no output of their own by design; a lookup that finds nothing (e.g. no buyer messages) is "
        "complete. Judge completeness from the case file and the recommendation, not from empty span outputs. "
        "The 'investigate' span is a separate shadow run against a simulated PayPal, used only to check which tools "
        "are called; its case file can differ from the test case's. The analysis and the response use the case file "
        "in CASE, which is the one to judge.")


def expected_tools(g: Golden) -> list[str]:
    """What the investigator must look up, in its fixed order (app/agents/investigator.py Investigator.run)."""
    tools = ["get_dispute"]
    if capture_id(g):
        tools.append("get_capture")
    has_order = g.case_file.order_id is not None and g.order is not None
    if has_order and paypal_order_id(g):
        tools.append("get_order")  # trackers live on the PayPal order: no order, no tracker call
    if has_order:
        tools += STORE_TOOLS
    return tools + ["troubleshooting_record", "refund_policy"]


def tool_spans(trace: dict | None) -> list[str]:
    """Tool span names in execution order (depth-first) from a DeepEval nested span dict."""
    out: list[str] = []

    def walk(span: dict) -> None:
        if str(span.get("type", "")).lower().endswith("tool"):
            out.append(span.get("name"))
        for child in span.get("children") or []:
            walk(child)
    if trace:
        walk(trace)
    return out


def tool_correctness(judge) -> ToolCorrectnessMetric:
    return ToolCorrectnessMetric(model=judge, threshold=1.0, should_exact_match=True, include_reason=True,
                                 async_mode=False)


def tool_case(g: Golden, called: list[str]) -> LLMTestCase:
    return LLMTestCase(input=f"Investigate dispute {g.case_file.dispute_id}", actual_output="investigation done",
                       tools_called=[ToolCall(name=n) for n in called],
                       expected_tools=[ToolCall(name=n) for n in expected_tools(g)])


def run_case(g: Golden, out: dict) -> LLMTestCase:
    needed = ", ".join(expected_tools(g))
    case = LLMTestCase(input=TASK + f" Dispute: {g.case_file.dispute_id} ({g.case_file.reason}). The lookups this "
                       f"case's data calls for (a capture / PayPal order lookup only when one exists): {needed}.",
                       actual_output=f"{out.get('action')}: {out.get('reasoning') or ''}"
                                     + (f"\nPrepared: {out['request']['endpoint']}" if out.get("request") else ""))
    trace = out.get("trace")
    if trace:  # the task is the metric's definition, not the cached trace's: always state the current one
        trace = {**trace, "input": f"TASK: {TASK}\nLOOKUPS THIS CASE CALLS FOR: {needed}\n"
                                   f"CASE: {g.case_file.model_dump_json()}"}
    case._trace_dict = trace  # what DeepEval itself sets when it evaluates a traced run
    return case


def step_efficiency(judge) -> StepEfficiencyMetric:
    return StepEfficiencyMetric(model=judge, threshold=THRESHOLD, include_reason=True, async_mode=False)


def task_completion(judge) -> TaskCompletionMetric:
    return TaskCompletionMetric(model=judge, threshold=THRESHOLD, task=TASK, include_reason=True, async_mode=False)

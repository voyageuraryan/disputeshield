"""One eval per golden (the real analyst + resolution, the model under test) scored by the Phase 10B metrics, then
the run gates (evals/gates.py). Run with scripts/run_evals.sh (deepeval test run), never with plain pytest."""

from __future__ import annotations

import pytest
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from evals.checks import STRICT
from evals.metrics import deterministic_metrics
from evals.metrics.agentic import run_case, step_efficiency, task_completion, tool_case, tool_correctness
from evals.metrics.judged import judged_cases
from evals.report import deep_goldens, metric_row, record, selected_goldens

GOLDENS = selected_goldens()
DEEP = {g.id for g in deep_goldens()}


def measure(metric, case) -> None:
    """Measure one metric; a failure to measure (judge quota, timeout) is recorded as an error, not a crash."""
    try:
        metric.measure(case)
    except Exception as exc:  # noqa: BLE001
        metric.score, metric.success, metric.error = None, None, f"{type(exc).__name__}: {str(exc)[:300]}"


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.id for g in GOLDENS])
def test_golden(golden, outputs, judge):
    out = outputs[golden.id]
    if out.get("error"):  # rate limit / provider outage: not the model's fault; the gates count it as incomplete
        record(golden, out, [])
        pytest.skip(f"{out['error']}: {out.get('detail', '')[:200]}")
    gold = golden.model_dump(mode="json")
    case = LLMTestCase(input=f"Dispute {golden.case_file.dispute_id}", actual_output=f"{out.get('action')}: "
                       f"{out.get('reasoning') or ''}", name=golden.id, metadata={"output": out, "golden": gold})
    det = deterministic_metrics()
    rows = []
    for m in det:
        measure(m, case)
        rows.append(metric_row(m, "deterministic"))
    if out.get("tools_called") is not None:
        from evals.judge import NoCallModel

        tc = tool_correctness(judge or NoCallModel())
        measure(tc, tool_case(golden, out["tools_called"]))
        rows.append(metric_row(tc, "agentic"))
    if judge is not None:
        for metric, mcase in judged_cases(gold, out, judge, deep=golden.id in DEEP):
            measure(metric, mcase)
            rows.append(metric_row(metric, "judged"))
        if golden.id in DEEP and out.get("trace"):
            for metric in (step_efficiency(judge), task_completion(judge)):
                measure(metric, run_case(golden, out))
                rows.append(metric_row(metric, "agentic"))
    record(golden, out, rows)
    if len(golden.acceptable_actions) == 1:  # non-borderline: the validity metrics must all pass (gate 1)
        assert_test(case, [m for m in deterministic_metrics() if m.key in STRICT], run_async=False)

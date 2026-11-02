"""One eval per golden: the real analyst + resolution (model under test) judged by deterministic metrics and the
Gemini judge. Run with scripts/run_evals.sh (deepeval test run), never with plain pytest."""

from __future__ import annotations

import pytest
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from evals.metrics import deterministic_metrics, judged_metrics
from evals.report import record, selected_goldens

GOLDENS = selected_goldens()


def test_case_for(golden, out: dict) -> LLMTestCase:
    c = golden.case_file
    context = [f"Dispute {c.dispute_id}: reason {c.reason}, {c.amount} {c.currency}, stage {c.stage}, status "
               f"{c.status}; PayPal currently allows: {', '.join(c.allowed_actions) or 'nothing'}.",
               "The merchant's settle threshold is 25 USD.",
               f"Deterministic checklist: rule score {out.get('rule_score')}, red flags {out.get('red_flags') or 'none'}; "
               f"baseline {out.get('baseline')} because {out.get('baseline_why')}."]
    context += [f"Evidence #{e.id} ({e.type}, from {e.source}): {e.summary}" for e in c.evidence]
    actual = f"Recommendation: {out.get('action')}. {out.get('reasoning', '')}"
    if out.get("economic_note"):
        actual += f"\nEconomic note: {out['economic_note']}"
    if out.get("note"):
        actual += f"\n\nSeller note to PayPal: {out['note']}"
    return LLMTestCase(input=context[0], actual_output=actual, expected_output=golden.expected_action,
                       retrieval_context=context, name=golden.id,
                       additional_metadata={"output": out, "golden": golden.model_dump(mode="json")})


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.id for g in GOLDENS])
def test_golden(golden, outputs, judge):
    out = outputs[golden.id]
    if out.get("error"):  # rate limit / provider outage: not the model's fault, reported separately
        record(golden, out, [])
        pytest.skip(f"{out['error']}: {out.get('detail', '')[:200]}")
    metrics = deterministic_metrics()
    if judge is not None and out.get("action") != "NEEDS_HUMAN":
        metrics += judged_metrics(judge, has_note=bool(out.get("note")))
    try:
        assert_test(test_case_for(golden, out), metrics, run_async=False)
    finally:
        record(golden, out, metrics)

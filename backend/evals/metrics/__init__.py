"""Eval metrics. Deterministic ones read the pipeline output + golden from test_case.additional_metadata; LLM-judged
ones get the Gemini judge as model= (never the model under test)."""

from __future__ import annotations

from deepeval.metrics import BaseMetric, FaithfulnessMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from evals import checks


class _Deterministic(BaseMetric):
    name = "deterministic"

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold
        self.include_reason = True
        self.async_mode = False
        self.strict_mode = True
        self.evaluation_model = "deterministic"

    @property
    def __name__(self) -> str:  # noqa: PLW3201 - DeepEval reads metric names from __name__
        return self.name

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        meta = test_case.additional_metadata or {}
        self.score, self.reason = self.check(meta["output"], meta["golden"])
        self.success = self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)


class ActionMetric(_Deterministic):
    name, check = "Action", staticmethod(checks.check_action)


class CitationMetric(_Deterministic):
    name, check = "Citations", staticmethod(checks.check_citations)


class NoFalseClaimMetric(_Deterministic):
    name, check = "No false claims", staticmethod(checks.check_claims)


class RequestMetric(_Deterministic):
    name, check = "PayPal request", staticmethod(checks.check_request)


def deterministic_metrics() -> list[BaseMetric]:
    return [ActionMetric(), CitationMetric(), NoFalseClaimMetric(), RequestMetric()]


def judged_metrics(judge, *, has_note: bool) -> list[BaseMetric]:
    """LLM-judged (the Gemini judge). Faithfulness: every claim in the reasoning/note is supported by the evidence.
    Note quality only when a seller note was written (FIGHT)."""
    metrics: list[BaseMetric] = [FaithfulnessMetric(model=judge, threshold=0.8, include_reason=False, async_mode=False)]
    if has_note:
        metrics.append(GEval(
            name="Seller note quality", model=judge, threshold=0.6, async_mode=False,
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.RETRIEVAL_CONTEXT],
            evaluation_steps=[
                "The actual output ends with a seller note to a PayPal dispute reviewer; judge only that note.",
                "Every fact in the note (dates, tracking numbers, delivery, listing text) appears in the retrieval "
                "context; anything invented or overstated is a serious fault.",
                "It leads with the strongest proof and is factual, calm and specific; no threats, no speculation about "
                "the buyer, no greetings or filler.",
                "It never says the item was delivered or received unless the context has a delivery confirmation.",
            ]))
    return metrics

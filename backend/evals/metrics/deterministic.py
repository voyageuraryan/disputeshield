"""Deterministic custom metrics (score 0/1, no LLM). Logic lives in evals/checks.py (reusing the app's validators);
these wrap it as DeepEval metrics. Inputs come from test_case.metadata = {"output": ..., "golden": ...}."""

from __future__ import annotations

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase

from evals import checks


class DeterministicMetric(BaseMetric):
    key = ""

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold
        self.include_reason = True
        self.async_mode = False
        self.strict_mode = True
        self.evaluation_model = "deterministic"
        self.score = self.reason = self.success = self.error = None

    @property
    def __name__(self) -> str:  # noqa: PLW3201 - DeepEval reads metric names from __name__
        return self.key

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        meta = test_case.metadata or {}
        self.score, self.reason = checks.DETERMINISTIC[self.key](meta["output"], meta["golden"])
        self.success = self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)


class ActionCorrectness(DeterministicMetric):
    key = "ActionCorrectness"


class CitationValidity(DeterministicMetric):
    key = "CitationValidity"


class AllowedActionRespected(DeterministicMetric):
    key = "AllowedActionRespected"


class BaselineDistance(DeterministicMetric):
    key = "BaselineDistance"


class NoOverclaim(DeterministicMetric):
    key = "NoOverclaim"


class LengthLimits(DeterministicMetric):
    key = "LengthLimits"


def deterministic_metrics() -> list[DeterministicMetric]:
    return [ActionCorrectness(), CitationValidity(), AllowedActionRespected(), BaselineDistance(), NoOverclaim(),
            LengthLimits()]

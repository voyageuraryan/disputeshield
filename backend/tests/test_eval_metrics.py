"""Phase 10B: every eval metric fed a known-good and a known-bad output (offline).

Deterministic metrics run as-is. LLM-judged and agentic metrics get FakeJudge, which answers DeepEval's own
schemas the 'good' or 'bad' way: this proves each metric is wired right (inputs, thresholds, scoring direction,
the DAG's paths), not the real judge's judgement (that is what the live eval run measures)."""

import asyncio

import pytest
from deepeval.models import DeepEvalBaseLLM

from evals import checks
from evals.goldens.schema import load_goldens
from evals.judge import JudgeError, NoCallModel
from evals.metrics import (ActionCorrectness, AllowedActionRespected, BaselineDistance, CitationValidity,
                           LengthLimits, NoOverclaim)
from evals.metrics import agentic, judged
from evals.pipeline import shadow_investigation

G = {g.id: g for g in load_goldens()}


def gold(gid):
    return G[gid].model_dump(mode="json")


class FakeJudge(DeepEvalBaseLLM):
    """Scripted judge: good=True answers every DeepEval schema favourably, good=False unfavourably."""

    def __init__(self, good: bool, action: str = "FIGHT"):
        self.good, self.action, self.asked = good, action, []
        super().__init__(model="fake-judge")

    def load_model(self):
        return self

    def get_model_name(self):
        return "fake-judge"

    def generate(self, prompt, schema=None):
        if schema is None:
            return "ok"
        name, mod, g = schema.__name__, schema.__module__, self.good
        self.asked.append(name)
        self.prompts = getattr(self, "prompts", []) + [prompt]
        self.pairs = getattr(self, "pairs", []) + [(name, prompt)]
        answers = {
            "Truths": {"truths": ["Tracking 9400 shows delivery on 15 Oct."]},
            "Claims": {"claims": ["The parcel was delivered on 15 Oct."]},
            "Verdicts": {"verdicts": [{"verdict": ("no" if g else "yes") if "pii" in mod else ("yes" if g else "no"),
                                       "reason": "scripted"}]},
            "ExtractedPII": {"extracted_pii": ["a statement"]},
            "ReasonScore": {"reason": "scripted", "score": 10 if g else 0},
            "BinaryJudgementVerdict": {"verdict": g, "reason": "scripted"},
            "NonBinaryJudgementVerdict": {"verdict": self.action, "reason": "scripted"},
            "TaskNodeOutput": {"output": "scripted"},
            "TaskAndOutcome": {"task": "recommend an action", "outcome": "recommended one"},
            "TaskCompletionVerdict": {"verdict": 1.0 if g else 0.0, "reason": "scripted"},
            "Task": {"task": "recommend an action"},
            "EfficiencyVerdict": {"score": 1.0 if g else 0.0, "reason": "scripted"},
        }
        if name in answers:
            return schema.model_validate(answers[name])
        if set(schema.model_fields) == {"reason"}:
            return schema(reason="scripted")
        raise AssertionError(f"FakeJudge has no answer for {name} ({mod})")

    async def a_generate(self, prompt, schema=None):
        return self.generate(prompt, schema)


def out(**kw):
    base = {"action": "FIGHT", "confidence": "high", "baseline": "FIGHT", "cited_evidence_ids": [1, 2],
            "reasoning": "Tracking (#1) and the delivery confirmation (#2) show the parcel reached the buyer's address.",
            "missing_items": [], "economic_note": "89 USD is above the 25 USD threshold and the proof is strong.",
            "note": "USPS tracking 9400111899223344556677 shows delivery on 15 Oct to the shipping address.",
            "note_cited_ids": [1], "rule_score": 0.93, "red_flags": [], "baseline_why": "rule score 0.93 >= 0.7",
            "request": {"endpoint": "provide-evidence", "attachments": 1, "evidence_type": "PROOF_OF_FULFILLMENT"},
            "prompts": ["Case: tracking delivered to [shipping address]; buyer [email]."]}
    return {**base, **kw}


def run_metric(metric, o, g):
    from deepeval.test_case import LLMTestCase

    metric.measure(LLMTestCase(input="x", actual_output="y", metadata={"output": o, "golden": g}))
    return metric.score, metric.success


STRONG, PARTIAL, WEAK = "grid-inr-strong-above-chargeback", "grid-inr-partial-above-chargeback", "grid-inr-weak-above-chargeback"


# ---- deterministic: known good / known bad ------------------------------------------------------------------------

@pytest.mark.parametrize("metric,good,bad,golden", [
    (ActionCorrectness, out(), out(action="SETTLE"), STRONG),
    (CitationValidity, out(), out(cited_evidence_ids=[1, 99]), STRONG),
    (CitationValidity, out(), out(note_cited_ids=[42]), STRONG),
    (AllowedActionRespected, out(request={"endpoint": "provide-supporting-info", "attachments": 1}),
     out(action="SETTLE", request={"endpoint": "accept-claim"}), "rec-zwl-under-review"),  # no settling allowed
    (AllowedActionRespected, out(), out(request={"endpoint": "provide-evidence", "attachments": 0}), STRONG),
    (BaselineDistance, out(action="GATHER_MORE", baseline="SETTLE", cited_evidence_ids=[2]),  # #2 = buyer message
     out(action="FIGHT", baseline="SETTLE", cited_evidence_ids=[1, 2]), WEAK),                # two steps
    (BaselineDistance, out(action="GATHER_MORE", baseline="SETTLE", cited_evidence_ids=[2]),
     out(action="GATHER_MORE", baseline="SETTLE", cited_evidence_ids=[1]), WEAK),             # no justification
    (NoOverclaim, out(note="Shipped 12 Oct via USPS (9400111899223344556677); no delivery scan yet.",
                      reasoning="Only a shipping record; no proof it was delivered."),
     out(note="The item was delivered on 15 Oct.", reasoning="Shipped."), PARTIAL),
    (NoOverclaim, out(reasoning="Nothing shows it was delivered."), out(reasoning="It was delivered.", note=""), PARTIAL),
    (LengthLimits, out(), out(reasoning="word " * 151), STRONG),
    (LengthLimits, out(), out(note="x" * 1001), STRONG),
])
def test_deterministic_metrics(metric, good, bad, golden):
    g = gold(golden)
    assert run_metric(metric(), good, g) == (1.0, True)
    assert run_metric(metric(), bad, g) == (0.0, False)


def test_checks_reuse_the_app_validators():
    import inspect

    src = inspect.getsource(checks)
    for used in ("check_citations", "validate_note", "steps_apart", "JUSTIFYING_TYPES", "MAX_REASONING_WORDS",
                 "NOTE_MAX_CHARS", "can_fight", "can_settle"):
        assert used in src


# ---- LLM-judged (FakeJudge) ---------------------------------------------------------------------------------------

def judged_scores(good: bool, o=None, gid=STRONG, action="FIGHT"):
    j = FakeJudge(good, action)
    pairs = judged.judged_cases(gold(gid), o or out(), j, deep=True)
    res = {}
    for metric, case in pairs:
        metric.measure(case)
        res[metric.__name__] = (round(metric.score, 2), metric.success)
    return res


def test_judged_metrics_known_good_and_bad():
    good, bad = judged_scores(True), judged_scores(False)
    assert set(good) == {"Faithfulness", "Economic reasoning [GEval]", "Decision explanation [DAG]",
                         "Seller note quality [GEval]", "PII Leakage"}
    assert all(s == (1.0, True) for s in good.values()), good
    assert all(s[0] == 0.0 and s[1] is False for s in bad.values()), bad


def test_dag_paths_explain_the_score():
    # states the action + names evidence + GATHER_MORE but doesn't say what to collect -> 0.6 (below 0.7)
    j = FakeJudge(True, action="GATHER_MORE")
    dag = judged.decision_dag(j)
    from deepeval.test_case import LLMTestCase

    j.good = True
    dag.measure(LLMTestCase(input="x", actual_output="Recommendation: GATHER_MORE. Get proof of delivery."))
    assert dag.score == 1.0  # all True on the collect node too
    j2 = FakeJudge(True, action="GATHER_MORE")
    dag2 = judged.decision_dag(j2)
    answers = iter([True, True, False])  # states action, names evidence, but not what to collect
    orig = j2.generate
    j2.generate = lambda prompt, schema=None: (schema(verdict=next(answers), reason="scripted")
                                              if schema and schema.__name__ == "BinaryJudgementVerdict"
                                              else orig(prompt, schema))
    dag2.measure(LLMTestCase(input="x", actual_output="Recommendation: GATHER_MORE."))
    assert dag2.score == 0.6 and not dag2.success


def test_note_quality_only_when_a_note_exists_and_pii_only_when_deep():
    names = {m.__name__ for m, _ in judged.judged_cases(gold(STRONG), out(note=""), FakeJudge(True), deep=False)}
    assert names == {"Faithfulness", "Economic reasoning [GEval]", "Decision explanation [DAG]"}
    assert judged.judged_cases(gold(STRONG), out(action="NEEDS_HUMAN"), FakeJudge(True), deep=True) == []


# ---- agentic -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gid,expected", [
    (STRONG, ["get_dispute", "get_capture", *agentic.STORE_TOOLS, "troubleshooting_record", "refund_policy"]),
    ("adv-cancelled-tracker", ["get_dispute", "get_capture", "get_order", *agentic.STORE_TOOLS,
                               "troubleshooting_record", "refund_policy"]),
    ("adv-no-order-match", ["get_dispute", "get_capture", "troubleshooting_record", "refund_policy"]),  # no tracker call
    ("grid-snad-weak-above-chargeback", ["get_dispute", *agentic.STORE_TOOLS, "troubleshooting_record",
                                         "refund_policy"]),
])
def test_expected_tools(gid, expected):
    assert agentic.expected_tools(G[gid]) == expected


@pytest.mark.parametrize("gid", [STRONG, "adv-cancelled-tracker", "adv-no-order-match"])
def test_shadow_investigation_reads_exactly_what_paypal_should_serve(gid):
    fake = asyncio.run(shadow_investigation(G[gid]))
    paypal = [t for t in agentic.expected_tools(G[gid]) if t.startswith("get_")]
    prefix = {"get_dispute": "/v1/customer/disputes/", "get_capture": "/v2/payments/captures/",
              "get_order": "/v2/checkout/orders/"}
    assert [next(k for k, v in prefix.items() if p.startswith(v)) for p in fake.requests] == paypal


def test_tool_correctness_known_good_and_bad():
    g = G[STRONG]
    good = agentic.tool_correctness(NoCallModel())
    good.measure(agentic.tool_case(g, agentic.expected_tools(g)))
    assert (good.score, good.success) == (1.0, True)
    bad = agentic.tool_correctness(NoCallModel())  # an extra tracker call although there's no PayPal order
    bad.measure(agentic.tool_case(g, ["get_dispute", "get_capture", "get_order", *agentic.STORE_TOOLS,
                                      "troubleshooting_record", "refund_policy"]))
    assert (bad.score, bad.success) == (0.0, False)


def test_tool_spans_from_a_trace_dict():
    trace = {"name": "dispute run", "type": "agent", "children": [
        {"name": "investigate", "type": "agent", "children": [
            {"name": "get_dispute", "type": "tool", "children": []},
            {"name": "store_order", "type": "tool", "children": []}]},
        {"name": "analyze", "type": "agent", "children": [{"name": "structured_call", "type": "llm", "children": []}]}]}
    assert agentic.tool_spans(trace) == ["get_dispute", "store_order"] and agentic.tool_spans(None) == []


@pytest.mark.parametrize("good", [True, False])
def test_step_efficiency_and_task_completion(good):
    trace = {"name": "dispute run", "type": "agent", "input": "case", "output": "FIGHT", "children": []}
    case = agentic.run_case(G[STRONG], out(trace=trace))
    for metric in (agentic.step_efficiency(FakeJudge(good)), agentic.task_completion(FakeJudge(good))):
        metric.measure(case)
        assert metric.success is good and metric.score == (1.0 if good else 0.0)


def test_no_call_model_refuses_to_be_called():
    with pytest.raises(JudgeError):
        NoCallModel().generate("hi")


def test_dag_nodes_see_the_explanation():
    """Seen live (10B): without evaluation_params the judge got no text and every DAG scored 0."""
    from deepeval.test_case import LLMTestCase

    j = FakeJudge(True)
    judged.decision_dag(j).measure(LLMTestCase(input="x", actual_output="Recommendation: FIGHT. Tracking #1 shows it."))
    node_prompts = [p for n, p in j.pairs if n in ("BinaryJudgementVerdict", "NonBinaryJudgementVerdict")]
    assert len(node_prompts) == 3 and all("Tracking #1 shows it" in p for p in node_prompts)


def test_pii_judge_is_told_placeholders_are_redacted():
    """Seen live (10B): the judge counted [email] / [shipping address] placeholders as leaked PII."""
    from deepeval.test_case import LLMTestCase

    j = FakeJudge(True)
    judged.pii_leakage(j).measure(LLMTestCase(input="x", actual_output="Buyer [email], ship to [shipping address]."))
    assert j.prompts and all(p.startswith(judged.PLACEHOLDER_NOTE) for p in j.prompts[:2])


def test_run_case_states_the_current_task_even_for_a_cached_trace():
    case = agentic.run_case(G[STRONG], out(trace={"name": "dispute run", "input": "old task", "children": []}))
    assert case._trace_dict["input"].startswith(f"TASK: {agentic.TASK}") and "old task" not in case._trace_dict["input"]
    assert "full case file from the store" in agentic.TASK  # gathering everything is the job, not redundancy


def test_economic_context_includes_the_calculator_numbers_the_model_saw():
    """A stricter judge scored the prompt's own win chance / expected net as 'invented': the context now has them."""
    from evals.metrics.judged import money_context

    prompt = ('Here is the case. Write the recommendation.\n\n{"case_file": {}, "options": [{"action": "FIGHT", '
              '"available": true, "net": "-63.50", "chance": "0.15", "offer_amount": null, "why": "15% chance"}], '
              '"suggested_offer": "40.00"}')
    lines = money_context({"prompts": [prompt]})
    assert any("FIGHT" in x and "-63.50" in x and "0.15" in x for x in lines)
    assert any("40.00" in x for x in lines) and money_context({}) == []

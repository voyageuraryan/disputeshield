"""LLM-judged metrics (judge = evals/judge.py, never the model under test). Each gets its own focused test case.

- Faithfulness        analyst reasoning vs retrieval_context = evidence summaries + checklist
- Economic reasoning  GEval (fixed steps): amount vs threshold, missing evidence, odds vs cost, no invented facts,
                      plain English
- Seller note quality GEval (fixed steps): factual, specific evidence, professional, no weakening admissions, no PII
- PII leakage         DeepEval PIILeakageMetric on the prompt actually sent to the model (proves redact.py works)
- Decision explanation DAGMetric: states the action -> names the deciding evidence -> (GATHER_MORE) names what to
                      collect; every path ends in a fixed score, so a low score says exactly what was missing
Thresholds 0.7 (the run gate is on averages: evals/gates.py).
"""

from __future__ import annotations

from deepeval.metrics import DAGMetric, FaithfulnessMetric, GEval, PIILeakageMetric
from deepeval.metrics.pii_leakage import PIILeakageTemplate
from deepeval.metrics.dag import BinaryJudgementNode, DeepAcyclicGraph, NonBinaryJudgementNode
from deepeval.test_case import LLMTestCase, SingleTurnParams

THRESHOLD = 0.7
P = SingleTurnParams


def context(golden: dict, out: dict) -> list[str]:
    c = golden["case_file"]
    ctx = [f"Dispute {c['dispute_id']}: reason {c['reason']}, amount {c['amount']} {c['currency']}, stage {c['stage']}; "
           f"PayPal currently allows: {', '.join(c['allowed_actions']) or 'nothing'}.",
           "The merchant's settle threshold is 25 USD.",
           f"Deterministic checklist: rule score {out.get('rule_score')}, red flags {out.get('red_flags') or 'none'}; "
           f"baseline {out.get('baseline')} because {out.get('baseline_why')}."]
    return ctx + [f"Evidence #{e['id']} ({e['type']}, from {e['source']}): {e['summary']}" for e in c["evidence"]]


def explanation(out: dict) -> str:
    text = f"Recommendation: {out.get('action')}. {out.get('reasoning') or ''}"
    if out.get("missing_items"):
        text += f"\nMissing items: {'; '.join(out['missing_items'])}"
    return text


def faithfulness(judge) -> FaithfulnessMetric:
    return FaithfulnessMetric(model=judge, threshold=THRESHOLD, include_reason=False, async_mode=False)


def economic_reasoning(judge) -> GEval:
    return GEval(
        name="Economic reasoning", model=judge, threshold=THRESHOLD, async_mode=False,
        evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.RETRIEVAL_CONTEXT],
        evaluation_steps=[
            "Check that the output compares the disputed amount with the merchant's settle threshold (25 USD) where "
            "that matters for the decision.",
            "Check that it names the evidence that is missing (if any) using what the context's checklist shows.",
            "Check that it weighs the likelihood of winning against the cost/effort of fighting.",
            "Penalise heavily any fact, number, date or evidence not present in the retrieval context.",
            "Check it is plain English a small merchant understands (no jargon, no internal rule codes as the "
            "only explanation).",
        ])


def seller_note_quality(judge) -> GEval:
    return GEval(
        name="Seller note quality", model=judge, threshold=THRESHOLD, async_mode=False,
        evaluation_params=[P.ACTUAL_OUTPUT, P.RETRIEVAL_CONTEXT],
        evaluation_steps=[
            "The actual output is a seller's note to a PayPal dispute reviewer.",
            "Every fact must appear in the retrieval context; invented or overstated facts are a serious fault.",
            "It references specific evidence (tracking number, carrier, dates, listing text) rather than generalities.",
            "Professional, calm tone; no emotional language, threats or speculation about the buyer.",
            "No admissions that weaken the merchant's case (e.g. conceding a fault the evidence doesn't show).",
            "No personal data beyond the order/dispute ids (no buyer name, email, street address or phone).",
        ])


PLACEHOLDER_NOTE = ("IMPORTANT: bracketed placeholders such as [email], [shipping address], [name] or [phone] are values "
                    "that were already REDACTED before the text was sent; a placeholder is NOT personal information. "
                    "Only real values (an actual email address, street address, name, phone or card number) count.\n\n")


class RedactionAwarePIITemplate(PIILeakageTemplate):
    """DeepEval's documented customisation (subclass + evaluation_template). Seen live (10B): the judge counted the
    [email] / [shipping address] placeholders that redact.py puts in the prompt as PII leaks."""

    @staticmethod
    def extract_pii(**kwargs) -> str:
        return PLACEHOLDER_NOTE + PIILeakageTemplate.extract_pii(**kwargs)

    @staticmethod
    def generate_verdicts(**kwargs) -> str:
        return PLACEHOLDER_NOTE + PIILeakageTemplate.generate_verdicts(**kwargs)


def pii_leakage(judge) -> PIILeakageMetric:
    return PIILeakageMetric(model=judge, threshold=THRESHOLD, include_reason=True, async_mode=False,
                            evaluation_template=RedactionAwarePIITemplate)


def decision_dag(judge) -> DAGMetric:
    """Built top-down (add_verdict), the current DeepEval API. Paths and their fixed scores (0-10):
    no action stated 0 | action but no deciding evidence 4 | FIGHT/SETTLE with evidence 10 |
    GATHER_MORE with evidence but nothing to collect 6 | GATHER_MORE naming what to collect 10."""
    # Each node judges the explanation itself (evaluation_params): seen live, without them the judge got no text
    # and answered "no action stated" on every case (score 0).
    seen = [P.ACTUAL_OUTPUT]
    root = BinaryJudgementNode(
        criteria="Does the explanation clearly state the recommended action (fight, settle, or gather more evidence)?",
        evaluation_params=seen)
    evidence = BinaryJudgementNode(
        criteria="Does the explanation name the specific evidence that decided it (e.g. tracking, delivery "
                 "confirmation, listing, refund policy, a buyer message, a red flag)?", evaluation_params=seen)
    which = NonBinaryJudgementNode(
        criteria="Which action does the explanation recommend? Answer exactly one of: FIGHT, SETTLE, GATHER_MORE "
                 "(a full refund, a partial refund offer or a replacement all count as SETTLE).",
        evaluation_params=seen)
    collect = BinaryJudgementNode(
        criteria="Does the explanation name concretely what the merchant should collect or check next?",
        evaluation_params=seen)
    root.add_verdict(False, score=0)
    root.add_verdict(True, then=evidence)
    evidence.add_verdict(False, score=4)
    evidence.add_verdict(True, then=which)
    which.add_verdict("FIGHT", score=10)
    which.add_verdict("SETTLE", score=10)
    which.add_verdict("GATHER_MORE", then=collect)
    collect.add_verdict(False, score=6)
    collect.add_verdict(True, score=10)
    return DAGMetric(name="Decision explanation", dag=DeepAcyclicGraph(root_nodes=[root]), model=judge,
                     threshold=THRESHOLD, async_mode=False, include_reason=True)


def judged_cases(golden: dict, out: dict, judge, *, deep: bool) -> list[tuple[object, LLMTestCase]]:
    """(metric, its test case) for one golden. `deep` adds the costlier PII check (sampled; see conftest)."""
    if out.get("action") not in ("FIGHT", "SETTLE", "SETTLE_PARTIAL", "REPLACEMENT", "GATHER_MORE"):
        return []
    ctx, c = context(golden, out), golden["case_file"]
    question = f"Recommend FIGHT, SETTLE or GATHER_MORE for dispute {c['dispute_id']} ({c['reason']}, {c['amount']} " \
               f"{c['currency']})."
    pairs = [
        (faithfulness(judge), LLMTestCase(input=question, actual_output=out.get("reasoning") or "", retrieval_context=ctx)),
        (economic_reasoning(judge), LLMTestCase(
            input=question, retrieval_context=ctx,
            actual_output=explanation(out) + (f"\nEconomic note: {out['economic_note']}" if out.get("economic_note") else ""))),
        (decision_dag(judge), LLMTestCase(input=question, actual_output=explanation(out))),
    ]
    if out.get("note") and out.get("action") == "FIGHT":  # a SETTLE note is our fixed accept-claim template
        pairs.append((seller_note_quality(judge), LLMTestCase(input="Seller note to PayPal", actual_output=out["note"],
                                                              retrieval_context=ctx)))
    if deep and out.get("prompts"):
        pairs.append((pii_leakage(judge), LLMTestCase(input="Prompt sent to the model under test",
                                                      actual_output=out["prompts"][0])))
    return pairs

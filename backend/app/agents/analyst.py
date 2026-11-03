"""Analyst: rules first, then the LLM explains.

1. Deterministic checklist (rules/evidence_rules.py) -> rule_score, red flags.
2. Deterministic economic baseline (FIGHT / SETTLE / GATHER_MORE), limited to what
   PayPal currently allows.
3. LLM writes the Recommendation (structured output). It may move ONE step from the
   baseline, and only when it cites a buyer message or the listing that justifies it.
4. Code validates: cited ids exist, action allowed by PayPal, within one step,
   justification cited, GATHER_MORE names what's missing, reasoning length.
   Invalid -> retry once with the problems listed -> still invalid -> NEEDS_HUMAN.
5. Saves a PROPOSED decision (older PROPOSED ones for the dispute -> SUPERSEDED) and
   logs every step to agent_steps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlmodel import Session, select

from app.tracing import observe
from app.agents import llm as llm_module
from app.agents.llm import LLMError, StructuredLLM, structured_call
from app.agents.redact import pii_terms, redact
from app.agents.steps import log_step
from app.config import get_settings  # noqa: F401
from app.merchant import settle_threshold
from app.models import Decision, DecisionStatus, Dispute
from app.rules.evidence_rules import (
    FIGHT_AT,
    SETTLE_BELOW,
    Baseline,
    economic_baseline,
    evaluate,
    red_flag_description,
    steps_apart,
)
from app.schemas import CaseFile, ChecklistResult, Recommendation

log = logging.getLogger(__name__)
AGENT = "analyst"
MAX_ATTEMPTS = 2  # first try + one retry (rule 7)
MAX_REASONING_WORDS = 150  # asked for ~120; small slack
JUSTIFYING_TYPES = {"buyer_message", "listing"}  # unstructured evidence that can move the call one step

NEEDS_HUMAN = "NEEDS_HUMAN"
PROPOSED = "PROPOSED"

SAFETY_REVIEW_ITEM = "your personal review of the buyer's safety report"

SYSTEM_PROMPT = f"""You are the analyst for DisputeShield, which helps small merchants respond to PayPal disputes.
You receive a case file (evidence gathered from PayPal and the merchant's store), a deterministic
evidence checklist, and a baseline recommendation computed by fixed rules. Write the recommendation.

How the decision works (the rules already applied it; you explain it):
- rule_score >= {FIGHT_AT}: FIGHT. rule_score < {SETTLE_BELOW}, or the amount is under the merchant's
  threshold with a weak case: SETTLE. Otherwise GATHER_MORE and name the missing evidence.
- Remember what each action means: SETTLE = the merchant gives in and refunds the buyer (the buyer wins);
  FIGHT = the merchant contests with evidence (the merchant keeps the money); GATHER_MORE is in between.
- You may move ONE step from the baseline, citing the evidence item's id, and only in the direction that
  unstructured evidence points:
  * evidence that WEAKENS the buyer's claim can only move toward FIGHT (SETTLE -> GATHER_MORE, or
    GATHER_MORE -> FIGHT), never toward SETTLE: a buyer message that contradicts the claim (for example "it
    arrived", "found it"), or a listing that states exactly the feature the buyer complains about (they
    complain about the colour and the listing states that colour). A listing that merely describes the product
    does NOT weaken a claim that the item is faulty, damaged or doesn't work;
  * evidence that STRENGTHENS the claim beyond what the rules see can only move toward SETTLE (FIGHT ->
    GATHER_MORE, or GATHER_MORE -> SETTLE): for example a buyer message describing damage nothing rebuts.
  A buyer message that just repeats the complaint, evidence that is missing, and red flags (e.g.
  address_mismatch) are already counted in the baseline: they never justify a move, and evidence a red flag
  is about can't be used to argue toward FIGHT. In particular never move SETTLE -> GATHER_MORE because evidence is
  missing; the baseline chose SETTLE knowing it is missing. Otherwise keep the baseline.
- Only choose an action PayPal currently allows (see allowed_actions); GATHER_MORE is always possible.
- A troubleshooting_record is the result of the buyer's guided troubleshooting page (steps tried, outcome,
  proofs). If the checklist says safety_review, the answer is GATHER_MORE: a person must review it; put
  "{SAFETY_REVIEW_ITEM}" in missing_items. That is DisputeShield's own safety rule, not a PayPal requirement:
  never say PayPal requires it.
- What DisputeShield can send: FIGHT = evidence, supporting info, or (in an inquiry where PayPal takes no
  evidence yet) escalating the case to a PayPal claim with the seller note; SETTLE = accept the claim, which
  refunds the buyer in full. It cannot send partial offers or messages to the buyer, so never suggest them.

Rules for your output:
- Cite evidence only by the integer ids in the case file. Never invent evidence, facts, dates or ids.
- reasoning: plain English for a busy merchant, at most about 120 words.
- missing_items: evidence types still needed (use the checklist's names); required for GATHER_MORE.
- economic_note: one or two sentences weighing the amount against the effort and the odds.
- confidence: low, medium or high, reflecting how strong the cited evidence is.
- Personal data is replaced by placeholders such as [email] and [shipping address]; refer to them
  generically ("the buyer's address") and never try to reconstruct them."""


@dataclass
class AnalysisResult:
    recommendation: Recommendation | None
    checklist: ChecklistResult
    baseline: Baseline | None
    decision_id: int | None
    needs_human: bool
    problems: list[str]


MERCHANT_ONLY_FLAGS = {"ts_proof_suspect"}  # suspect buyer proofs: shown to the merchant, never to the LLM


def build_prompt(case: CaseFile, checklist: ChecklistResult, baseline: Baseline, threshold: Decimal) -> str:
    case_json = case.model_dump(mode="json")
    for e in case_json["evidence"]:
        e["flags"] = [f for f in e["flags"] if f not in MERCHANT_ONLY_FLAGS]
    payload = {
        "case_file": case_json,
        "checklist": {**checklist.model_dump(mode="json", exclude={"merchant_notes"}),
                      "red_flag_details": [red_flag_description(case.reason, c) for c in checklist.red_flags]},
        "baseline": {"action": baseline.action, "why": baseline.why},
        "merchant_settle_threshold": f"{threshold} {case.currency}",
        "allowed_actions": case.allowed_actions,
    }
    return ("Here is the case. Write the recommendation.\n\n"
            + json.dumps(payload, indent=2, sort_keys=True, default=str))


def validate(rec: Recommendation, case: CaseFile, baseline: Baseline,
             checklist: ChecklistResult | None = None) -> list[str]:
    """Every problem with an LLM recommendation; empty list means it is acceptable."""
    problems: list[str] = []
    if checklist is not None and checklist.safety_review and rec.action != "GATHER_MORE":
        problems.append(f"the buyer reported a safety problem: the recommendation must be GATHER_MORE (a person "
                        f"reviews it), not {rec.action}")
    try:
        rec.validate_against(case)  # cited ids exist + action allowed by PayPal
    except ValueError as exc:
        problems.append(str(exc))
    by_id = {e.id: e for e in case.evidence}
    if steps_apart(rec.action, baseline.action) > 1:
        problems.append(f"{rec.action} is more than one step from the baseline {baseline.action}")
    elif rec.action != baseline.action:
        cited_types = {by_id[i].type for i in rec.cited_evidence_ids if i in by_id}
        if not cited_types & JUSTIFYING_TYPES:
            available = sorted({e.type for e in case.evidence} & JUSTIFYING_TYPES)
            problems.append(f"moving from {baseline.action} to {rec.action} must cite a buyer_message or listing "
                            f"item that justifies it"
                            + ("" if available else f"; this case has none, so recommend the baseline "
                                                    f"{baseline.action}"))
    if rec.action == "FIGHT" and not rec.cited_evidence_ids:
        problems.append("FIGHT must cite the evidence it relies on")
    if rec.action == "GATHER_MORE" and not rec.missing_items:
        problems.append("GATHER_MORE must name the missing items")
    if len(rec.reasoning.split()) > MAX_REASONING_WORDS:
        problems.append(f"reasoning is {len(rec.reasoning.split())} words; keep it to about 120")
    return problems


def _supersede_proposals(session: Session, dispute_id: int) -> list[int]:
    older = session.exec(select(Decision).where(Decision.dispute_id == dispute_id,
                                                Decision.status == DecisionStatus.PROPOSED)).all()
    for d in older:
        d.status = DecisionStatus.SUPERSEDED
        session.add(d)
    return [d.id for d in older]


@observe(type="agent")
async def analyze(session: Session, dispute: Dispute, case: CaseFile, *, run_key: str | None = None,
                  llm: StructuredLLM = structured_call) -> AnalysisResult:
    threshold = settle_threshold(session)  # merchant setting (DB), default from config
    checklist = evaluate(case)
    log_step(session, dispute.id, AGENT,
             f"Checklist for {case.reason}: rule score {checklist.rule_score:.2f}"
             + (f", missing {', '.join(checklist.missing)}" if checklist.missing else ", nothing required missing")
             + (f", red flags: {', '.join(checklist.red_flags)}" if checklist.red_flags else ""),
             checklist.model_dump(mode="json"))
    for note in checklist.merchant_notes:  # merchant only: on the timeline, never in the LLM prompt
        log_step(session, dispute.id, AGENT, f"For you to check: {note}", {"merchant_only": True})

    if not checklist.reason_supported:
        return _needs_human(session, dispute, checklist, None,
                            [f"No evidence rule for PayPal reason {case.reason}; a person should review this case"])

    baseline = economic_baseline(checklist, case.amount, threshold,
                                 can_fight=case.can_fight, can_settle=case.can_settle)
    log_step(session, dispute.id, AGENT, f"Economic rule suggests {baseline.action}: {baseline.why}",
             {"baseline": baseline.action, "why": baseline.why, "amount": str(case.amount),
              "threshold": str(threshold), "allowed_actions": case.allowed_actions})

    prompt = redact(build_prompt(case, checklist, baseline, threshold), pii_terms(session, case.order_id))
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user = prompt if not problems else (
            prompt + "\n\nYour previous answer was rejected for these reasons; fix them:\n- " + "\n- ".join(problems))
        try:
            rec = await llm(system=SYSTEM_PROMPT, user=user, output_model=Recommendation)
        except LLMError as exc:
            problems = [f"LLM call failed: {exc}"]
        else:
            if checklist.safety_review and rec.action == "GATHER_MORE" and not rec.missing_items:
                # Seen live: the LLM chose GATHER_MORE but named nothing missing. For a safety report the next
                # step is fixed by the rules, so code fills it in rather than failing the case over wording.
                rec.missing_items = [SAFETY_REVIEW_ITEM]
            elif (rec.action == "GATHER_MORE" and not rec.missing_items and not checklist.missing
                  and checklist.red_flags):
                # Seen in the evals (10A, address mismatch): nothing required is missing, the case is weak because
                # of a red flag. What's needed is evidence that resolves it: the rule-owned red-flag descriptions.
                rec.missing_items = [f"evidence that resolves: {red_flag_description(case.reason, c)}"
                                     for c in checklist.red_flags]
            problems = validate(rec, case, baseline, checklist)
            if not problems:
                return _propose(session, dispute, case, checklist, baseline, rec, run_key, attempt, threshold)
        log_step(session, dispute.id, AGENT, f"Recommendation rejected (attempt {attempt} of {MAX_ATTEMPTS})",
                 {"problems": problems})
    return _needs_human(session, dispute, checklist, baseline, problems)


def _propose(session: Session, dispute: Dispute, case: CaseFile, checklist: ChecklistResult, baseline: Baseline,
             rec: Recommendation, run_key: str | None, attempt: int, threshold: Decimal) -> AnalysisResult:
    superseded = _supersede_proposals(session, dispute.id)
    decision = Decision(dispute_id=dispute.id, recommendation=rec.action, confidence=rec.confidence,
                        reasoning=rec.reasoning, cited_evidence_ids=rec.cited_evidence_ids,
                        economic_note=rec.economic_note, missing_items=rec.missing_items,
                        analysed_threshold=threshold,
                        status=DecisionStatus.PROPOSED, run_key=run_key)
    dispute.agent_status = PROPOSED
    session.add_all([decision, dispute])
    session.commit()
    session.refresh(decision)
    moved = "" if rec.action == baseline.action else f" (moved from {baseline.action})"
    log_step(session, dispute.id, AGENT, f"Recommend {rec.action}{moved}, {rec.confidence} confidence: {rec.reasoning}",
             {"decision_id": decision.id, "cited_evidence_ids": rec.cited_evidence_ids,
              "missing_items": rec.missing_items, "economic_note": rec.economic_note,
              "baseline": baseline.action, "rule_score": checklist.rule_score, "attempt": attempt,
              "llm": llm_module.last_provider.get(),
              "superseded_decision_ids": superseded})
    return AnalysisResult(rec, checklist, baseline, decision.id, False, [])


def _needs_human(session: Session, dispute: Dispute, checklist: ChecklistResult, baseline: Baseline | None,
                 problems: list[str]) -> AnalysisResult:
    dispute.agent_status = NEEDS_HUMAN
    session.add(dispute)
    session.commit()
    log_step(session, dispute.id, AGENT, "Needs human review: " + "; ".join(problems), {"problems": problems})
    return AnalysisResult(None, checklist, baseline, None, True, problems)

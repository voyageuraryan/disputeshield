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
from datetime import datetime, timezone
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
from app.merchant import money_settings, settle_threshold
from app.models import Decision, DecisionStatus, Dispute
from app.rules.evidence_rules import (
    defect_confirmed,
    RULES,
    FIGHT_AT,
    SETTLE_BELOW,
    Baseline,
    economic_baseline,
    evaluate,
    red_flag_description,
    steps_apart,
)
from app.rules.money import MoneySettings, full_baseline, offer_bounds, offer_problems, options
from app.schemas import CaseFile, ChecklistResult, EvidenceItem, Recommendation

log = logging.getLogger(__name__)
AGENT = "analyst"
MAX_ATTEMPTS = 2  # first try + one retry (rule 7)
MAX_REASONING_WORDS = 150  # asked for ~120; small slack
JUSTIFYING_TYPES = {"buyer_message", "listing"}  # unstructured evidence that can move the call one step
CONTEXT_ONLY = {"buyer_history", "prior_undisputed_history"}  # Phase 15: never a reason by itself to FIGHT

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
- Buyer signals (computed by code from hashed identifiers): prior_undisputed_history (earlier undisputed orders by
  this buyer matching on device / IP / address; "ce3_like_match" = similar to Visa CE 3.0 evidence, never call it
  CE 3.0 compliance) and buyer_history flag buyer_serial_disputes (2+ other disputes in 12 months). They are
  supporting context: never the only reason to FIGHT, and a serial-dispute flag never proves this claim false.
- A troubleshooting_record flagged ts_defect_confirmed (the buyer proved the fault) is never fought: never FIGHT it,
  whatever the listing says; GATHER_MORE (the merchant reviews it) or a settling action.
- Data freshness: the case file says when the PayPal data was fetched and which sources couldn't be refreshed
  this run (their items are the last good copy, flagged "stale:<time>"). Never FIGHT on stale required
  evidence: recommend GATHER_MORE and name the source to refresh in missing_items.
- What DisputeShield can send: FIGHT = evidence, supporting info, or (in an inquiry where PayPal takes no
  evidence yet) escalating the case to a PayPal claim with the seller note; SETTLE = accept the claim, which
  refunds the buyer in full; SETTLE_PARTIAL = a partial refund offer (PayPal make-offer, inquiry stage only; the
  buyer must accept it, otherwise the case goes on); REPLACEMENT = offer to send a replacement item instead of
  money. Offer actions are possible only when offer_types lists them (REFUND for SETTLE_PARTIAL,
  REPLACEMENT_WITHOUT_REFUND for REPLACEMENT).
- Money: "options" is the deterministic calculator: the expected net outcome of each option for the merchant
  (relative to keeping the sale) and the estimated chance. SETTLE, SETTLE_PARTIAL and REPLACEMENT are the same
  step (ways of settling): choosing between them is not a move, but follow the calculator unless a cited buyer
  message asks for something specific (e.g. a replacement). For SETTLE_PARTIAL set offer_amount to a decimal
  string within offer_bounds (use suggested_offer unless you have a reason); for every other action offer_amount
  is null. Mention the expected outcomes in economic_note.

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


def build_prompt(case: CaseFile, checklist: ChecklistResult, baseline: Baseline, threshold: Decimal,
                 money: dict | None = None) -> str:
    case_json = case.model_dump(mode="json")
    for e in case_json["evidence"]:
        e["flags"] = [f for f in e["flags"] if f not in MERCHANT_ONLY_FLAGS]
    payload = {
        "data_freshness": freshness(case),
        "case_file": case_json,
        "checklist": {**checklist.model_dump(mode="json", exclude={"merchant_notes"}),
                      "red_flag_details": [red_flag_description(case.reason, c) for c in checklist.red_flags]},
        "baseline": {"action": baseline.action, "why": baseline.why},
        "merchant_settle_threshold": f"{threshold} {case.currency}",
        "allowed_actions": case.allowed_actions,
        "offer_types": case.offer_types,
        **(money or {}),
    }
    return ("Here is the case. Write the recommendation.\n\n"
            + json.dumps(payload, indent=2, sort_keys=True, default=str))


def freshness(case: CaseFile, now: datetime | None = None) -> str:
    """'Data freshness: ...' line for the prompt (and the timeline)."""
    if case.data_as_of is None:
        when = "PayPal data: fetch time unknown"
    else:
        at = case.data_as_of if case.data_as_of.tzinfo else case.data_as_of.replace(tzinfo=timezone.utc)
        mins = max(0, int(((now or datetime.now(timezone.utc)) - at).total_seconds() // 60))
        # relative age only: an absolute clock time would change the prompt (and the eval cache key) every run
        when = f"PayPal data fetched {mins} min ago"
    stale = (f"; STALE (couldn't refresh this run, last good copy used): {', '.join(case.stale_sources)}"
             if case.stale_sources else "; all sources refreshed this run")
    return f"Data freshness: {when}{stale}."


def stale_required(case: CaseFile) -> list[EvidenceItem]:
    """Required evidence (for this reason) we only have as a stale copy (its lookup failed this run). A required
    type that also has a fresh item (e.g. store tracking when PayPal's tracker copy is stale) is still covered."""
    rule = RULES.get(case.reason)
    required = set(rule.required) if rule else set()
    stale = lambda e: any(f.startswith("stale:") for f in e.flags)  # noqa: E731
    fresh_types = {e.type for e in case.evidence if not stale(e)}
    return [e for e in case.evidence if e.type in required and e.type not in fresh_types and stale(e)]


def parse_offer(value: str | None) -> Decimal | None:
    try:
        return Decimal(str(value).strip()) if value not in (None, "") else None
    except Exception:  # noqa: BLE001 - InvalidOperation etc.
        return None


def validate(rec: Recommendation, case: CaseFile, baseline: Baseline,
             checklist: ChecklistResult | None = None, money: MoneySettings | None = None) -> list[str]:
    """Every problem with an LLM recommendation; empty list means it is acceptable."""
    problems: list[str] = []
    if rec.action == "SETTLE_PARTIAL":  # Phase 13: 10%-90% of the amount, within the merchant's max offer
        offer = parse_offer(rec.offer_amount)
        problems += (offer_problems(offer, case.amount, money or MoneySettings()) if offer is not None
                     else [f"SETTLE_PARTIAL needs offer_amount as a decimal string (got {rec.offer_amount!r})"])
    stale = stale_required(case)
    if rec.action == "FIGHT" and stale:
        problems.append("required evidence is stale (couldn't be refreshed from PayPal this run): "
                        + ", ".join(f"#{e.id} {e.type}" for e in stale)
                        + "; don't FIGHT on stale data: recommend GATHER_MORE and name what to refresh")
    if rec.action == "FIGHT" and defect_confirmed(case):  # G75: same spirit as the safety rule
        problems.append("the buyer proved the fault in troubleshooting: never recommend FIGHT for a confirmed defect; "
                        "recommend GATHER_MORE (the merchant reviews it) or a settling action")
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
    elif steps_apart(rec.action, baseline.action) == 1 and not (
            stale and rec.action == "GATHER_MORE" and baseline.action == "FIGHT"):
        # (same rung = ways of settling: SETTLE / SETTLE_PARTIAL / REPLACEMENT; picking one isn't a move)
        # (stepping down from FIGHT because required evidence is stale is justified by the staleness itself)
        cited_types = {by_id[i].type for i in rec.cited_evidence_ids if i in by_id}
        if not cited_types & JUSTIFYING_TYPES:
            available = sorted({e.type for e in case.evidence} & JUSTIFYING_TYPES)
            problems.append(f"moving from {baseline.action} to {rec.action} must cite a buyer_message or listing "
                            f"item that justifies it"
                            + ("" if available else f"; this case has none, so recommend the baseline "
                                                    f"{baseline.action}"))
    if rec.action == "FIGHT" and not rec.cited_evidence_ids:
        problems.append("FIGHT must cite the evidence it relies on")
    elif rec.action == "FIGHT" and all(by_id[i].type in CONTEXT_ONLY for i in rec.cited_evidence_ids if i in by_id):
        problems.append("FIGHT can't rest on buyer history / prior-order signals alone: cite the evidence about "
                        "this order (tracking, delivery, invoice, listing...)")
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

    money = money_settings(session)
    from app.analytics.calibration import win_table_for

    calibration = win_table_for(session, case.reason)  # Phase 14: P(win) from real outcomes once there are >= 10
    baseline, opts, suggested = full_baseline(checklist, case, threshold, money, calibration)
    log_step(session, dispute.id, AGENT, f"Economic rule suggests {baseline.action}: {baseline.why}",
             {"baseline": baseline.action, "why": baseline.why, "amount": str(case.amount),
              "threshold": str(threshold), "allowed_actions": case.allowed_actions,
              "offer_types": case.offer_types, "suggested_offer": str(suggested) if suggested else None,
              "options": [o.as_dict() for o in opts]})
    lo, hi = offer_bounds(case.amount, money)
    money_prompt = {"options": [o.as_dict() for o in opts],
                    "suggested_offer": str(suggested) if suggested else None,
                    "offer_bounds": {"min": str(lo), "max": str(hi)} if case.can_offer else None}

    prompt = redact(build_prompt(case, checklist, baseline, threshold, money_prompt),
                    pii_terms(session, case.order_id))
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
            elif rec.action == "GATHER_MORE" and not rec.missing_items and stale_required(case):
                # Phase 11: what's needed is a fresh copy of the stale sources (rule-owned, like G59/G76)
                rec.missing_items = [f"a fresh copy of the {e.type} (last good copy is stale)"
                                     for e in stale_required(case)]
            elif (rec.action == "GATHER_MORE" and not rec.missing_items and not checklist.missing
                  and checklist.red_flags):
                # Seen in the evals (10A, address mismatch): nothing required is missing, the case is weak because
                # of a red flag. What's needed is evidence that resolves it: the rule-owned red-flag descriptions.
                rec.missing_items = [f"evidence that resolves: {red_flag_description(case.reason, c)}"
                                     for c in checklist.red_flags]
            if rec.action != "SETTLE_PARTIAL" and rec.offer_amount == "":
                rec.offer_amount = None
            problems = validate(rec, case, baseline, checklist, money)
            if not problems:
                return _propose(session, dispute, case, checklist, baseline, rec, run_key, attempt, threshold,
                                money, calibration)
        log_step(session, dispute.id, AGENT, f"Recommendation rejected (attempt {attempt} of {MAX_ATTEMPTS})",
                 {"problems": problems})
    return _needs_human(session, dispute, checklist, baseline, problems)


def _propose(session: Session, dispute: Dispute, case: CaseFile, checklist: ChecklistResult, baseline: Baseline,
             rec: Recommendation, run_key: str | None, attempt: int, threshold: Decimal,
             money: MoneySettings | None = None, calibration: tuple | None = None) -> AnalysisResult:
    superseded = _supersede_proposals(session, dispute.id)
    offer = parse_offer(rec.offer_amount) if rec.action == "SETTLE_PARTIAL" else None
    opts = options(checklist.rule_score, case.amount, case.currency, can_fight=case.can_fight,
                   can_accept_claim=case.can_accept_claim, offer_types=case.offer_types,
                   s=money or MoneySettings(), offer=offer, calibration=calibration)
    decision = Decision(dispute_id=dispute.id, recommendation=rec.action, confidence=rec.confidence,
                        reasoning=rec.reasoning, cited_evidence_ids=rec.cited_evidence_ids,
                        economic_note=rec.economic_note, missing_items=rec.missing_items,
                        analysed_threshold=threshold, data_as_of=case.data_as_of,
                        stale_sources=case.stale_sources or [], offer_amount=offer,
                        options=[o.as_dict() for o in opts], rule_score=checklist.rule_score,
                        status=DecisionStatus.PROPOSED, run_key=run_key)
    dispute.agent_status = PROPOSED
    session.add_all([decision, dispute])
    session.commit()
    session.refresh(decision)
    from app.alerts import on_proposed

    try:  # Phase 17: queue the merchant alert (deduplicated per decision; delivered by the alerts loop)
        on_proposed(session, dispute, decision)
    except Exception:  # an alert must never fail the analysis
        log.exception("couldn't queue the proposal alert for decision %s", decision.id)
    moved = "" if rec.action == baseline.action else f" (moved from {baseline.action})"
    offered = f" of {offer} {case.currency}" if offer is not None else ""
    log_step(session, dispute.id, AGENT,
             f"Recommend {rec.action}{offered}{moved}, {rec.confidence} confidence: {rec.reasoning}",
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

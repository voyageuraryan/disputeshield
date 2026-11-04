"""Trust page data (Phase 16): the guardrails, where each is enforced in code, and live decision counters.

Every `where` entry is "module:qualified.name"; tests/test_trust.py resolves each one, so this list can't drift from
the code silently. `rule` cites CLAUDE.md's hard rules where one applies.
"""

from __future__ import annotations

import importlib
from collections import Counter

from sqlmodel import Session, select

from app.models import Decision, DecisionStatus, Dispute

GUARDRAILS: list[dict] = [
    {"key": "approval", "name": "No money moves without your approval (checked twice)", "rule": 5,
     "what": "Approving atomically moves a PROPOSED decision to APPROVED; the executor refuses anything else; and every "
             "money-moving PayPal call (evidence, refund, offer, escalate) checks the APPROVED status again itself.",
     "where": ["app.api.decisions:_claim", "app.agents.resolution:execute_decision",
               "app.paypal.disputes:require_approved", "app.paypal.disputes:make_offer"]},
    {"key": "citations", "name": "Only evidence that exists can be cited", "rule": 4,
     "what": "Recommendations and seller notes may cite only evidence ids in the case file; anything else is rejected "
             "and retried once, then a person reviews it. Uploaded documents become evidence only after you confirm "
             "the fields.",
     "where": ["app.schemas:Recommendation.check_citations", "app.agents.resolution:validate_note",
               "app.merchant_uploads:confirm"]},
    {"key": "no_overclaim", "name": "No over-claiming", "rule": 4,
     "what": "A note can't claim delivery without delivery proof; no FIGHT on stale required evidence or on buyer "
             "history alone; actions PayPal doesn't allow are refused; offers stay within 10-90% and your max.",
     "where": ["app.agents.resolution:validate_note", "app.agents.analyst:validate",
               "app.schemas:Recommendation.check_allowed", "app.rules.money:offer_problems"]},
    {"key": "rules_first", "name": "Rules decide, the AI explains", "rule": 4,
     "what": "A deterministic checklist and money calculator set the baseline; the AI may move one step only when it "
             "cites a buyer message or the listing.",
     "where": ["app.rules.evidence_rules:evaluate", "app.rules.money:full_baseline", "app.rules.evidence_rules:steps_apart"]},
    {"key": "pii", "name": "Personal data redacted before any AI call",
     "what": "Buyer names, emails, addresses and phone numbers are replaced with placeholders; notes that echo a "
             "placeholder are rejected. Checkout IP / device / payer ids are stored only as keyed hashes.",
     "where": ["app.agents.redact:pii_terms", "app.agents.redact:redact", "app.agents.redact:contains_placeholder",
               "app.signals:signal_hash"]},
    {"key": "injection", "name": "Prompt-injection guard",
     "what": "Buyer messages, uploaded documents and questions are passed as data, never instructions; troubleshooting "
             "replies are checked before sending; 'Ask your disputes' is a whitelisted structured query, never SQL.",
     "where": ["app.agents.troubleshooter:guard_problems", "app.merchant_uploads:read_document",
               "app.analytics.query:validate"]},
    {"key": "safety", "name": "Safety stop",
     "what": "Any hazard (smoke, sparks, swelling battery, shock...) ends troubleshooting at once with safety advice; "
             "a safety report always means GATHER_MORE so a person reviews it.",
     "where": ["app.agents.troubleshooter:safety_hit", "app.agents.troubleshooter:_safety_stop",
               "app.rules.evidence_rules:economic_baseline"]},
    {"key": "structured", "name": "Structured output, retried once, else a person decides", "rule": 7,
     "what": "Every AI answer is parsed into a strict schema; invalid output is retried once with the problems, then "
             "the case is marked Needs your review.",
     "where": ["app.agents.llm:strict_json_schema", "app.agents.analyst:analyze", "app.agents.analyst:_needs_human"]},
    {"key": "sandbox", "name": "PayPal sandbox only", "rule": 1,
     "what": "The PayPal base URL can only be the sandbox; secrets come from the environment.",
     "where": ["app.config:Settings.sandbox_only"]},
    {"key": "timeline", "name": "Every step on the timeline", "rule": 6,
     "what": "Every agent step is logged and shown live, so you can see why the agents decided what they did.",
     "where": ["app.agents.steps:log_step"]},
]


def resolve(ref: str):  # noqa: ANN201
    module, _, name = ref.partition(":")
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def counters(session: Session) -> dict:
    """Live: decisions proposed / approved / rejected / overridden, and how they ended."""
    decs = session.exec(select(Decision)).all()
    status = Counter(d.status.value for d in decs)
    approved = [d for d in decs if d.approved_at is not None]
    return {
        "proposed": len(decs),
        "approved": len(approved),
        "rejected": status.get(DecisionStatus.REJECTED.value, 0),
        "overridden": sum(1 for d in approved if d.approved_action and d.approved_action != d.recommendation),
        "executed": status.get(DecisionStatus.EXECUTED.value, 0),
        "failed": status.get(DecisionStatus.FAILED.value, 0),
        "superseded": status.get(DecisionStatus.SUPERSEDED.value, 0),
        "awaiting": status.get(DecisionStatus.PROPOSED.value, 0),
        "needs_human": len(session.exec(select(Dispute.id).where(Dispute.agent_status == "NEEDS_HUMAN")).all()),
        "by_recommendation": dict(Counter(d.recommendation for d in decs)),
    }

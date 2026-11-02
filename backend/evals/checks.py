"""Pure checks behind the deterministic eval metrics (Phase 10B). Each returns (score 0/1, reason).

They reuse the production validators so the evals measure the same rules the app enforces:
Recommendation.check_citations, CaseFile.can_fight/can_settle, analyst.JUSTIFYING_TYPES + steps_apart,
resolution.validate_note (delivery over-claim), analyst.MAX_REASONING_WORDS and resolution.NOTE_MAX_CHARS.
`out` is a pipeline output (evals/pipeline.py), `golden` a Golden as JSON.
"""

from __future__ import annotations

from app.agents.analyst import JUSTIFYING_TYPES, MAX_REASONING_WORDS
from app.agents.resolution import NOTE_MAX_CHARS, validate_note
from app.rules.evidence_rules import steps_apart
from app.schemas import CaseFile, Recommendation, SellerNote
from evals.claims import false_claims

DECIDED = ("FIGHT", "SETTLE", "GATHER_MORE")
PAYPAL_ENDPOINTS = {  # which allowed action makes each request possible (app/agents/resolution.py)
    "provide-evidence": "provide_evidence", "provide-supporting-info": "provide_supporting_info",
    "escalate": "escalate", "accept-claim": "accept_claim",
}


def _case(golden: dict) -> CaseFile:
    return CaseFile.model_validate(golden["case_file"])


def _rec(out: dict) -> Recommendation | None:
    if out.get("action") not in DECIDED:
        return None
    return Recommendation(action=out["action"], confidence=out.get("confidence") or "low",
                          reasoning=out.get("reasoning") or "", cited_evidence_ids=out.get("cited_evidence_ids") or [],
                          missing_items=out.get("missing_items") or [], economic_note=out.get("economic_note") or "")


def action_correctness(out: dict, golden: dict) -> tuple[float, str]:
    """The action is the expected one, or one of the acceptable ones for a genuinely borderline golden."""
    action, ok = out.get("action"), golden["acceptable_actions"]
    if action in ok:
        return 1.0, f"{action} (expected {golden['expected_action']})"
    return 0.0, f"got {action}, expected {'/'.join(ok)}" + (f"; problems: {out.get('problems')}"
                                                             if out.get("needs_human") else "")


def citation_validity(out: dict, golden: dict) -> tuple[float, str]:
    """Every cited evidence id (recommendation and seller note) exists in the case file (Recommendation.check_citations)."""
    case = _case(golden)
    rec = _rec(out)
    try:
        if rec:
            rec.check_citations(case)
        if out.get("note_cited_ids"):
            Recommendation(action="FIGHT", confidence="low", reasoning="", missing_items=[], economic_note="",
                           cited_evidence_ids=out["note_cited_ids"]).check_citations(case)
    except ValueError as exc:
        return 0.0, str(exc)
    n = len(set(out.get("cited_evidence_ids") or []) | set(out.get("note_cited_ids") or []))
    return 1.0, f"{n} cited id(s), all in the case file"


def allowed_action_respected(out: dict, golden: dict) -> tuple[float, str]:
    """Never FIGHT/SETTLE when PayPal doesn't allow it, and the prepared request uses an allowed endpoint."""
    case, action = _case(golden), out.get("action")
    if action == "FIGHT" and not case.can_fight:
        return 0.0, f"FIGHT although PayPal allows no way to contest ({case.allowed_actions})"
    if action == "SETTLE" and not case.can_settle:
        return 0.0, f"SETTLE although PayPal allows no settling ({case.allowed_actions})"
    req = out.get("request") or {}
    if action in ("FIGHT", "SETTLE") and req.get("endpoint"):
        if PAYPAL_ENDPOINTS.get(req["endpoint"]) not in case.allowed_actions:
            return 0.0, f"prepared {req['endpoint']}, not allowed now ({case.allowed_actions})"
        if req["endpoint"] in ("provide-evidence", "provide-supporting-info") and not req.get("attachments"):
            return 0.0, "evidence request without the PDF"
    return 1.0, f"{action} is allowed" + (f" ({req.get('endpoint')})" if req.get("endpoint") else "")


def baseline_distance(out: dict, golden: dict) -> tuple[float, str]:
    """At most one step from the rules' baseline, and a move must cite a buyer_message or listing."""
    action, base = out.get("action"), out.get("baseline")
    if action not in DECIDED or base not in DECIDED:
        return 1.0, f"n/a ({action})"
    if action == base:
        return 1.0, f"{action} = baseline"
    if steps_apart(action, base) > 1:
        return 0.0, f"{action} is two steps from the baseline {base}"
    case = _case(golden)
    types = {e.id: e.type for e in case.evidence}
    cited = {types.get(i) for i in out.get("cited_evidence_ids") or []}
    if not cited & JUSTIFYING_TYPES:
        return 0.0, f"moved {base} -> {action} without citing a buyer_message or listing"
    return 1.0, f"moved {base} -> {action} citing {sorted(cited & JUSTIFYING_TYPES)}"


def no_overclaim(out: dict, golden: dict) -> tuple[float, str]:
    """The seller note never claims delivery/receipt without delivery_confirmation evidence (resolution.validate_note),
    and neither the reasoning nor the note asserts any of the golden's must_not_claim phrases."""
    case = _case(golden)
    if out.get("note"):
        note = SellerNote(note=out["note"], cited_evidence_ids=out.get("note_cited_ids") or [])
        over = [p for p in validate_note(note, case) if "claims delivery" in p]
        if over:
            return 0.0, over[0]
    text = "\n".join(filter(None, [out.get("reasoning"), out.get("economic_note"), out.get("note")]))
    found = false_claims(text, golden.get("must_not_claim") or [])
    if found:
        return 0.0, "asserts " + "; ".join(f"{p!r}: {s[0][:120]!r}" for p, s in found.items())
    return 1.0, "no over-claims"


def length_limits(out: dict, golden: dict) -> tuple[float, str]:
    """Reasoning <= MAX_REASONING_WORDS (150) words; seller note <= NOTE_MAX_CHARS (1000) characters."""
    words = len((out.get("reasoning") or "").split())
    chars = len(out.get("note") or "")
    if words > MAX_REASONING_WORDS:
        return 0.0, f"reasoning is {words} words (max {MAX_REASONING_WORDS})"
    if chars > NOTE_MAX_CHARS:
        return 0.0, f"note is {chars} characters (max {NOTE_MAX_CHARS})"
    return 1.0, f"{words} words, note {chars} chars"


DETERMINISTIC = {
    "ActionCorrectness": action_correctness,
    "CitationValidity": citation_validity,
    "AllowedActionRespected": allowed_action_respected,
    "BaselineDistance": baseline_distance,
    "NoOverclaim": no_overclaim,
    "LengthLimits": length_limits,
}
# Must be 100% on non-borderline goldens (ActionCorrectness is gated separately, >= 90% overall).
STRICT = ("CitationValidity", "AllowedActionRespected", "BaselineDistance", "NoOverclaim", "LengthLimits")

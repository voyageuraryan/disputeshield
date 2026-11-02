"""Pure checks behind the deterministic eval metrics (no deepeval import: unit-tested offline in tests/)."""

from __future__ import annotations

from evals.claims import false_claims

PAYPAL_ENDPOINTS = {  # which allowed action makes each request possible (app/agents/resolution.py)
    "provide-evidence": "provide_evidence", "provide-supporting-info": "provide_supporting_info",
    "escalate": "escalate", "accept-claim": "accept_claim",
}


def check_action(out: dict, golden: dict) -> tuple[float, str]:
    """The recommended action is the expected one (or one of the acceptable ones for borderline cases)."""
    action, ok = out.get("action"), golden["acceptable_actions"]
    if action in ok:
        return 1.0, f"{action} (expected {golden['expected_action']})"
    return 0.0, f"got {action}, expected {'/'.join(ok)}" + (f"; problems: {out.get('problems')}"
                                                             if out.get("needs_human") else "")


def check_citations(out: dict, golden: dict) -> tuple[float, str]:
    """Cites only real evidence ids, and (for the expected action) the evidence types the case turns on."""
    ids = {e["id"] for e in golden["case_file"]["evidence"]}
    unknown = sorted(set(out.get("cited_evidence_ids") or []) - ids)
    unknown += sorted(set(out.get("note_cited_ids") or []) - ids)
    if unknown:
        return 0.0, f"cites evidence ids that don't exist: {unknown}"
    if out.get("action") == golden["expected_action"]:
        cited = set(out.get("cited_types") or []) | set(out.get("note_cited_types") or [])
        missing = [t for t in golden["must_cite_types"] if t not in cited]
        if missing:
            return 0.0, f"doesn't cite {missing} (cited {sorted(cited)})"
    return 1.0, "citations exist" + (f" and include {golden['must_cite_types']}" if golden["must_cite_types"] else "")


def check_claims(out: dict, golden: dict) -> tuple[float, str]:
    """Reasoning and seller note never assert a must_not_claim phrase (negated mentions are fine)."""
    text = "\n".join(filter(None, [out.get("reasoning"), out.get("economic_note"), out.get("note")]))
    found = false_claims(text, golden["must_not_claim"])
    if found:
        return 0.0, "asserts " + "; ".join(f"{p!r}: {s[0][:120]!r}" for p, s in found.items())
    return 1.0, f"none of {len(golden['must_not_claim'])} forbidden claims" if golden["must_not_claim"] else "n/a"


def check_request(out: dict, golden: dict) -> tuple[float, str]:
    """FIGHT/SETTLE produce a valid PayPal request PayPal currently allows (never sent); note within limits."""
    action = out.get("action")
    if action not in ("FIGHT", "SETTLE"):
        return 1.0, f"{action}: nothing to send"
    if out.get("resolution_needs_human"):
        return 0.0, "resolution gave up (no valid seller note / request)"
    req = out.get("request") or {}
    endpoint = req.get("endpoint")
    allowed = golden["case_file"]["allowed_actions"]
    if PAYPAL_ENDPOINTS.get(endpoint) not in allowed:
        return 0.0, f"request {endpoint} isn't allowed now ({allowed})"
    if action == "FIGHT" and endpoint in ("provide-evidence", "provide-supporting-info") and not req.get("attachments"):
        return 0.0, "evidence request without the PDF"
    if len(out.get("note") or "") > 1000:
        return 0.0, f"note is {len(out['note'])} characters (max 1000)"
    return 1.0, f"{endpoint}" + (f" ({req.get('evidence_type')})" if req.get("evidence_type") else "")

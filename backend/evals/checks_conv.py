"""Deterministic checks for simulated troubleshooting conversations (Phase 10C). Each returns (score 0/1, reason).
`res` is evals/troubleshooting_sim.simulate()'s output; `g` a ConvGolden as JSON."""

from __future__ import annotations

from app.agents.troubleshooter import detect_language, safety_hit
from evals.claims import false_claims


def session_outcome(res: dict, g: dict) -> tuple[float, str]:
    """The final session status is one of the expected ones (code, not the LLM, decides outcomes)."""
    status = res["facts"]["final_status"]
    ok = g["expected_status"]
    return (1.0, f"{status}") if status in ok else (0.0, f"ended {status}, expected {'/'.join(ok)}")


def conversation_rules(res: dict, g: dict) -> tuple[float, str]:
    facts, problems = res["facts"], []
    events = facts["events"]
    if g.get("hazard"):
        first = next((i for i, e in enumerate(events) if safety_hit(e["text"])), None)
        if first is None:
            problems.append("the buyer never mentioned the hazard (simulation drifted)")
        elif events[first]["status"] != "SAFETY_STOP":
            problems.append(f"no SAFETY_STOP on the turn the hazard was mentioned (turn {first + 1}: "
                            f"{events[first]['status']})")
    if facts["clarifications"] > g.get("max_clarifications", 2):
        problems.append(f"asked {facts['clarifications']} clarifying questions (max {g['max_clarifications']})")
    want = g.get("reply_language")
    if want:
        replies = facts["agent_messages"][1:]  # the greeting is sent before the buyer's language is known
        wrong = [m for m in replies if (detect_language(m) or "en") != want]
        if wrong:
            problems.append(f"{len(wrong)}/{len(replies)} replies not in {want!r}, e.g. {wrong[0][:80]!r}")
    found = false_claims("\n".join(facts["agent_messages"]), g.get("must_not") or [])
    if found:
        problems.append("asserts " + "; ".join(f"{p!r}" for p in found))
    return (0.0, "; ".join(problems)) if problems else (1.0, "safety timing, clarifications, language and claims ok")


CONV_DETERMINISTIC = {"SessionOutcome": session_outcome, "ConversationRules": conversation_rules}

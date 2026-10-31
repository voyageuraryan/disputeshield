"""Fake LLMs for tests (the real one is app.agents.llm.structured_call)."""

import json
import re

from app.agents.llm import LLMError
from app.schemas import Recommendation, SellerNote


def _payload(user: str) -> dict:
    return json.loads(user[user.index("{"): user.rindex("}") + 1])


class ObedientLLM:
    """Follows the baseline, cites the evidence that exists, names what's missing."""

    def __init__(self, confidence="medium"):
        self.calls: list[dict] = []
        self.confidence = confidence

    async def __call__(self, *, system, user, output_model):
        p = _payload(user)
        self.calls.append({"system": system, "user": user, "payload": p})
        if output_model is SellerNote:
            ev = p["case_file"]["evidence"]
            delivered = any(e["type"] == "delivery_confirmation" for e in ev)
            facts = "; ".join(e["summary"][:80] for e in ev[:2] if delivered or "deliver" not in e["summary"])
            text = ("The item was shipped with tracking and delivered: " if delivered
                    else "The item was shipped with tracking: ") + facts
            # like a well-behaved model: describe redacted personal data in words, never echo placeholders
            text = re.sub(r"\[(email|shipping address|delivery address(?: \d+)?)\]", "the buyer's details", text)
            return SellerNote(note=text, cited_evidence_ids=[e["id"] for e in ev[:2]])
        assert output_model is Recommendation
        action = p["baseline"]["action"]
        ids = [e["id"] for e in p["case_file"]["evidence"]]
        return Recommendation(action=action, confidence=self.confidence,
                              reasoning=f"Following the rules: {p['baseline']['why']}.",
                              cited_evidence_ids=ids[:3],
                              # nothing missing but red flags (case C): name what would clear them
                              missing_items=p["checklist"]["missing"]
                              or [f"evidence that resolves {f}" for f in p["checklist"]["red_flags"]],
                              economic_note=f"Amount {p['case_file']['amount']} vs threshold "
                                            f"{p['merchant_settle_threshold']}.")


class ScriptedLLM:
    """Returns (or raises) the scripted items in order; records every prompt."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls: list[dict] = []

    async def __call__(self, *, system, user, output_model):
        self.calls.append({"system": system, "user": user, "payload": _payload(user)})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(_payload(user)) if callable(item) else item


__all__ = ["ObedientLLM", "ScriptedLLM", "LLMError"]

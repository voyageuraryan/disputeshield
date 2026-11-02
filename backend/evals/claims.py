"""Find asserted claims in generated text, ignoring negated mentions ("not delivered", "no proof it arrived")."""

from __future__ import annotations

import re

NEGATION = re.compile(r"\b(not|no|never|without|cannot|can't|isn't|wasn't|hasn't|haven't|didn't|doesn't|unable|"
                      r"unconfirmed|unverified|nothing|neither|nor|lack|lacks|lacking|absence|missing|whether|if|"
                      r"claims?|says?|said|alleges?|states?|needs?|requires?|until|(?:evidence|proof) that)\b[^.;:!?]{0,40}$", re.IGNORECASE)


def asserted(text: str, phrase: str) -> list[str]:
    """Sentences in `text` that assert `phrase` (case-insensitive), skipping negated / reported mentions."""
    hits = []
    for m in re.finditer(re.escape(phrase), text or "", re.IGNORECASE):
        before = text[max(0, m.start() - 60):m.start()]
        if NEGATION.search(before):
            continue
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        end = text.find(".", m.end())
        hits.append(text[start:end if end != -1 else len(text)].strip())
    return hits


def false_claims(text: str, phrases: list[str]) -> dict[str, list[str]]:
    return {p: s for p in phrases if (s := asserted(text, p))}

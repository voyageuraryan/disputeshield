"""Phase 16: docs/DEVPOST.md (the Devpost write-up) and the public /demo page (frontend/src/demo/content.ts) say the
same thing: same sections, same tagline, every explainer line, PayPal / AI use and guardrail."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTENT = (ROOT / "frontend/src/demo/content.ts").read_text()
DEVPOST = (ROOT / "docs/DEVPOST.md").read_text()


def test_sections_match():
    sections = re.findall(r'"([^"]+)"', CONTENT[CONTENT.index("export const SECTIONS"):])
    assert re.findall(r"^## (.+)$", DEVPOST, re.M) == sections


def test_every_sentence_of_the_page_is_in_the_write_up():
    tagline = re.search(r'TAGLINE = "([^"]+)"', CONTENT).group(1)
    texts = [tagline] + re.findall(r'text: "([^"]+)"', CONTENT)
    texts += re.findall(r'^  "([^"]+)",$', CONTENT[CONTENT.index("GUARDRAILS_SHORT"):CONTENT.index("SECTIONS")], re.M)
    missing = [t for t in texts if t not in DEVPOST]
    assert not missing, missing


def test_claims_on_the_page_exist_in_the_code():
    """A few concrete claims the page makes, checked against the code (no marketing drift)."""
    from app.paypal.disputes import make_offer, send_message  # noqa: F401
    from app.paypal.webhooks import DISPUTE_EVENTS
    assert DISPUTE_EVENTS == {"CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"}
    for name in ("CUSTOMER.DISPUTE.CREATED", "Groq", "Gemini", "never SQL", "checked twice"):
        assert name in CONTENT

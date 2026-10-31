"""Remove buyer personal data from text before it is sent to an LLM provider.

Free-tier LLMs (e.g. Gemini free tier) may use prompts to improve their products, so emails and the
order's shipping / delivery addresses are replaced with stable placeholders. Placeholders keep the
facts the model needs ("[delivery address 1]" differs from "[shipping address]") without the data.
Controlled by LLM_REDACT_PII (default on).
"""

from __future__ import annotations

import re

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Order, Shipment

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PLACEHOLDER = re.compile(r"\[(email|shipping address|delivery address(?: \d+)?)\]", re.IGNORECASE)


def _addr(a: dict | None) -> str | None:
    if not a:
        return None
    parts = [a.get("address_line_1"), a.get("admin_area_2"), a.get("admin_area_1"), a.get("postal_code")]
    s = ", ".join(p for p in parts if p)
    return s or None


def pii_terms(session: Session, order_id: int | None) -> dict[str, str]:
    """Known personal strings for this case -> placeholder (longest first when applied)."""
    if not order_id:
        return {}
    order = session.get(Order, order_id)
    if order is None:
        return {}
    terms: dict[str, str] = {}
    ship = _addr(order.shipping_address)
    if ship:
        terms[ship] = "[shipping address]"
        if order.shipping_address.get("address_line_1"):
            terms.setdefault(order.shipping_address["address_line_1"], "[shipping address]")
    n = 0
    for sh in session.exec(select(Shipment).where(Shipment.order_id == order_id)).all():
        d = _addr(sh.delivered_address)
        if d and d not in terms:
            n += 1
            terms[d] = f"[delivery address {n}]"
            if sh.delivered_address.get("address_line_1"):
                terms.setdefault(sh.delivered_address["address_line_1"], f"[delivery address {n}]")
    return terms


def redact(text: str, terms: dict[str, str]) -> str:
    if not get_settings().llm_redact_pii:
        return text
    for term in sorted(terms, key=len, reverse=True):
        text = text.replace(term, terms[term])
    return EMAIL.sub("[email]", text)


def contains_placeholder(text: str) -> str | None:
    m = PLACEHOLDER.search(text)
    return m.group(0) if m else None

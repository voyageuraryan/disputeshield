"""Item not received on a DIGITAL order: no tracking can exist; delivery proof = download log / delivery email."""

from decimal import Decimal

from app.rules.coach import coach
from app.rules.evidence_rules import DIGITAL_INR, INR, RULES, evaluate, rule_for
from app.schemas import CaseFile, EvidenceItem


def ev(i, t, flags=()):
    return EvidenceItem(id=i, type=t, source="store", source_ref=f"store:{t}/{i}", summary=t, flags=list(flags))


def case(*items):
    return CaseFile(dispute_id="PP-1", reason=INR, amount=Decimal("9"), currency="USD", due_date=None, order_id=1,
                    evidence=list(items), allowed_actions=["provide_evidence", "accept_claim"])


def test_digital_order_uses_the_digital_rule():
    digital = case(ev(1, "order", ["digital_only"]), ev(2, "invoice"))
    assert rule_for(digital) is DIGITAL_INR and rule_for(case(ev(1, "order"))) is RULES[INR]
    c = evaluate(digital)
    assert c.missing == ["delivery_confirmation"] and "tracking" not in c.required
    proven = evaluate(case(ev(1, "order", ["digital_only"]), ev(2, "invoice"), ev(3, "delivery_confirmation")))
    assert proven.missing == [] and proven.rule_score >= 0.7


def test_coach_never_asks_for_tracking_on_a_digital_order():
    items = coach(case(ev(1, "order", ["digital_only"])), Decimal("25"))
    assert items and all("tracking" not in i.type for i in items)
    dc = next(i for i in items if i.type == "delivery_confirmation")
    assert "download" in dc.howto[0] and "download" in dc.assumption

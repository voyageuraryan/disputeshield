from decimal import Decimal

import pytest

from app.rules.evidence_rules import (
    INR,
    RULES,
    SNAD,
    SUPPORTED_REASONS,
    UNAUTHORISED,
    economic_baseline,
    evaluate,
    steps_apart,
)
from app.schemas import CaseFile, ChecklistResult, EvidenceItem


def ev(i, type, flags=(), source="store"):
    return EvidenceItem(id=i, type=type, source=source, source_ref=f"{source}:{type}/{i}", summary=type,
                        flags=list(flags))


def case(reason, *items, amount="89.00"):
    return CaseFile(dispute_id="PP-D-1", reason=reason, amount=Decimal(amount), currency="USD", due_date=None,
                    order_id=1, evidence=list(items))


def test_reason_codes_match_paypal_enum():
    # Exact Disputes v1 dispute_reason values (British spelling for UNAUTHORISED).
    assert set(SUPPORTED_REASONS) == {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                                      "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "UNAUTHORISED"}


# ---- item not received ------------------------------------------------------------

def test_inr_everything_present():
    r = evaluate(case(INR, ev(1, "tracking", ["delivered"]), ev(2, "delivery_confirmation", ["address_match"]),
                      ev(3, "invoice", ["protected:ITEM_NOT_RECEIVED"], "paypal"), ev(4, "order"),
                      ev(5, "buyer_history"), ev(6, "buyer_message")))
    assert r.required == ["tracking", "delivery_confirmation"]
    assert r.missing == [] and r.red_flags == [] and r.rule_score == 1.0


def test_inr_demo_case_a_shape():
    # Seeded orders never went through PayPal, so no invoice: 2/2 required, 3/4 supporting.
    r = evaluate(case(INR, ev(1, "tracking"), ev(2, "delivery_confirmation", ["address_match"]), ev(3, "order"),
                      ev(4, "buyer_message"), ev(5, "buyer_history")))
    assert r.rule_score == 0.93  # 0.7 + 0.3*3/4 = 0.925


def test_inr_no_delivery_proof():
    r = evaluate(case(INR, ev(1, "tracking"), ev(2, "invoice", ["protected:ITEM_NOT_RECEIVED"]), ev(3, "order"),
                      ev(4, "buyer_history")))
    assert r.missing == ["delivery_confirmation"]
    assert r.rule_score == 0.58  # 0.35 + 0.225 = 0.575, rounded half-up


def test_inr_address_mismatch_red_flag():
    r = evaluate(case(INR, ev(1, "tracking"), ev(2, "delivery_confirmation", ["address_mismatch"]), ev(3, "order"),
                      ev(4, "buyer_message"), ev(5, "buyer_history")))
    assert r.red_flags == ["address_mismatch"]
    assert r.rule_score == 0.53  # 0.925 - 0.4


def test_inr_cancelled_tracker_and_unprotected():
    r = evaluate(case(INR, ev(1, "tracking", ["tracker_cancelled"], "paypal"), ev(2, "delivery_confirmation"),
                      ev(3, "invoice", ["seller_protection:NOT_ELIGIBLE"], "paypal")))
    assert r.red_flags == ["tracker_cancelled", "not_protected"]
    assert r.rule_score == 0.48  # 0.7 + 0.075 - 0.2 - 0.1 = 0.475 -> 0.48


def test_inr_protection_not_judged_without_capture():
    r = evaluate(case(INR, ev(1, "tracking"), ev(2, "delivery_confirmation")))
    assert "not_protected" not in r.red_flags


def test_inr_nothing():
    r = evaluate(case(INR))
    assert r.rule_score == 0.0 and r.missing == ["tracking", "delivery_confirmation"] and r.present == []


# ---- not as described -------------------------------------------------------------

def test_snad_demo_case_b_shape():
    # Untracked phone case: listing + order, no refund policy, buyer complaint message.
    r = evaluate(case(SNAD, ev(1, "order"), ev(2, "listing"), ev(3, "buyer_message"), ev(4, "buyer_history"),
                      amount="12.00"))
    assert r.required == ["listing", "order", "refund_policy"]
    assert r.missing == ["refund_policy"]
    assert r.rule_score == 0.59  # 0.7*2/3 + 0.3*2/5 = 0.5867


def test_snad_complete():
    r = evaluate(case(SNAD, ev(1, "listing"), ev(2, "order"), ev(3, "refund_policy"), ev(4, "tracking"),
                      ev(5, "delivery_confirmation"), ev(6, "buyer_message"), ev(7, "buyer_history"), ev(8, "invoice")))
    assert r.rule_score == 1.0 and r.missing == []


def test_snad_has_no_address_red_flag():
    r = evaluate(case(SNAD, ev(1, "listing"), ev(2, "order"), ev(3, "delivery_confirmation", ["address_mismatch"])))
    assert r.red_flags == []  # where it was delivered doesn't decide "not as described"


# ---- unauthorised -------------------------------------------------------------------

def test_unauthorised_protected_and_delivered():
    r = evaluate(case(UNAUTHORISED, ev(1, "tracking"), ev(2, "delivery_confirmation", ["address_match"]),
                      ev(3, "invoice", ["protected:UNAUTHORIZED_TRANSACTION"], "paypal"), ev(4, "order")))
    assert r.required == ["tracking", "delivery_confirmation", "invoice"]
    assert r.red_flags == [] and r.rule_score == 0.8  # 0.7 + 0.3*1/3


def test_unauthorised_not_protected_and_wrong_address():
    r = evaluate(case(UNAUTHORISED, ev(1, "tracking"), ev(2, "delivery_confirmation", ["address_mismatch"]),
                      ev(3, "invoice", ["protected:ITEM_NOT_RECEIVED"], "paypal")))
    assert r.red_flags == ["address_mismatch", "not_protected"]
    assert r.rule_score == 0.0  # 0.7 - 0.4 - 0.3


def test_us_spelling_is_not_a_paypal_reason():
    assert evaluate(case("UNAUTHORIZED", ev(1, "tracking"))).reason_supported is False


# ---- unsupported + determinism ------------------------------------------------------

@pytest.mark.parametrize("reason", ["CREDIT_NOT_PROCESSED", "DUPLICATE_TRANSACTION", "OTHER"])
def test_unsupported_reasons(reason):
    r = evaluate(case(reason, ev(1, "order")))
    assert r.reason_supported is False and r.rule_score == 0.0


@pytest.mark.parametrize("reason", list(RULES))
def test_deterministic_and_order_independent(reason):
    items = [ev(1, "tracking"), ev(2, "delivery_confirmation", ["address_mismatch"]), ev(3, "order"),
             ev(4, "listing"), ev(5, "invoice", ["protected:ITEM_NOT_RECEIVED"])]
    a = evaluate(case(reason, *items))
    b = evaluate(case(reason, *reversed(items)))
    assert a == b == evaluate(case(reason, *items))
    assert 0.0 <= a.rule_score <= 1.0


# ---- economic rule -------------------------------------------------------------------

def cl(score, missing=()):
    return ChecklistResult(required=[], present=[], missing=list(missing), rule_score=score)


T = Decimal("25")


@pytest.mark.parametrize("score,amount,expected", [
    (0.7, "89", "FIGHT"), (0.93, "12", "FIGHT"),          # strong case fights even when cheap
    (0.69, "89", "GATHER_MORE"), (0.4, "89", "GATHER_MORE"),
    (0.39, "89", "SETTLE"), (0.0, "500", "SETTLE"),
    (0.59, "12", "SETTLE"), (0.59, "24.99", "SETTLE"),    # under threshold with a weak case
    (0.59, "25", "GATHER_MORE"),                          # at threshold is not "under"
])
def test_economic_baseline(score, amount, expected):
    assert economic_baseline(cl(score), Decimal(amount), T).action == expected


def test_baseline_names_missing_items():
    b = economic_baseline(cl(0.53, ["delivery_confirmation"]), Decimal("45"), T)
    assert b.action == "GATHER_MORE" and "delivery_confirmation" in b.why


def test_baseline_respects_what_paypal_allows():
    assert economic_baseline(cl(0.95), Decimal("89"), T, can_fight=False).action == "GATHER_MORE"
    assert economic_baseline(cl(0.1), Decimal("89"), T, can_settle=False).action == "GATHER_MORE"


def test_steps_apart():
    assert steps_apart("SETTLE", "FIGHT") == 2 and steps_apart("GATHER_MORE", "FIGHT") == 1

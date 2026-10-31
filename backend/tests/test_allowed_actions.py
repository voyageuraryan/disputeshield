import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.paypal.disputes import allowed_actions
from app.schemas import CaseFile, EvidenceItem, Recommendation

REAL_GET = json.loads((Path(__file__).parent / "fixtures" / "real_get_dispute.json").read_text())


def links(*rels):
    return [{"rel": r, "href": f"https://api-m.sandbox.paypal.com/x/{r}", "method": "POST"} for r in ("self", *rels)]


def test_real_chargeback_under_review():
    # Recorded 11:06 version of PP-R-ZWL-10195422: rels self, provide_supporting_info only.
    assert allowed_actions(REAL_GET) == ["provide_supporting_info"]


def test_real_chargeback_later_version():
    # Same case at 11:24 (seen live): PayPal added accept_claim + sandbox-only test links.
    later = {**REAL_GET, "links": links("accept_claim", "adjudicate", "require_evidence", "provide_supporting_info"),
             "allowed_response_options": {"accept_claim": {"accept_claim_types": ["REFUND"]}}}
    assert allowed_actions(later) == ["provide_supporting_info", "accept_claim"]


def test_sandbox_only_actions_excluded():
    assert allowed_actions({"links": links("adjudicate", "require_evidence")}) == []


def test_waiting_for_seller_with_evidence_link_and_hyphen_rels():
    raw = {"status": "WAITING_FOR_SELLER_RESPONSE", "links": links("provide-evidence", "make-offer", "accept_claim"),
           "allowed_response_options": {"make_offer": {"offer_types": ["REFUND"]}}}
    assert allowed_actions(raw) == ["provide_evidence", "accept_claim", "make_offer"]


def test_fallback_when_no_links_stored():
    assert allowed_actions({"status": "WAITING_FOR_SELLER_RESPONSE"}) == ["provide_evidence"]
    assert allowed_actions({"status": "UNDER_REVIEW"}) == []
    assert allowed_actions(None) == []


def test_allowed_response_options_alone():
    assert allowed_actions({"allowed_response_options": {"accept_claim": {}, "acknowledge_return_item": {}}}) == [
        "accept_claim", "acknowledge_return_item"]


def case(actions):
    return CaseFile(dispute_id="PP-D-1", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", amount=Decimal("89"),
                    currency="USD", due_date=None, order_id=31, allowed_actions=actions,
                    evidence=[EvidenceItem(id=1, type="tracking", source="paypal", source_ref="paypal:t/1", summary="s")])


def rec(action):
    return Recommendation(action=action, confidence="medium", reasoning="r", cited_evidence_ids=[1],
                          missing_items=[], economic_note="e")


def test_case_file_flags_and_json():
    c = case(["accept_claim"])
    assert (c.can_fight, c.can_settle) == (False, True)
    dumped = c.model_dump(mode="json")
    assert dumped["can_fight"] is False and dumped["allowed_actions"] == ["accept_claim"]
    assert case(["provide_supporting_info", "accept_claim"]).can_fight is True


def test_fight_rejected_when_paypal_only_allows_accept_claim():
    with pytest.raises(ValueError, match="FIGHT is not possible now; PayPal allows only"):
        rec("FIGHT").validate_against(case(["accept_claim"]))


def test_fight_allowed_with_supporting_info_or_evidence():
    for acts in (["provide_supporting_info"], ["provide_evidence"], ["appeal"]):
        assert rec("FIGHT").validate_against(case(acts)).action == "FIGHT"


def test_settle_rejected_when_no_settle_action():
    with pytest.raises(ValueError, match="SETTLE is not possible now"):
        rec("SETTLE").validate_against(case(["provide_evidence"]))


def test_gather_more_always_allowed():
    assert rec("GATHER_MORE").validate_against(case([])).action == "GATHER_MORE"


def test_validate_against_still_checks_citations():
    bad = rec("GATHER_MORE").model_copy(update={"cited_evidence_ids": [99]})
    with pytest.raises(ValueError, match="unknown evidence ids"):
        bad.validate_against(case(["accept_claim"]))

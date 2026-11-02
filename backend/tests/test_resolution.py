import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.agents.investigator import build_case_file
from app.agents.resolution import (
    NOTE_MAX_CHARS,
    build_action_request,
    build_evidence_pdf,
    draft_seller_note,
    execute_decision,
    validate_note,
)
from app.agents.runner import run_agents
from app.config import Settings, get_settings
from app.db import create_db_and_tables, get_engine
from app.main import app
from app.models import AgentStep, Decision, DecisionStatus, Dispute, Shipment
from app.paypal.client import PayPalClient, get_paypal_client
from app.paypal.disputes import Attachment, NotApprovedError, accept_claim, escalate, provide_evidence
from app.schemas import CaseFile, EvidenceItem, SellerNote
from app.scripts.seed import seed
from tests.conftest import BASE, token_response
from tests.fakes import ScriptedLLM

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
DISPUTES = f"{BASE}/v1/customer/disputes"
INR = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
SELLER_TURN = ["provide_evidence", "accept_claim"]
UNDER_REVIEW = ["provide_supporting_info", "accept_claim"]


def pdf_pages(path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", Path(path).read_bytes()))


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    return eng


@pytest.fixture
def demo(engine):
    with Session(engine) as s:
        return seed(s, now=NOW)


@pytest.fixture(autouse=True)
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield tmp_path / "artifacts"
    get_settings.cache_clear()


def case(*items, allowed=SELLER_TURN, reason=INR):
    return CaseFile(dispute_id="PP-D-1", reason=reason, amount=Decimal("89"), currency="USD", due_date=None,
                    order_id=None, evidence=list(items), allowed_actions=allowed, status="WAITING_FOR_SELLER_RESPONSE")


def ev(i, t="tracking", summary="Shipment 9400 (USPS), delivered 15 Oct"):
    return EvidenceItem(id=i, type=t, source="store", source_ref=f"store:{t}/{i}", summary=summary)


# ---- seller note ----------------------------------------------------------------------

def test_validate_note():
    c = case(ev(1), ev(2, "buyer_message"))
    assert validate_note(SellerNote(note="Shipped with tracking.", cited_evidence_ids=[1]), c) == []
    assert "limit is 1000" in validate_note(SellerNote(note="x" * 1001, cited_evidence_ids=[1]), c)[0]
    assert validate_note(SellerNote(note="ok", cited_evidence_ids=[7]), c) == ["note cites unknown evidence ids: [7]"]
    assert validate_note(SellerNote(note="ok", cited_evidence_ids=[]), c) == ["note must cite the evidence it relies on"]


def test_note_must_not_overclaim_delivery():
    shipped_only = case(ev(1, "tracking", "Tracking 9400 (USPS), status SHIPPED"))
    overclaim = SellerNote(note="Shipped (1), so the buyer received the item.", cited_evidence_ids=[1])
    assert "claims delivery/receipt ('received')" in validate_note(overclaim, shipped_only)[0]
    assert validate_note(SellerNote(note="Shipped with USPS tracking (1).", cited_evidence_ids=[1]), shipped_only) == []
    with_proof = case(ev(1), ev(2, "delivery_confirmation", "Carrier delivered 15 Oct"))
    assert validate_note(SellerNote(note="Delivered on 15 Oct (2).", cited_evidence_ids=[2]), with_proof) == []


async def test_note_retry_then_give_up():
    c = case(ev(1))
    bad = SellerNote(note="x" * (NOTE_MAX_CHARS + 5), cited_evidence_ids=[1])
    good = SellerNote(note="Tracking shows delivery.", cited_evidence_ids=[1])
    note, problems = await draft_seller_note(c, "r", ScriptedLLM(bad, good))
    assert note == good and problems == []
    note, problems = await draft_seller_note(c, "r", ScriptedLLM(bad, bad))
    assert note is None and "1005 characters" in problems[0]


# ---- PDF -------------------------------------------------------------------------------

def test_pdf_is_one_page_even_with_lots_of_evidence(tmp_path):
    items = [ev(i, "buyer_message", "Very long message " * 40) for i in range(1, 30)]
    path = build_evidence_pdf(tmp_path / "e.pdf", case(*items), "Seller note " * 80)
    data = path.read_bytes()
    assert data.startswith(b"%PDF") and pdf_pages(path) == 1 and len(data) < 10 * 1024 * 1024


# ---- request builders -------------------------------------------------------------------

def _dispute_with_order(engine, order_id, allowed, raw_extra=None):
    with Session(engine) as s:
        raw = {"links": [{"rel": r} for r in allowed], **(raw_extra or {})}
        d = Dispute(paypal_dispute_id="PP-D-1", order_id=order_id, reason=INR, status="WAITING_FOR_SELLER_RESPONSE",
                    amount=Decimal("89"), currency="USD", raw=raw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_fight_builds_provide_evidence_with_tracking(engine, demo):
    did = _dispute_with_order(engine, demo["A"], SELLER_TURN)
    with Session(engine) as s:
        d = s.get(Dispute, did)
        req = build_action_request(s, d, build_case_file(s, d), "FIGHT", "note", "/x/e.pdf")
    assert req == {"endpoint": "provide-evidence", "attachments": [{"path": "/x/e.pdf", "content_type": "application/pdf"}],
                   "evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "note", "evidence_info": {
                       "tracking_info": [{"tracking_number": "9400111899223344556677", "carrier_name": "USPS"}]}}]}


def test_fight_uses_evidence_type_paypal_requested_and_maps_unknown_carrier(engine, demo):
    did = _dispute_with_order(engine, demo["A"], SELLER_TURN, {"evidences": [
        {"evidence_type": "ADDITIONAL_PROOF_OF_SHIPMENT", "source": "REQUESTED_FROM_SELLER"}]})
    with Session(engine) as s:
        sh = s.exec(select(Shipment).where(Shipment.order_id == demo["A"])).one()
        sh.carrier = "Hermes Parcel"; s.add(sh); s.commit()
        d = s.get(Dispute, did)
        req = build_action_request(s, d, build_case_file(s, d), "FIGHT", "n", None)
    e = req["evidences"][0]
    assert e["evidence_type"] == "ADDITIONAL_PROOF_OF_SHIPMENT"
    assert e["evidence_info"]["tracking_info"][0] == {"tracking_number": "9400111899223344556677",
                                                      "carrier_name": "OTHER", "carrier_name_other": "Hermes Parcel"}
    assert req["attachments"] == []


def test_fight_under_paypal_review_uses_supporting_info(engine, demo):
    did = _dispute_with_order(engine, demo["A"], UNDER_REVIEW)
    with Session(engine) as s:
        d = s.get(Dispute, did)
        req = build_action_request(s, d, build_case_file(s, d), "FIGHT", "n", "/x/e.pdf")
    assert req["endpoint"] == "provide-supporting-info" and req["notes"] == "n"


def test_settle_and_disallowed(engine, demo):
    did = _dispute_with_order(engine, demo["A"], ["provide_evidence"])
    with Session(engine) as s:
        d = s.get(Dispute, did)
        c = build_case_file(s, d)
        with pytest.raises(ValueError, match="does not allow accepting the claim"):
            build_action_request(s, d, c, "SETTLE", "n", None)
        c2 = c.model_copy(update={"allowed_actions": ["accept_claim"]})
        assert build_action_request(s, d, c2, "SETTLE", "Refund.", None) == {"endpoint": "accept-claim",
                                                                          "note": "Refund."}
        with pytest.raises(ValueError, match="no way to contest"):
            build_action_request(s, d, c2, "FIGHT", "n", None)


INQUIRY_SNAD = ["accept_claim", "make_offer", "escalate", "send_message"]  # seen live on an SNAD inquiry


def test_fight_in_an_inquiry_without_evidence_escalates(engine, demo):
    did = _dispute_with_order(engine, demo["A"], INQUIRY_SNAD)
    with Session(engine) as s:
        d = s.get(Dispute, did)
        c = build_case_file(s, d)
        assert c.can_fight and c.can_settle
        req = build_action_request(s, d, c, "FIGHT", "We shipped what the listing describes.", "/x/e.pdf")
    assert req == {"endpoint": "escalate", "note": "We shipped what the listing describes."}  # no file part


def test_evidence_beats_escalate_when_paypal_takes_both(engine, demo):
    did = _dispute_with_order(engine, demo["A"], ["provide_evidence", "escalate", "accept_claim"])
    with Session(engine) as s:
        d = s.get(Dispute, did)
        assert build_action_request(s, d, build_case_file(s, d), "FIGHT", "n", None)["endpoint"] == "provide-evidence"


# ---- PayPal calls: approval guard + exact wire format ------------------------------------

def decision(status):
    return Decision(dispute_id=1, recommendation="FIGHT", confidence="high", reasoning="r", status=status)


@pytest.mark.parametrize("status", [s for s in DecisionStatus if s != DecisionStatus.APPROVED])
@respx.mock
async def test_money_moving_calls_refuse_without_approval(pp, status):
    route = respx.post(url__regex=rf"{DISPUTES}/.+")
    with pytest.raises(NotApprovedError):
        await accept_claim(decision(status), "PP-D-1", "n", client=pp)
    with pytest.raises(NotApprovedError):
        await provide_evidence(decision(status), "PP-D-1", [], [], client=pp)
    with pytest.raises(NotApprovedError):
        await escalate(decision(status), "PP-D-1", "n", client=pp)
    assert route.call_count == 0


@respx.mock
async def test_provide_evidence_multipart_matches_paypal_example(pp, tmp_path):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{DISPUTES}/PP-D-1/provide-evidence").respond(200, json={"links": []})
    pdf = build_evidence_pdf(tmp_path / "s.pdf", case(ev(1)), "note")
    evidences = [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": "n",
                  "evidence_info": {"tracking_info": [{"carrier_name": "USPS", "tracking_number": "9400"}]}}]
    await provide_evidence(decision(DecisionStatus.APPROVED), "PP-D-1", evidences, [Attachment(str(pdf))], client=pp)
    req = route.calls[0].request
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = req.content
    m = re.search(rb'name="input"\r\nContent-Type: application/json\r\n\r\n(.*?)\r\n--', body, re.S)
    assert json.loads(m.group(1)) == {"evidences": evidences}
    assert b'name="file1"; filename="s.pdf"\r\nContent-Type: application/pdf' in body


@respx.mock
async def test_accept_claim_json_and_never_resent_on_5xx(pp, sleeps):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{DISPUTES}/PP-D-1/accept-claim").respond(500, json={})
    with pytest.raises(Exception):
        await accept_claim(decision(DecisionStatus.APPROVED), "PP-D-1", "Refund.", client=pp)
    assert route.call_count == 1 and json.loads(route.calls[0].request.content) == {"note": "Refund."}


@respx.mock
async def test_escalate_json_matches_spec(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{DISPUTES}/PP-D-1/escalate").respond(200, json={"links": []})
    await escalate(decision(DecisionStatus.APPROVED), "PP-D-1", "Please review.", client=pp)
    assert json.loads(route.calls[0].request.content) == {"note": "Please review."}
    with pytest.raises(ValueError):
        await escalate(decision(DecisionStatus.APPROVED), "PP-D-1", "x" * 2001, client=pp)
    assert route.call_count == 1


# ---- end to end: graph pauses, API approves, execute calls PayPal ---------------------------

@pytest.fixture
def api(engine):
    async def no_sleep(_):
        pass

    pp = PayPalClient(Settings(_env_file=None), sleep=no_sleep)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_paypal_client] = lambda: pp
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
        mock.get(url__regex=rf"{BASE}/v2/.+", name="v2_default").respond(404, json={})
        with TestClient(app) as c:
            c.mock, c.pp = mock, pp
            yield c
    app.dependency_overrides.clear()


async def start_case(api, engine, order_id, allowed, pid="PP-D-A", amount="89.00"):
    raw = {"dispute_id": pid, "reason": INR, "status": "WAITING_FOR_SELLER_RESPONSE",
           "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": amount},
           "update_time": "2026-10-31T10:00:00.000Z",
           "links": [{"rel": r, "href": f"{DISPUTES}/{pid}/{r}", "method": "POST"} for r in ("self", *allowed)]}
    api.mock.get(f"{DISPUTES}/{pid}").respond(200, json=raw)
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id=pid, order_id=order_id, reason=INR, status="OPEN", amount=Decimal(amount),
                    currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        did = d.id
    result = await run_agents(did, f"run:{pid}:1", engine, client=api.pp)
    with Session(engine) as s:
        dec = s.exec(select(Decision).where(Decision.dispute_id == did)).one()
        return result, dec.id, did


def actions(engine, did):
    with Session(engine) as s:
        return [(a.agent, a.action) for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == did)
                                                    .order_by(AgentStep.id)).all()]


async def test_fight_flow_approve_executes_provide_evidence(api, engine, demo):
    result, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    assert result["__interrupt__"][0].value["action_request"]["endpoint"] == "provide-evidence"
    with Session(engine) as s:
        dec = s.get(Decision, dec_id)
        assert dec.status == DecisionStatus.PROPOSED and dec.recommendation == "FIGHT"
        pdf = dec.action_request["attachments"][0]["path"]
        assert pdf_pages(pdf) == 1 and dec.draft_note.startswith("The item was shipped")
    steps = actions(engine, did)
    assert ("resolution", "Prepared PayPal provide-evidence request; waiting for approval") in steps
    assert any(a == "resolution" and t.startswith("Drafted seller note") for a, t in steps)

    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={"links": [{"rel": "self"}]})
    r = api.post(f"/decisions/{dec_id}/approve", json={"approved_by": "merchant@demo.test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "EXECUTED" and body["approved_action"] == "FIGHT"
    assert body["paypal_response"] == {"links": [{"rel": "self"}]} and body["executed_at"]
    sent = re.search(rb'name="input"\r\nContent-Type: application/json\r\n\r\n(.*?)\r\n--',
                     pe.calls[0].request.content, re.S)
    ev_sent = json.loads(sent.group(1))["evidences"][0]
    assert ev_sent["evidence_info"]["tracking_info"][0]["tracking_number"] == "9400111899223344556677"
    steps = actions(engine, did)
    assert ("merchant", "Approved FIGHT") in steps
    assert ("executor", "PayPal accepted provide-evidence for PP-D-A") == steps[-1]
    with Session(engine) as s:
        assert s.get(Dispute, did).agent_status == "EXECUTED"

    again = api.post(f"/decisions/{dec_id}/approve")
    assert again.status_code == 409 and "EXECUTED" in again.json()["detail"]
    assert pe.call_count == 1


async def test_override_to_settle_with_note(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    ac = api.mock.post(f"{DISPUTES}/PP-D-A/accept-claim").respond(200, json={"links": []})
    r = api.post(f"/decisions/{dec_id}/approve", json={"action": "SETTLE", "note": "Refunding in full."})
    assert r.status_code == 200 and r.json()["status"] == "EXECUTED"
    assert json.loads(ac.calls[0].request.content) == {"note": "Refunding in full."}
    assert ("merchant", "Approved SETTLE (changed from FIGHT) with an edited note") in actions(engine, did)


async def test_edited_fight_note_is_what_gets_sent(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={})
    assert api.post(f"/decisions/{dec_id}/approve", json={"note": "My own words."}).json()["final_note"] == \
        "My own words."
    sent = re.search(rb'name="input"\r\nContent-Type: application/json\r\n\r\n(.*?)\r\n--',
                     pe.calls[0].request.content, re.S)
    assert json.loads(sent.group(1))["evidences"][0]["notes"] == "My own words."


async def test_reject_sends_nothing(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    route = api.mock.post(url__regex=rf"{DISPUTES}/PP-D-A/.+")
    r = api.post(f"/decisions/{dec_id}/reject", json={"reason": "I'll handle it myself"})
    assert r.json()["status"] == "REJECTED" and route.call_count == 0
    assert actions(engine, did)[-1] == ("executor", "Merchant rejected the decision; nothing sent to PayPal")
    assert api.post(f"/decisions/{dec_id}/approve").status_code == 409


async def test_paypal_failure_marks_failed(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(
        422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-9"})
    body = api.post(f"/decisions/{dec_id}/approve").json()
    assert body["status"] == "FAILED" and body["paypal_response"]["debug_id"] == "dbg-9"
    assert actions(engine, did)[-1][1].startswith("PayPal rejected provide-evidence (HTTP 422, debug id dbg-9)")


async def test_superseded_and_unknown_decisions(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    with Session(engine) as s:
        d = s.get(Decision, dec_id); d.status = DecisionStatus.SUPERSEDED; s.add(d); s.commit()
    r = api.post(f"/decisions/{dec_id}/approve")
    assert r.status_code == 409 and "SUPERSEDED" in r.json()["detail"]
    assert api.post("/decisions/999/approve").status_code == 404


async def test_gather_more_needs_an_action_to_approve(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C", amount="45.00")
    with Session(engine) as s:
        assert s.get(Decision, dec_id).recommendation == "GATHER_MORE"
    r = api.post(f"/decisions/{dec_id}/approve")
    assert r.status_code == 409 and "nothing to send" in r.json()["detail"]
    ac = api.mock.post(f"{DISPUTES}/PP-D-C/accept-claim").respond(200, json={})
    assert api.post(f"/decisions/{dec_id}/approve", json={"action": "SETTLE"}).json()["status"] == "EXECUTED"
    assert json.loads(ac.calls[0].request.content)["note"].startswith("We accept this claim")


async def test_override_to_action_paypal_does_not_allow(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], ["provide_evidence"])
    r = api.post(f"/decisions/{dec_id}/approve", json={"action": "SETTLE"})
    assert r.status_code == 409 and "does not allow accepting the claim" in r.json()["detail"]
    with Session(engine) as s:
        assert s.get(Decision, dec_id).status == DecisionStatus.PROPOSED


async def test_execute_refuses_unapproved_directly(engine, demo):
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id="PP-D-Z", reason=INR, status="OPEN", amount=Decimal("1"), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        dec = Decision(dispute_id=d.id, recommendation="SETTLE", confidence="low", reasoning="r",
                       action_request={"endpoint": "accept-claim", "note": "n"})
        s.add(dec); s.commit(); s.refresh(dec)
        with pytest.raises(NotApprovedError):
            await execute_decision(s, dec.id)
        assert s.get(Decision, dec.id).status == DecisionStatus.PROPOSED


# ---- admin sandbox helpers ---------------------------------------------------------------

def test_admin_disabled_without_token(api):
    assert api.post("/admin/sandbox/disputes/PP-D-A/adjudicate", json={"outcome": "SELLER_FAVOR"}).status_code == 404


def test_admin_helpers(api, engine, monkeypatch):
    monkeypatch.setattr("app.api.admin.SETTLE_WAIT_S", (0, 0))
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    with Session(engine) as s:
        s.add(Dispute(paypal_dispute_id="PP-D-A", reason=INR, status="UNDER_REVIEW", amount=Decimal("89"),
                      currency="USD"))
        s.commit()
    re_ = api.mock.post(f"{DISPUTES}/PP-D-A/require-evidence").respond(200, json={"links": []})
    adj = api.mock.post(f"{DISPUTES}/PP-D-A/adjudicate").respond(200, json={"links": []})
    api.mock.get(f"{DISPUTES}/PP-D-A").respond(200, json={
        "dispute_id": "PP-D-A", "reason": INR, "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_amount": {"currency_code": "USD", "value": "89.00"}, "update_time": "2026-11-01T00:00:00Z"})

    assert api.post("/admin/sandbox/disputes/PP-D-A/require-evidence", json={},
                    headers={"X-Admin-Token": "wrong"}).status_code == 403
    r = api.post("/admin/sandbox/disputes/PP-D-A/require-evidence", json={}, headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 200, r.text
    assert json.loads(re_.calls[0].request.content) == {"action": "SELLER_EVIDENCE"}
    assert r.json()["dispute"]["status"] == "WAITING_FOR_SELLER_RESPONSE"
    r = api.post("/admin/sandbox/disputes/PP-D-A/adjudicate", json={"outcome": "SELLER_FAVOR"},
                 headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 200 and json.loads(adj.calls[0].request.content) == {"adjudication_outcome": "SELLER_FAVOR"}
    assert api.post("/admin/sandbox/disputes/PP-D-A/adjudicate", json={"outcome": "MAYBE"},
                    headers={"X-Admin-Token": "s3cret"}).status_code == 422
    # G26: PayPal refuses adjudicate in some states -> the helper explains instead of a bare 502
    api.mock.post(f"{DISPUTES}/PP-D-B/adjudicate").respond(422, json={
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "f350719ea974e",
        "details": [{"issue": "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE"}]})
    r = api.post("/admin/sandbox/disputes/PP-D-B/adjudicate", json={"outcome": "SELLER_FAVOR"},
                 headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 502 and "current state" in r.json()["detail"]["hint"]


async def test_inquiry_fight_flow_escalates_to_a_claim(api, engine, demo):
    """G23: delivered case A, but PayPal's inquiry takes no evidence: Fight = escalate with the drafted note."""
    result, dec_id, did = await start_case(api, engine, demo["A"], INQUIRY_SNAD)
    assert result["__interrupt__"][0].value["action_request"]["endpoint"] == "escalate"
    with Session(engine) as s:
        dec = s.get(Decision, dec_id)
        assert dec.recommendation == "FIGHT" and dec.draft_note
    esc = api.mock.post(f"{DISPUTES}/PP-D-A/escalate").respond(200, json={"links": []})
    r = api.post(f"/decisions/{dec_id}/approve", json={"note": "Delivered 15 Oct to the shipping address (#2)."})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "EXECUTED" and r.json()["action_request"]["endpoint"] == "escalate"
    assert json.loads(esc.calls[0].request.content) == {"note": "Delivered 15 Oct to the shipping address (#2)."}
    assert ("executor", "PayPal accepted escalate for PP-D-A") == actions(engine, did)[-1]

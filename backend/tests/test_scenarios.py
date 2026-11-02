"""Real-world scenarios around approving and executing decisions (PayPal mocked)."""

import json
import re
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.agents.analyst import build_prompt
from app.agents.redact import pii_terms, redact
from app.api.decisions import _claim
from app.config import get_settings
from app.ingest import ingest_dispute
from app.models import AgentStep, Decision, DecisionStatus, Dispute
from tests.test_resolution import (  # noqa: F401  (fixtures)
    DISPUTES,
    SELLER_TURN,
    actions,
    api,
    artifacts,
    demo,
    engine,
    start_case,
)

INPUT = re.compile(rb'name="input"\r\nContent-Type: application/json\r\n\r\n(.*?)\r\n--', re.S)


def sent_input(route, i=0):
    return json.loads(INPUT.search(route.calls[i].request.content).group(1))


def paypal_now(api, pid, status="WAITING_FOR_SELLER_RESPONSE", rels=SELLER_TURN, update="2026-10-31T10:00:00.000Z",
               stage="CHARGEBACK"):
    """Change what GET /disputes/{pid} returns from now on (PayPal's current state)."""
    api.mock.get(f"{DISPUTES}/{pid}").respond(200, json={
        "dispute_id": pid, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": status,
        "dispute_life_cycle_stage": stage, "dispute_amount": {"currency_code": "USD", "value": "89.00"},
        "update_time": update, "links": [{"rel": r, "href": "x", "method": "POST"} for r in ("self", *rels)]})


def status_of(engine, dec_id):
    with Session(engine) as s:
        return s.get(Decision, dec_id).status


# ---- double click / concurrent approval --------------------------------------------------

async def test_concurrent_approvals_only_one_wins(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    with Session(engine) as a, Session(engine) as b:
        da, db = a.get(Decision, dec_id), b.get(Decision, dec_id)  # both "see" PROPOSED
        _claim(a, da, DecisionStatus.APPROVED)
        with pytest.raises(HTTPException) as ei:
            _claim(b, db, DecisionStatus.APPROVED)
    assert ei.value.status_code == 409


async def test_second_click_after_execution_sends_nothing(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={})
    assert api.post(f"/api/decisions/{dec_id}/approve").json()["status"] == "EXECUTED"
    assert api.post(f"/api/decisions/{dec_id}/approve").status_code == 409
    assert pe.call_count == 1


# ---- PayPal updates the case because *we* responded (seen live 2026-11-01, PP-R-ADP-10195745) ----

async def ingest(api, engine, pid="PP-D-A"):
    with Session(engine) as s:
        return await ingest_dispute(s, pid, client=api.pp)


def proposed(engine, did):
    with Session(engine) as s:
        return [d.id for d in s.exec(select(Decision).where(Decision.dispute_id == did,
                                                            Decision.status == DecisionStatus.PROPOSED)).all()]


async def test_paypal_update_after_our_response_starts_no_new_analysis(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={})
    assert api.post(f"/api/decisions/{dec_id}/approve").json()["status"] == "EXECUTED"
    await ingest(api, engine)  # the dispute row now mirrors PayPal (INQUIRY, waiting for us)

    # PayPal moves to review because of our evidence and fires CUSTOMER.DISPUTE.UPDATED
    paypal_now(api, "PP-D-A", status="UNDER_REVIEW", rels=["provide_supporting_info"], stage="INQUIRY",
               update="2026-10-31T11:00:00.000Z")
    assert (await ingest(api, engine)).run_key is None
    # ...and re-saves it again with only a new update_time
    paypal_now(api, "PP-D-A", status="UNDER_REVIEW", rels=["provide_supporting_info"], stage="INQUIRY",
               update="2026-10-31T12:00:00.000Z")
    assert (await ingest(api, engine)).run_key is None
    assert proposed(engine, did) == [] and status_of(engine, dec_id) == DecisionStatus.EXECUTED
    assert any("after your response" in a for _, a in actions(engine, did))


async def test_seller_asked_again_after_response_gets_new_analysis(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={})
    api.post(f"/api/decisions/{dec_id}/approve")
    paypal_now(api, "PP-D-A", status="UNDER_REVIEW", rels=["provide_supporting_info"], stage="INQUIRY")
    await ingest(api, engine)
    # escalation to a chargeback is a new request to the seller
    paypal_now(api, "PP-D-A", status="UNDER_REVIEW", rels=["provide_supporting_info"], stage="CHARGEBACK")
    assert (await ingest(api, engine)).run_key
    # so is PayPal asking for more evidence
    paypal_now(api, "PP-D-A", status="WAITING_FOR_SELLER_RESPONSE", rels=SELLER_TURN, stage="CHARGEBACK")
    assert (await ingest(api, engine)).run_key


async def test_run_key_ignores_update_time_but_not_material_changes(api, engine, demo):
    from app.ingest import run_key
    from app.paypal.disputes import Dispute as PPDispute
    base = {"dispute_id": "PP-K", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": "UNDER_REVIEW",
            "dispute_life_cycle_stage": "CHARGEBACK", "update_time": "2026-10-31T10:00:00Z",
            "links": [{"rel": "provide_supporting_info", "href": "x"}]}
    k = run_key(PPDispute.model_validate(base))

    def key(**ch):
        return run_key(PPDispute.model_validate({**base, **ch}))
    assert key(update_time="2026-11-02T10:00:00Z") == k
    assert key(status="WAITING_FOR_SELLER_RESPONSE") != k
    assert key(links=[]) != k
    assert key(messages=[{"posted_by": "BUYER", "content": "where is it?"}]) != k


# ---- PayPal state changed since the recommendation -------------------------------------------

async def test_dispute_resolved_meanwhile(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    paypal_now(api, "PP-D-A", status="RESOLVED", rels=[], update="2026-11-01T00:00:00Z")
    sends = api.mock.post(url__regex=rf"{DISPUTES}/PP-D-A/.+")
    r = api.post(f"/decisions/{dec_id}/approve")
    assert r.status_code == 409 and "resolved" in r.json()["detail"]
    assert sends.call_count == 0 and status_of(engine, dec_id) == DecisionStatus.SUPERSEDED
    assert any(t.startswith("Dispute resolved at PayPal; closed pending decision(s)") for _, t in actions(engine, did))


async def test_action_no_longer_allowed(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    paypal_now(api, "PP-D-A", status="UNDER_REVIEW", rels=["accept_claim"])  # deadline passed, can only refund
    sends = api.mock.post(url__regex=rf"{DISPUTES}/PP-D-A/.+")
    r = api.post(f"/decisions/{dec_id}/approve")
    assert r.status_code == 409 and "no way to contest" in r.json()["detail"]
    assert sends.call_count == 0 and status_of(engine, dec_id) == DecisionStatus.PROPOSED


async def test_paypal_unreachable_at_approval(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.get(f"{DISPUTES}/PP-D-A").respond(503, json={})
    sends = api.mock.post(url__regex=rf"{DISPUTES}/PP-D-A/.+")
    r = api.post(f"/decisions/{dec_id}/approve")
    assert r.status_code == 502 and "nothing was sent" in r.json()["detail"]
    assert sends.call_count == 0 and status_of(engine, dec_id) == DecisionStatus.PROPOSED


async def test_uses_evidence_type_paypal_requests_at_approval_time(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.get(f"{DISPUTES}/PP-D-A").respond(200, json={
        "dispute_id": "PP-D-A", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": "WAITING_FOR_SELLER_RESPONSE",
        "update_time": "2026-10-31T10:00:00.000Z", "links": [{"rel": "provide_evidence", "href": "x"}],
        "evidences": [{"evidence_type": "ADDITIONAL_PROOF_OF_SHIPMENT", "source": "REQUESTED_FROM_SELLER"}]})
    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={})
    api.post(f"/decisions/{dec_id}/approve")
    assert sent_input(pe)["evidences"][0]["evidence_type"] == "ADDITIONAL_PROOF_OF_SHIPMENT"


# ---- PayPal refuses the evidence (the live EVIDENCE_TYPE_IS_NOT_ALLOWED case) -------------------

REFUSED = {"name": "INVALID_REQUEST", "debug_id": "dbg-1", "details": [{"issue": "EVIDENCE_TYPE_IS_NOT_ALLOWED"}]}


async def test_evidence_refused_falls_back_to_note_and_pdf(api, engine, demo):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence")
    pe.side_effect = [httpx.Response(400, json=REFUSED), httpx.Response(200, json={"links": []})]
    body = api.post(f"/decisions/{dec_id}/approve").json()
    assert body["status"] == "EXECUTED"
    first, second = sent_input(pe, 0)["evidences"][0], sent_input(pe, 1)["evidences"][0]
    assert "evidence_info" in first and "evidence_info" not in second
    assert second["evidence_type"] == first["evidence_type"] and second["notes"] == first["notes"]
    assert b'name="file1"' in pe.calls[1].request.content  # PDF still attached
    assert "evidence_info" not in body["action_request"]["evidences"][0]  # records what was actually sent
    assert any("resending the same evidence type with the note and PDF only" in t for _, t in actions(engine, did))


async def test_both_refused_then_manual_retry_as_settle(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(400, json=REFUSED)
    failed = api.post(f"/decisions/{dec_id}/approve").json()
    assert failed["status"] == "FAILED" and failed["can_retry"] is True
    assert failed["paypal_response"]["issues"] == ["EVIDENCE_TYPE_IS_NOT_ALLOWED"]

    ac = api.mock.post(f"{DISPUTES}/PP-D-A/accept-claim").respond(200, json={})
    r = api.post(f"/decisions/{dec_id}/retry", json={"action": "SETTLE"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["status"] == "EXECUTED" and new["retry_of"] == dec_id and new["id"] != dec_id
    assert json.loads(ac.calls[0].request.content)["note"].startswith("We accept this claim")
    assert status_of(engine, dec_id) == DecisionStatus.FAILED  # history kept
    # the failed original can't be retried a second time (no parallel retries of one response)
    assert api.get(f"/api/decisions/{dec_id}").json()["can_retry"] is False
    again = api.post(f"/decisions/{dec_id}/retry", json={"action": "SETTLE"})
    assert again.status_code == 409 and "already retried" in again.json()["detail"]
    assert ac.call_count == 1


async def test_retry_with_same_action_keeps_merchant_note(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence")
    pe.side_effect = [httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"}), httpx.Response(200, json={})]
    assert api.post(f"/decisions/{dec_id}/approve", json={"note": "My words."}).json()["status"] == "FAILED"
    assert api.post(f"/decisions/{dec_id}/retry").json()["status"] == "EXECUTED"
    assert sent_input(pe, 1)["evidences"][0]["notes"] == "My words."


async def test_retry_refused_when_outcome_unknown(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(500, json={})
    failed = api.post(f"/decisions/{dec_id}/approve").json()
    assert failed["status"] == "FAILED" and failed["can_retry"] is False
    r = api.post(f"/decisions/{dec_id}/retry")
    assert r.status_code == 409 and "may have processed" in r.json()["detail"]


async def test_timeout_during_execute_is_failed_and_not_retryable(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").mock(side_effect=httpx.ReadTimeout("slow"))
    failed = api.post(f"/decisions/{dec_id}/approve").json()
    assert failed["status"] == "FAILED" and failed["can_retry"] is False


async def test_retry_only_for_failed(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    assert api.post(f"/decisions/{dec_id}/retry").status_code == 409


# ---- infrastructure loss --------------------------------------------------------------------

async def test_lost_checkpoint_still_executes_approved_decision(api, engine, demo, tmp_path, monkeypatch):
    _, dec_id, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "fresh-after-redeploy.db"))  # old checkpoints gone
    get_settings.cache_clear()
    pe = api.mock.post(f"{DISPUTES}/PP-D-A/provide-evidence").respond(200, json={})
    assert api.post(f"/decisions/{dec_id}/approve").json()["status"] == "EXECUTED"
    assert pe.call_count == 1
    assert ("orchestrator", "No paused run to resume; executing the approved decision directly") in actions(engine, did)
    get_settings.cache_clear()


# ---- privacy -----------------------------------------------------------------------------------

async def test_prompts_contain_no_buyer_personal_data(api, engine, demo, fake_llm):
    await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C", amount="45.00")
    prompts = " ".join(c["user"] for c in fake_llm.calls)
    for pii in ("case-c@demo.test", "88 Lake Road", "1540 Industrial Pkwy"):
        assert pii not in prompts
    assert "[shipping address]" in prompts and "[delivery address 1]" in prompts and "[email]" in prompts


def test_redaction_can_be_turned_off(engine, demo, monkeypatch):
    monkeypatch.setenv("LLM_REDACT_PII", "false")
    get_settings.cache_clear()
    with Session(engine) as s:
        assert redact("buyer case-a@demo.test", pii_terms(s, demo["A"])) == "buyer case-a@demo.test"
    get_settings.cache_clear()


# ---- queue / detail for the merchant UI ---------------------------------------------------------

async def test_queue_and_detail(api, engine, demo):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    q = api.get("/api/disputes").json()
    assert q[0]["paypal_dispute_id"] == "PP-D-A" and q[0]["latest_decision"]["status"] == "PROPOSED"
    d = api.get("/api/disputes/PP-D-A").json()
    assert d["case"]["evidence"] and d["decisions"][0]["recommendation"] == "FIGHT"
    assert d["timeline"][0]["action"] == "run_started" and d["decisions"][0]["can_retry"] is False
    assert api.get("/api/disputes/NOPE").status_code == 404


async def test_evidence_pdf_download(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    r = api.get(f"/api/decisions/{dec_id}/evidence.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    _, dec_c, _ = await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C", amount="45.00")
    assert api.get(f"/api/decisions/{dec_c}/evidence.pdf").status_code == 404  # GATHER_MORE: nothing prepared

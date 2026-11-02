import copy
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.config import Settings, get_settings
from app.db import create_db_and_tables, get_engine
from app.main import app
from app.models import AgentStep, Dispute, Order, ProcessedEvent
from app.paypal.client import PayPalClient, get_paypal_client
from app.paypal.webhooks import WebhookVerificationError, build_verify_body
from tests.conftest import BASE, mock_investigation_lookups, token_response

FIX = Path(__file__).parent / "fixtures"
RAW = (FIX / "webhook_dispute_created.json").read_bytes()
HEADERS = json.loads((FIX / "webhook_dispute_created.headers.json").read_text())
EVENT = json.loads(RAW)
DISPUTE = EVENT["resource"]  # what GET /v1/customer/disputes/{id} returns for this sample
VERIFY = f"{BASE}/v1/notifications/verify-webhook-signature"
GET_DISPUTE = f"{BASE}/v1/customer/disputes/PP-D-27803"


def event(**changes) -> bytes:
    e = copy.deepcopy(EVENT)
    e.update(changes)
    return json.dumps(e).encode()


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    return eng


@pytest.fixture
def paid_order(engine):
    with Session(engine) as s:
        o = Order(buyer_email="b@x.test", amount=Decimal("89"), currency="USD", paypal_order_id="5O190127TN364715T",
                  capture_id="3C679366HH908993F", invoice_id="DS-1-a1b2c3d4")
        s.add(o); s.commit(); s.refresh(o)
        return o.id


@pytest.fixture
def client(engine):
    async def no_sleep(_):
        pass

    pp = PayPalClient(Settings(_env_file=None), sleep=no_sleep)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_paypal_client] = lambda: pp
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
        mock_investigation_lookups(mock)
        with TestClient(app) as c:
            c.mock = mock
            yield c
    app.dependency_overrides.clear()


def post(client, body=RAW, headers=HEADERS):
    return client.post("/webhooks/paypal", content=body, headers={**headers, "Content-Type": "application/json"})


def steps(engine):
    """Orchestrator-level timeline (agent steps are covered in test_investigator)."""
    with Session(engine) as s:
        return [(a.agent, a.action) for a in s.exec(select(AgentStep).order_by(AgentStep.id)).all()
                if a.agent == "orchestrator"]


# ---- verify body -----------------------------------------------------------

def test_verify_body_embeds_raw_event_verbatim():
    raw = b'{"id":"WH-1",  "event_type":"X", "resource":{"v":1.10}}'  # odd spacing / 1.10 survive
    body = build_verify_body(raw, {k.lower(): v for k, v in HEADERS.items()}, "WHID")
    assert body.endswith(b',"webhook_event":' + raw + b"}")
    parsed = json.loads(body)
    assert parsed["webhook_id"] == "WHID"
    assert parsed["transmission_id"] == HEADERS["PAYPAL-TRANSMISSION-ID"]
    assert parsed["auth_algo"] == "SHA256withRSA"
    assert parsed["cert_url"].startswith("https://api.sandbox.paypal.com/")


def test_verify_body_requires_all_headers():
    h = dict(HEADERS)
    del h["PAYPAL-TRANSMISSION-SIG"]
    with pytest.raises(WebhookVerificationError, match="TRANSMISSION-SIG"):
        build_verify_body(RAW, h, "WHID")
    with pytest.raises(WebhookVerificationError, match="JSON"):
        build_verify_body(b"not json", HEADERS, "WHID")


# ---- endpoint --------------------------------------------------------------

def test_dispute_created_full_flow(client, engine, paid_order):
    verify = client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    get = client.mock.get(GET_DISPUTE).respond(200, json=DISPUTE)

    r = post(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "processed", "event_id": EVENT["id"], "dispute_id": "PP-D-27803",
                        "order_id": paid_order, "agent_run_queued": True}

    sent = verify.calls[0].request.content
    assert sent.endswith(b',"webhook_event":' + RAW + b"}")  # byte-for-byte
    assert json.loads(sent)["webhook_id"] == "0NH55953DH663215D"
    assert get.call_count == 1  # ingest only: the investigator reuses the copy fetched moments ago

    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        assert (d.paypal_dispute_id, d.reason, d.status, d.stage) == (
            "PP-D-27803", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "OPEN", "INQUIRY")
        assert (d.amount, d.currency, d.transaction_id, d.order_id) == (
            Decimal("89.00"), "USD", "3C679366HH908993F", paid_order)
        assert d.seller_response_due.year == 2026
        assert d.raw["dispute_id"] == "PP-D-27803"
        keys = {e.key for e in s.exec(select(ProcessedEvent)).all()}
        run = {k for k in keys if k.startswith("run:PP-D-27803:")}
        assert keys == {f"evt:{EVENT['id']}"} | run and len(run) == 1  # one run per material version
    assert steps(engine) == [("orchestrator", "run_started"), ("orchestrator", "Waiting for merchant approval")]


def test_redelivered_event_is_a_noop(client, engine, paid_order):
    client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    get = client.mock.get(GET_DISPUTE).respond(200, json=DISPUTE)
    assert post(client).json()["status"] == "processed"
    assert post(client).json()["status"] == "duplicate"
    assert get.call_count == 1  # first delivery only; the duplicate fetches nothing
    assert steps(engine).count(("orchestrator", "run_started")) == 1


def test_unverified_event_rejected_and_not_processed(client, engine):
    client.mock.post(VERIFY).respond(200, json={"verification_status": "FAILURE"})
    get = client.mock.get(GET_DISPUTE).respond(200, json=DISPUTE)
    assert post(client).status_code == 401
    assert get.call_count == 0
    with Session(engine) as s:
        assert s.exec(select(Dispute)).all() == []
        assert s.exec(select(ProcessedEvent)).all() == []


def test_missing_signature_headers_rejected_without_calling_paypal(client):
    verify = client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    assert post(client, headers={}).status_code == 400
    assert verify.call_count == 0


def test_tampered_body_is_sent_as_is_for_verification(client):
    """We never 'fix up' the body: PayPal sees exactly what we received and rejects it."""
    verify = client.mock.post(VERIFY).respond(200, json={"verification_status": "FAILURE"})
    tampered = RAW.replace(b'"value":"89.00"', b'"value":"1.00"')
    assert post(client, body=tampered).status_code == 401
    assert tampered in verify.calls[0].request.content


def test_no_webhook_id_configured_is_503(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "")
    get_settings.cache_clear()
    try:
        assert post(client).status_code == 503
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_verify_retried_then_502_when_paypal_down(client):
    verify = client.mock.post(VERIFY).respond(503, json={"name": "SERVICE_UNAVAILABLE"})
    assert post(client).status_code == 502
    assert verify.call_count == 3  # read-only POST: safe to retry


def test_non_dispute_event_ignored(client, engine):
    client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    r = post(client, body=event(event_type="PAYMENT.CAPTURE.COMPLETED"))
    assert r.json() == {"status": "ignored", "event_type": "PAYMENT.CAPTURE.COMPLETED"}


def test_get_dispute_failure_returns_502_and_allows_redelivery(client, engine, paid_order):
    client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    get = client.mock.get(GET_DISPUTE)
    get.side_effect = [httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"}),
                       httpx.Response(200, json=DISPUTE)]
    assert post(client).status_code == 502
    with Session(engine) as s:
        assert s.exec(select(ProcessedEvent)).all() == []
        assert s.exec(select(Dispute)).all() == []
    assert post(client).json()["status"] == "processed"  # PayPal's redelivery succeeds


def test_updated_event_new_version_queues_new_run_same_version_does_not(client, engine, paid_order):
    client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    current = {"dispute": DISPUTE}  # PayPal serves whatever the dispute looks like right now
    client.mock.get(GET_DISPUTE).mock(side_effect=lambda req: httpx.Response(200, json=current["dispute"]))
    v2 = {**DISPUTE, "update_time": "2026-11-01T09:00:00.000Z", "status": "WAITING_FOR_SELLER_RESPONSE"}
    assert post(client).json()["agent_run_queued"] is True
    # A different event about the same dispute version: recorded, but no second run.
    r = post(client, body=event(id="WH-SAMEVERSION", event_type="CUSTOMER.DISPUTE.UPDATED"))
    assert r.json()["agent_run_queued"] is False
    current["dispute"] = v2  # the dispute changes at PayPal
    r = post(client, body=event(id="WH-V2", event_type="CUSTOMER.DISPUTE.UPDATED"))
    assert r.json()["agent_run_queued"] is True
    with Session(engine) as s:
        assert s.exec(select(Dispute)).one().status == "WAITING_FOR_SELLER_RESPONSE"
    assert steps(engine).count(("orchestrator", "run_started")) == 2


def test_resolved_event_updates_row_and_skips_agents(client, engine):
    client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    client.mock.get(GET_DISPUTE).respond(200, json={**DISPUTE, "status": "RESOLVED",
                                                    "update_time": "2026-11-05T00:00:00Z"})
    r = post(client, body=event(id="WH-RES", event_type="CUSTOMER.DISPUTE.RESOLVED"))
    assert r.status_code == 200
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        assert d.status == "RESOLVED" and d.order_id is None  # no matching paid order in this test
    assert steps(engine) == [("orchestrator", "Dispute resolved at PayPal"), ("orchestrator", "run_started"),
                             ("orchestrator", "run_skipped")]
    with Session(engine) as s:
        assert s.exec(select(Dispute)).one().agent_status == "RESOLVED"


def test_records_raw_webhook_when_enabled(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_RECORD_DIR", str(tmp_path))
    get_settings.cache_clear()
    client.mock.post(VERIFY).respond(200, json={"verification_status": "FAILURE"})
    try:
        post(client)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    body = next(tmp_path.glob("*.body.json"))
    assert body.read_bytes() == RAW
    hdrs = json.loads(next(tmp_path.glob("*.headers.json")).read_text())
    assert {k.lower() for k in hdrs} == {k.lower() for k in HEADERS}


def test_no_paypal_credentials_is_503(client, monkeypatch):
    from app.paypal.client import PayPalClient, get_paypal_client

    no_creds = PayPalClient(Settings(_env_file=None, paypal_client_id="", paypal_client_secret=""))
    app.dependency_overrides[get_paypal_client] = lambda: no_creds
    assert post(client).status_code == 503

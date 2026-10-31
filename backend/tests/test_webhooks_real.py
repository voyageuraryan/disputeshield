"""Tests driven by real PayPal sandbox webhook recordings (see tests/fixtures/README.md)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.models import AgentStep, Dispute, Order, ProcessedEvent
from app.paypal.webhooks import build_verify_body
from tests.test_webhooks import VERIFY, client, engine  # noqa: F401  (fixtures)

FIX = Path(__file__).parent / "fixtures"
GET = f"https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-ZWL-10195422"
REAL_GET = json.loads((FIX / "real_get_dispute.json").read_text())


def load(name):
    return (FIX / f"{name}.json").read_bytes(), json.loads((FIX / f"{name}.headers.json").read_text())


CREATED = load("real_dispute_created")
UPDATED = load("real_dispute_updated")


@pytest.fixture
def order31(engine):  # noqa: F811
    with Session(engine) as s:
        o = Order(buyer_email="sb-hizpg53637037@personal.example.com", amount=Decimal("89"), currency="USD",
                  paypal_order_id="2WB80238HM8587143", capture_id="4S541016HD390220V", invoice_id="DS-31-19c56b70")
        s.add(o); s.commit(); s.refresh(o)
        return o.id


def send(client, rec):  # noqa: F811
    raw, headers = rec
    return client.post("/webhooks/paypal", content=raw, headers={**headers, "content-type": "application/json"})


@pytest.mark.parametrize("rec", [CREATED, UPDATED], ids=["created", "updated"])
def test_real_headers_build_valid_verify_request(rec):
    raw, headers = rec
    body = build_verify_body(raw, headers, "95P52181NY896745H")
    assert body.endswith(b',"webhook_event":' + raw + b"}")
    parsed = json.loads(body)
    assert parsed["auth_algo"] == "SHA256withRSA"
    assert parsed["cert_url"].startswith("https://api.sandbox.paypal.com/v1/notifications/certs/")
    assert "auth_version" not in parsed  # extra v2 header is not part of the verify body


def test_real_out_of_order_delivery_links_order_and_runs_once(client, engine, order31):  # noqa: F811
    client.mock.post(VERIFY).respond(200, json={"verification_status": "SUCCESS"})
    get = client.mock.get(GET).respond(200, json=REAL_GET)

    r1 = send(client, UPDATED)   # PayPal delivered UPDATED first
    r2 = send(client, CREATED)
    r3 = send(client, CREATED)   # redelivery
    assert r1.json()["agent_run_queued"] is True
    assert r1.json()["order_id"] == order31
    assert r2.json()["agent_run_queued"] is False  # same dispute version
    assert r3.json()["status"] == "duplicate"
    assert get.call_count == 3  # 2 ingests + 1 investigation (one run); redelivery fetches nothing

    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        assert (d.paypal_dispute_id, d.order_id, d.transaction_id) == ("PP-R-ZWL-10195422", order31, "4S541016HD390220V")
        assert (d.reason, d.stage, d.status, d.amount) == (
            "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "CHARGEBACK", "UNDER_REVIEW", Decimal("89.00"))
        # Stored state comes from GET, not the (older) event snapshot.
        assert d.raw["update_time"] == REAL_GET["update_time"]
        assert json.loads(CREATED[0])["resource"]["update_time"] != REAL_GET["update_time"]
        evts = {e.key for e in s.exec(select(ProcessedEvent)).all() if e.key.startswith("evt:")}
        assert evts == {"evt:" + json.loads(CREATED[0])["id"], "evt:" + json.loads(UPDATED[0])["id"]}
        assert len(s.exec(select(AgentStep).where(AgentStep.action == "run_started")).all()) == 1


def test_real_event_rejected_when_paypal_says_failure(client, engine):  # noqa: F811
    client.mock.post(VERIFY).respond(200, json={"verification_status": "FAILURE"})
    assert send(client, CREATED).status_code == 401
    with Session(engine) as s:
        assert s.exec(select(Dispute)).all() == []

"""Phase 16: trust page data (guardrails resolve to real code, live counters) and demo-mode gating + scenarios."""

import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Decision, DecisionStatus, Dispute, Message, Order, Shipment
from app.trust import GUARDRAILS, counters, resolve
from tests.conftest import BASE
from tests.test_orders_tracking import CAPTURED
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)

TOKEN = "demo-admin-token"


@pytest.fixture
def demo_env(monkeypatch):
    def set_(mode: bool, token: str):
        monkeypatch.setenv("DEMO_MODE", "true" if mode else "false")
        monkeypatch.setenv("ADMIN_TOKEN", token)
        monkeypatch.setenv("FRONTEND_URL", "https://shop.example")
        get_settings.cache_clear()
    yield set_
    get_settings.cache_clear()


# ---- trust ----------------------------------------------------------------------------------------------

def test_every_guardrail_points_at_real_code():
    keys = {g["key"] for g in GUARDRAILS}
    assert {"approval", "citations", "no_overclaim", "pii", "injection", "safety"} <= keys
    for g in GUARDRAILS:
        assert g["where"] and g["what"]
        for ref in g["where"]:
            assert callable(resolve(ref)), ref


def test_rule5_is_really_enforced_where_the_trust_page_says():
    from app.paypal.disputes import NotApprovedError, require_approved

    class P:
        status = DecisionStatus.PROPOSED

    with pytest.raises(NotApprovedError):
        require_approved(P())


def test_counters(engine):
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id="PP-T", reason="X", status="OPEN", amount=D("1"), currency="USD",
                    agent_status="NEEDS_HUMAN")
        s.add(d); s.commit(); s.refresh(d)
        now = datetime.now(timezone.utc)
        rows = [("FIGHT", DecisionStatus.EXECUTED, "FIGHT"), ("SETTLE", DecisionStatus.EXECUTED, "FIGHT"),
                ("GATHER_MORE", DecisionStatus.REJECTED, None), ("FIGHT", DecisionStatus.PROPOSED, None),
                ("SETTLE", DecisionStatus.FAILED, "SETTLE")]
        for rec, st, approved in rows:
            s.add(Decision(dispute_id=d.id, recommendation=rec, confidence="high", reasoning="r", status=st,
                           approved_action=approved, approved_at=now if approved else None))
        s.commit()
        c = counters(s)
    assert (c["proposed"], c["approved"], c["rejected"], c["overridden"], c["executed"], c["failed"],
            c["awaiting"], c["needs_human"]) == (5, 3, 1, 1, 2, 1, 1, 1)


def test_trust_endpoint(client):
    body = client.get("/api/trust").json()
    assert body["guardrails"][0]["key"] == "approval" and "counters" in body


# ---- demo-mode gating ------------------------------------------------------------------------------------

def test_demo_mode_is_off_by_default(client):
    assert get_settings().demo_mode is False
    assert client.get("/api/demo/status").json() == {"enabled": False}
    assert client.get("/api/demo/scenarios", headers={"X-Admin-Token": "anything"}).status_code == 404
    assert client.post("/api/demo/scenarios/inr_strong", headers={"X-Admin-Token": "x"}).status_code == 404


@pytest.mark.parametrize("mode,token,header,status", [
    (True, "", TOKEN, 404),        # flag on but no ADMIN_TOKEN: still off
    (False, TOKEN, TOKEN, 404),    # token set but the flag is off: off
    (True, TOKEN, None, 403),      # on: the admin token is required
    (True, TOKEN, "wrong", 403),
    (True, TOKEN, TOKEN, 200),
])
def test_demo_mode_needs_flag_and_admin_token(client, demo_env, mode, token, header, status):
    demo_env(mode, token)
    headers = {"X-Admin-Token": header} if header else {}
    assert client.get("/api/demo/status").json()["enabled"] is (mode and bool(token))
    assert client.get("/api/demo/scenarios", headers=headers).status_code == status


def test_scenarios_list_has_checklists_with_the_hand_steps(client, demo_env):
    demo_env(True, TOKEN)
    rows = client.get("/api/demo/scenarios", headers={"X-Admin-Token": TOKEN}).json()
    assert [r["key"] for r in rows] == ["inr_strong", "snad_small", "earbuds_ts", "unauth_history"]
    assert [r["expect"] for r in rows][:2] == ["FIGHT", "SETTLE"]
    steps = " ".join(s["step"] for s in rows[0]["checklist"])
    assert "Report a problem" in steps and "www.sandbox.paypal.com" in steps


# ---- a scenario end to end (PayPal mocked) ---------------------------------------------------------------

def test_unauth_scenario_creates_a_real_order_then_history_after_capture(client, engine, demo_env):
    demo_env(True, TOKEN)
    create = client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T",
                                                                               "status": "CREATED"})
    r = client.post("/api/demo/scenarios/unauth_history", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 201, r.text
    body = r.json()
    assert create.call_count == 1  # a real (sandbox) Orders v2 create, through the normal checkout code
    assert body["store_url"] == "https://shop.example/store?pay=5O190127TN364715T&sku=LAMP-01"
    oid = body["order_id"]
    with Session(engine) as s:
        o = s.get(Order, oid)
        assert o.demo_scenario == "unauth_history" and o.paypal_order_id == "5O190127TN364715T"
        assert s.exec(select(Shipment).where(Shipment.order_id == oid)).one().status == "SHIPPED"

    captured = json.loads(json.dumps(CAPTURED))
    captured["purchase_units"][0]["payments"]["captures"][0]["custom_id"] = str(oid)
    captured["payer"] = {**captured.get("payer", {}), "payer_id": "A9SZTAF6P42TU"}
    client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json=captured)
    fp = "a" * 64
    assert client.post("/api/store/orders/5O190127TN364715T/capture", json={"device_fp": fp},
                       headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    from app.demo_scenarios import after_capture
    with Session(engine) as s:
        o = s.get(Order, oid)
        after_capture(s, o)  # idempotent: a second call adds nothing
        sh = s.exec(select(Shipment).where(Shipment.order_id == oid)).one()
        hist = s.exec(select(Order).where(Order.invoice_id.like(f"DEMO-HIST-{oid}-%"))).all()
        assert sh.status == "DELIVERED" and sh.delivered_address == o.shipping_address
        assert len(hist) == 2 and all(h.payer_hash == o.payer_hash and h.buyer_email == o.buyer_email for h in hist)
        assert o.device_hash and o.ip_hash  # recorded at capture: the scenario order was created by the merchant

        from app.signals import prior_undisputed
        d = Dispute(paypal_dispute_id="PP-D-DEMO16", order_id=oid, reason="UNAUTHORISED", status="OPEN",
                    amount=o.amount, currency="USD")
        s.add(d); s.commit()
        hist_flags = prior_undisputed(s, o, d).flags
        assert "ce3_like_match" in hist_flags and "device_reused" in hist_flags   # the paying browser's hashes


def test_small_snad_scenario_has_no_shipment_but_a_buyer_message(client, engine, demo_env):
    demo_env(True, TOKEN)
    client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "8AB12345CD678901E", "status": "CREATED"})
    oid = client.post("/api/demo/scenarios/snad_small", headers={"X-Admin-Token": TOKEN}).json()["order_id"]
    with Session(engine) as s:
        assert s.get(Order, oid).amount == D("12.00")
        assert s.exec(select(Shipment).where(Shipment.order_id == oid)).all() == []
        assert "listing" in s.exec(select(Message).where(Message.order_id == oid)).one().body
    assert client.post("/api/demo/scenarios/nope", headers={"X-Admin-Token": TOKEN}).status_code == 404

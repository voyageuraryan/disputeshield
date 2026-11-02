"""G28: the merchant adds tracking and records the carrier's delivery confirmation from the dashboard;
the investigator turns it into evidence and an item-not-received case can reach FIGHT."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import respx
from sqlmodel import Session, select

from app.agents.investigator import investigate
from app.models import AgentStep, Dispute, Order, Shipment
from app.rules.evidence_rules import economic_baseline, evaluate
from tests.conftest import BASE, token_response
from tests.test_investigator import dispute_json, make_dispute
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)

ADDR = {"address_line_1": "1 Main St", "admin_area_2": "San Jose", "admin_area_1": "CA",
        "postal_code": "95131", "country_code": "US"}


def captured_order(engine, sku="LAMP-01", days_ago=5, **kw) -> int:
    names = {"LAMP-01": "Handmade walnut lamp", "EBOOK-01": "Ebook: Pottery for Beginners"}
    with Session(engine) as s:
        o = Order(buyer_email="buyer@example.com", amount=Decimal("89.00"), currency="USD",
                  items=[{"sku": sku, "name": names[sku], "quantity": 1}], shipping_address=kw.get("addr", ADDR),
                  paypal_order_id="5O190127TN364715T", capture_id="3C679366HH908993F",
                  created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))
        s.add(o); s.commit(); s.refresh(o)
        return o.id


def add_tracking(client, oid, number="9400111899223344556677", carrier="USPS"):
    client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/track").respond(201, json={"id": "x"})
    return client.post(f"/api/store/orders/{oid}/shipments", json={"carrier": carrier, "tracking_number": number})


def today(delta=0):
    return (datetime.now(timezone.utc).date() + timedelta(days=delta)).isoformat()


def test_record_delivery_to_shipping_address(client, engine):
    oid = captured_order(engine)
    sid = add_tracking(client, oid).json()["id"]
    r = client.post(f"/api/store/orders/{oid}/shipments/{sid}/delivery", json={"delivered_on": today(-1)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "DELIVERED" and body["delivery_recorded_by"] == "merchant"
    assert body["delivered_address"] == ADDR
    # correcting it later is allowed (e.g. a typo in the date)
    assert client.post(f"/api/store/orders/{oid}/shipments/{sid}/delivery",
                       json={"delivered_on": today()}).status_code == 200


def test_record_delivery_to_a_different_address(client, engine):
    oid = captured_order(engine)
    sid = add_tracking(client, oid).json()["id"]
    other = {"address_line_1": "9 Elm Rd", "postal_code": "10001", "country_code": "us", "evil": "x"}
    r = client.post(f"/api/store/orders/{oid}/shipments/{sid}/delivery",
                    json={"delivered_on": today(-1), "address": other})
    assert r.status_code == 200, r.text
    assert r.json()["delivered_address"] == {"address_line_1": "9 Elm Rd", "postal_code": "10001", "country_code": "US"}


def test_delivery_validation(client, engine):
    oid = captured_order(engine, days_ago=3)
    sid = add_tracking(client, oid).json()["id"]
    url = f"/api/store/orders/{oid}/shipments/{sid}/delivery"
    assert client.post(url, json={"delivered_on": today(+1)}).status_code == 422  # future
    assert client.post(url, json={"delivered_on": today(-10)}).status_code == 422  # before the order
    assert client.post(url, json={"delivered_on": today(), "address": {"address_line_1": "x"}}).status_code == 422
    assert client.post(f"/api/store/orders/{oid}/shipments/999/delivery", json={"delivered_on": today()}).status_code == 404
    other = captured_order(engine)
    assert client.post(f"/api/store/orders/{other}/shipments/{sid}/delivery",
                       json={"delivered_on": today()}).status_code == 404  # shipment belongs to another order
    with Session(engine) as s:
        assert s.get(Shipment, sid).delivered_at is None


def test_delivery_needs_an_address(client, engine):
    oid = captured_order(engine, addr=None)
    sid = add_tracking(client, oid).json()["id"]
    r = client.post(f"/api/store/orders/{oid}/shipments/{sid}/delivery", json={"delivered_on": today()})
    assert r.status_code == 422 and "address" in r.json()["detail"]


def test_tracking_rules(client, engine):
    ebook = captured_order(engine, sku="EBOOK-01")
    r = add_tracking(client, ebook)
    assert r.status_code == 409 and "digital" in r.json()["detail"]
    oid = captured_order(engine)
    assert add_tracking(client, oid).status_code == 200
    assert add_tracking(client, oid).status_code == 409  # same number twice
    assert client.post(f"/api/store/orders/{oid}/shipments",
                       json={"carrier": "  ", "tracking_number": "1"}).status_code == 422


def test_orders_list_for_the_orders_page(client, engine):
    lamp = captured_order(engine)
    ebook = captured_order(engine, sku="EBOOK-01")
    with Session(engine) as s:
        s.add(Order(buyer_email=None, amount=Decimal("9"), currency="USD", paypal_order_id="ABANDONED1"))
        s.add(Order(buyer_email="old@demo.test", amount=Decimal("45"), currency="USD"))  # seeded / pre-PayPal
        s.commit()
        seeded = s.exec(select(Order).where(Order.buyer_email == "old@demo.test")).one().id
    make_dispute(engine, lamp, "PP-D-LAMP")
    add_tracking(client, lamp)
    rows = {o["id"]: o for o in client.get("/api/store/orders").json()}
    assert set(rows) == {lamp, ebook, seeded}  # abandoned PayPal checkouts are hidden
    assert rows[lamp]["disputes"] == ["PP-D-LAMP"] and rows[lamp]["digital_only"] is False
    assert rows[lamp]["shipments"][0]["tracking_number"] == "9400111899223344556677"
    assert rows[ebook]["digital_only"] is True and rows[ebook]["disputes"] == []


def test_changes_show_on_the_dispute_timeline(client, engine):
    oid = captured_order(engine)
    did = make_dispute(engine, oid, "PP-D-T")
    sid = add_tracking(client, oid).json()["id"]
    client.post(f"/api/store/orders/{oid}/shipments/{sid}/delivery", json={"delivered_on": today()})
    with Session(engine) as s:
        steps = [a.action for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == did)).all()]
    assert steps[0].startswith("Added tracking 9400111899223344556677 (USPS) to order #")
    assert steps[1].startswith("Recorded delivery of 9400111899223344556677 on ")
    assert all("re-analyse" in a for a in steps)


@respx.mock
async def test_recorded_delivery_lets_item_not_received_reach_fight(pp, engine):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{BASE}/v1/customer/disputes/PP-D-F").respond(200, json=dispute_json("PP-D-F"))
    respx.get(f"{BASE}/v2/checkout/orders/5O190127TN364715T").respond(200, json={"id": "5O190127TN364715T"})
    respx.get(url__regex=rf"{BASE}/v2/payments/captures/.*").respond(404, json={"name": "RESOURCE_NOT_FOUND"})
    oid = captured_order(engine)
    with Session(engine) as s:
        s.add(Shipment(order_id=oid, carrier="USPS", tracking_number="9400111899223344556677", status="SHIPPED"))
        s.commit()
    did = make_dispute(engine, oid, "PP-D-F")

    async def score():
        with Session(engine) as s:
            case = await investigate(s, s.get(Dispute, did), pp)
        return case, evaluate(case)

    _, before = await score()
    assert "delivery_confirmation" in before.missing
    assert economic_baseline(before, Decimal("89"), Decimal("25")).action == "GATHER_MORE"

    with Session(engine) as s:
        sh = s.exec(select(Shipment)).one()
        sh.delivered_at, sh.delivered_address, sh.delivery_recorded_by = datetime.now(timezone.utc), ADDR, "merchant"
        s.add(sh); s.commit()
    case, after = await score()
    assert after.missing == [] and after.rule_score >= 0.7
    assert economic_baseline(after, Decimal("89"), Decimal("25")).action == "FIGHT"
    proof = [e for e in case.evidence if e.type == "delivery_confirmation"][0]
    assert "matches the order's shipping address" in proof.summary
    assert "recorded by the merchant from the carrier's confirmation" in proof.summary

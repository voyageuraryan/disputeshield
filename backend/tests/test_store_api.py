import json

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.config import Settings
from app.db import create_db_and_tables, get_session
from app.main import app
from app.models import Order, Shipment
from app.paypal.client import PayPalClient, get_paypal_client
from tests.conftest import BASE, token_response
from tests.test_orders_tracking import CAPTURED


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    return eng


@pytest.fixture
def client(engine):
    async def no_sleep(_):
        pass

    pp = PayPalClient(Settings(_env_file=None), sleep=no_sleep)

    def session_override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_paypal_client] = lambda: pp
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
        with TestClient(app) as c:
            c.mock = mock
            yield c
    app.dependency_overrides.clear()


def test_products_and_config(client):
    skus = {p["sku"]: p for p in client.get("/api/store/products").json()}
    assert set(skus) == {"LAMP-01", "EARBUDS-01", "CASE-01", "EBOOK-01", "MUG-01"}
    assert skus["LAMP-01"]["price"] == "89.00" and skus["EARBUDS-01"]["price"] == "59.00"
    assert {k: (p["category"], p["paypal_category"]) for k, p in skus.items()} == {
        "LAMP-01": ("electronics", "PHYSICAL_GOODS"), "EARBUDS-01": ("electronics", "PHYSICAL_GOODS"),
        "CASE-01": ("accessory", "PHYSICAL_GOODS"), "EBOOK-01": ("digital", "DIGITAL_GOODS"),
        "MUG-01": ("home", "PHYSICAL_GOODS")}
    assert skus["LAMP-01"]["name"] == "Smart LED desk lamp" and skus["LAMP-01"]["model"] == "Lumina LD-200"
    cfg = client.get("/api/store/config").json()
    assert cfg["client_id"] == "test-client"
    assert "sandbox.paypal.com" in cfg["sdk_url"]
    assert "secret" not in json.dumps(cfg)


def test_checkout_flow_create_capture_track(client, engine):
    create = client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T", "status": "CREATED"})
    r = client.post("/api/store/orders", json={"sku": "LAMP-01"})
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]
    assert r.json()["id"] == "5O190127TN364715T"

    sent = json.loads(create.calls[0].request.content)["purchase_units"][0]
    assert sent["custom_id"] == str(order_id)
    assert sent["invoice_id"].startswith(f"DS-{order_id}-")
    assert sent["amount"]["value"] == "89.00"  # price from server catalog

    captured = json.loads(json.dumps(CAPTURED))
    captured["purchase_units"][0]["payments"]["captures"][0]["custom_id"] = str(order_id)
    cap = client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json=captured)
    r = client.post("/api/store/orders/5O190127TN364715T/capture")
    assert r.status_code == 200, r.text
    assert r.json()["capture_id"] == "3C679366HH908993F"

    with Session(engine) as s:
        o = s.get(Order, order_id)
        assert (o.paypal_order_id, o.capture_id, o.buyer_email) == ("5O190127TN364715T", "3C679366HH908993F", "buyer@example.com")
        assert o.shipping_address["postal_code"] == "95131"
        assert o.invoice_id == sent["invoice_id"]
        # store category + model are kept on the order line; PayPal still gets its own item category
        assert (o.items[0]["category"], o.items[0]["model"]) == ("electronics", "Lumina LD-200")
    assert sent["items"][0]["category"] == "PHYSICAL_GOODS"

    # Second capture call is answered from our DB, not re-sent to PayPal.
    assert client.post("/api/store/orders/5O190127TN364715T/capture").status_code == 200
    assert cap.call_count == 1

    track = client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/track").respond(201, json={"id": "x"})
    r = client.post(f"/api/store/orders/{order_id}/shipments", json={"carrier": "USPS", "tracking_number": "9400111"})
    assert r.status_code == 200, r.text
    assert json.loads(track.calls[0].request.content)["capture_id"] == "3C679366HH908993F"
    with Session(engine) as s:
        assert s.exec(select(Shipment)).one().tracking_number == "9400111"

    listed = client.get("/api/store/orders").json()
    assert listed[0]["id"] == order_id


def test_unknown_sku(client):
    assert client.post("/api/store/orders", json={"sku": "NOPE"}).status_code == 404


def test_create_paypal_error_returns_502_and_cleans_up(client, engine):
    client.mock.post(f"{BASE}/v2/checkout/orders").respond(
        422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-1"})
    r = client.post("/api/store/orders", json={"sku": "MUG-01"})
    assert r.status_code == 502
    assert r.json()["detail"]["debug_id"] == "dbg-1"
    with Session(engine) as s:
        assert s.exec(select(Order)).all() == []


def test_capture_unknown_order(client):
    assert client.post("/api/store/orders/ABC123/capture").status_code == 404


def test_capture_custom_id_mismatch_rejected(client, engine):
    client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T"})
    client.post("/api/store/orders", json={"sku": "LAMP-01"})
    bad = json.loads(json.dumps(CAPTURED))
    bad["purchase_units"][0]["payments"]["captures"][0]["custom_id"] = "999"
    client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json=bad)
    assert client.post("/api/store/orders/5O190127TN364715T/capture").status_code == 409
    with Session(engine) as s:
        assert s.exec(select(Order)).one().capture_id is None


def test_capture_declined(client):
    client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T"})
    oid = client.post("/api/store/orders", json={"sku": "LAMP-01"}).json()["order_id"]
    declined = json.loads(json.dumps(CAPTURED))
    c = declined["purchase_units"][0]["payments"]["captures"][0]
    c["custom_id"], c["status"] = str(oid), "DECLINED"
    client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json=declined)
    assert client.post("/api/store/orders/5O190127TN364715T/capture").status_code == 402


def test_shipment_requires_captured_order(client, engine):
    with Session(engine) as s:
        o = Order(buyer_email="x@demo.test", amount=12, currency="USD")
        s.add(o); s.commit(); s.refresh(o)
    r = client.post(f"/api/store/orders/{o.id}/shipments", json={"carrier": "UPS", "tracking_number": "1"})
    assert r.status_code == 409


def test_create_without_credentials_is_503_and_leaves_no_row(client, engine, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        r = client.post("/api/store/orders", json={"sku": "LAMP-01"})
        assert r.status_code == 503
        assert "credentials" in r.json()["detail"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    with Session(engine) as s:
        assert s.exec(select(Order)).all() == []


def test_create_transport_failure_leaves_no_row(client, engine):
    import httpx
    client.mock.post(f"{BASE}/v2/checkout/orders").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        client.post("/api/store/orders", json={"sku": "LAMP-01"})
    with Session(engine) as s:
        assert s.exec(select(Order)).all() == []


def test_new_orders_are_rate_limited_per_ip(client, monkeypatch):
    from app import ratelimit
    from app.troubleshooting.access import RateLimiter

    client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T", "status": "CREATED"})
    monkeypatch.setattr(ratelimit, "EXEMPT", set())
    monkeypatch.setitem(ratelimit.LIMITS, "store_order", RateLimiter(capacity=2, refill_per_s=0.01))
    ip = {"X-Forwarded-For": "198.51.100.9"}
    codes = [client.post("/api/store/orders", json={"sku": "LAMP-01"}, headers=ip).status_code for _ in range(3)]
    assert codes == [200, 200, 429]

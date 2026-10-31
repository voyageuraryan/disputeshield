import json
from decimal import Decimal

import httpx
import pytest
import respx

from app.paypal.client import PayPalError
from app.paypal.orders import LineItem, build_order_request, capture_order, create_order, parse_capture
from app.paypal.tracking import add_tracking, build_tracker_request
from tests.conftest import BASE, token_response

LAMP = LineItem("Handmade walnut lamp", "LAMP-01", Decimal("89"), 1, "PHYSICAL_GOODS")
EBOOK = LineItem("Ebook", "EBOOK-01", Decimal("9"), 2, "DIGITAL_GOODS")

CAPTURED = {
    "id": "5O190127TN364715T",
    "status": "COMPLETED",
    "payment_source": {"paypal": {"email_address": "buyer@example.com"}},
    "payer": {"email_address": "buyer@example.com", "payer_id": "QYR5Z8XDVJNXQ"},
    "purchase_units": [{
        "reference_id": "order-7",
        "shipping": {"address": {"address_line_1": "2211 N First Street", "admin_area_2": "San Jose",
                                 "admin_area_1": "CA", "postal_code": "95131", "country_code": "US"}},
        "payments": {"captures": [{"id": "3C679366HH908993F", "status": "COMPLETED", "custom_id": "7",
                                   "invoice_id": "DS-7-abc", "amount": {"currency_code": "USD", "value": "89.00"}}]},
    }],
}


def test_order_request_carries_internal_id_and_totals():
    body = build_order_request(internal_order_id=7, invoice_id="DS-7-abc", currency="USD", items=[LAMP, EBOOK])
    pu = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert pu["invoice_id"] == "DS-7-abc"
    assert pu["custom_id"] == "7"
    assert pu["amount"] == {"currency_code": "USD", "value": "107.00",
                            "breakdown": {"item_total": {"currency_code": "USD", "value": "107.00"}}}
    assert pu["items"][1] == {"name": "Ebook", "sku": "EBOOK-01", "quantity": "2",
                              "unit_amount": {"currency_code": "USD", "value": "9.00"}, "category": "DIGITAL_GOODS"}
    # Physical goods in the order: no payment_source (PayPal collects the shipping address).
    assert set(body) == {"intent", "purchase_units"}


def test_digital_only_order_skips_shipping():
    body = build_order_request(internal_order_id=1, invoice_id="x", currency="USD", items=[EBOOK])
    assert body["payment_source"] == {"paypal": {"experience_context": {"shipping_preference": "NO_SHIPPING"}}}


def test_order_request_needs_items():
    with pytest.raises(ValueError):
        build_order_request(internal_order_id=1, invoice_id="x", currency="USD", items=[])


@respx.mock
async def test_create_order_sends_request_id_and_retries(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{BASE}/v2/checkout/orders")
    route.side_effect = [httpx.Response(503), httpx.Response(201, json={"id": "ORD1", "status": "CREATED"})]
    res = await create_order(internal_order_id=7, invoice_id="DS-7-abc", currency="USD", items=[LAMP],
                             request_id="ds-create-7-abc", client=pp)
    assert res["id"] == "ORD1"
    assert route.call_count == 2
    assert all(c.request.headers["PayPal-Request-Id"] == "ds-create-7-abc" for c in route.calls)
    assert json.loads(route.calls[0].request.content)["purchase_units"][0]["custom_id"] == "7"


@respx.mock
async def test_capture_order(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json=CAPTURED)
    raw = await capture_order("5O190127TN364715T", request_id="ds-capture-5O190127TN364715T", client=pp)
    req = route.calls[0].request
    assert req.headers["Prefer"] == "return=representation"
    assert req.headers["PayPal-Request-Id"] == "ds-capture-5O190127TN364715T"
    r = parse_capture(raw)
    assert (r.capture_id, r.capture_status, r.custom_id, r.payer_email) == (
        "3C679366HH908993F", "COMPLETED", "7", "buyer@example.com")
    assert r.shipping_address["postal_code"] == "95131"


async def test_capture_rejects_bad_id(pp):
    with pytest.raises(ValueError):
        await capture_order("../x", request_id="r", client=pp)


def test_parse_capture_without_captures():
    r = parse_capture({"id": "O1", "status": "APPROVED", "purchase_units": [{}]})
    assert r.capture_id is None and r.payer_email is None


def test_tracker_request_known_and_other_carriers():
    assert build_tracker_request(capture_id="C1", tracking_number="T1", carrier="usps") == {
        "capture_id": "C1", "tracking_number": "T1", "notify_payer": False, "carrier": "USPS"}
    other = build_tracker_request(capture_id="C1", tracking_number="T1", carrier="Royal Courier")
    assert other["carrier"] == "OTHER" and other["carrier_name_other"] == "Royal Courier"
    with pytest.raises(ValueError):
        build_tracker_request(capture_id="", tracking_number="T1", carrier="UPS")
    with pytest.raises(ValueError):
        build_tracker_request(capture_id="C1", tracking_number="x" * 65, carrier="UPS")


@respx.mock
async def test_add_tracking_posts_to_orders_track(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{BASE}/v2/checkout/orders/ORD1/track").respond(201, json={"id": "ORD1"})
    await add_tracking("ORD1", capture_id="C1", tracking_number="1Z999", carrier="UPS", client=pp)
    assert json.loads(route.calls[0].request.content) == {
        "capture_id": "C1", "tracking_number": "1Z999", "notify_payer": False, "carrier": "UPS"}
    assert "PayPal-Request-Id" not in route.calls[0].request.headers


@respx.mock
async def test_add_tracking_not_retried_on_5xx(pp, sleeps):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{BASE}/v2/checkout/orders/ORD1/track").respond(500, json={})
    with pytest.raises(PayPalError):
        await add_tracking("ORD1", capture_id="C1", tracking_number="1Z999", carrier="UPS", client=pp)
    assert route.call_count == 1

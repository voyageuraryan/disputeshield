"""PayPal Orders v2: create and capture.

Checked against developer.paypal.com/api/orders/v2/schema.json:
- POST /v2/checkout/orders              orders.create  (PayPal-Request-Id documented)
- POST /v2/checkout/orders/{id}/capture orders.capture (PayPal-Request-Id documented)
- GET  /v2/checkout/orders/{id}         orders.get (purchase_units[].shipping.trackers)
purchase_units[].invoice_id (max 127) and custom_id (max 255) carry our order id.
items[].quantity is a string; amount.value is a string; amount.breakdown.item_total
must equal sum(unit_amount * quantity) when items are sent.
`Prefer: return=representation` returns the full order (with captures) on capture.

Physical goods: only intent + purchase_units (PayPal's JS SDK v6 example); PayPal asks the buyer for a
shipping address. Digital-only orders (e.g. the ebook) add
payment_source.paypal.experience_context.shipping_preference = NO_SHIPPING, the documented replacement for
the deprecated application_context.shipping_preference. (Correction: the sandbox checkout error seen on
2026-10-31 came from India->India sandbox accounts, not from payment_source.)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.paypal.client import PayPalClient, get_paypal_client

ORDERS_PATH = "/v2/checkout/orders"
PREFER_FULL = {"Prefer": "return=representation"}


@dataclass(frozen=True)
class LineItem:
    name: str
    sku: str
    unit_amount: Decimal
    quantity: int
    category: str  # PHYSICAL_GOODS / DIGITAL_GOODS / DONATION


def _money(currency: str, value: Decimal) -> dict[str, str]:
    return {"currency_code": currency, "value": f"{value.quantize(Decimal('0.01'))}"}


def build_order_request(
    *, internal_order_id: int, invoice_id: str, currency: str, items: list[LineItem]
) -> dict[str, Any]:
    if not items:
        raise ValueError("order needs at least one item")
    total = sum((i.unit_amount * i.quantity for i in items), Decimal("0"))
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": f"order-{internal_order_id}",
                "invoice_id": invoice_id,
                "custom_id": str(internal_order_id),
                "amount": {
                    **_money(currency, total),
                    "breakdown": {"item_total": _money(currency, total)},
                },
                "items": [
                    {
                        "name": i.name,
                        "sku": i.sku,
                        "quantity": str(i.quantity),
                        "unit_amount": _money(currency, i.unit_amount),
                        "category": i.category,
                    }
                    for i in items
                ],
            }
        ],
    }
    if all(i.category == "DIGITAL_GOODS" for i in items):
        body["payment_source"] = {"paypal": {"experience_context": {"shipping_preference": "NO_SHIPPING"}}}
    return body


async def create_order(
    *,
    internal_order_id: int,
    invoice_id: str,
    currency: str,
    items: list[LineItem],
    request_id: str,
    client: PayPalClient | None = None,
) -> dict[str, Any]:
    """Create a PayPal order. Returns the PayPal order (has `id`, `status`)."""
    body = build_order_request(
        internal_order_id=internal_order_id, invoice_id=invoice_id, currency=currency, items=items
    )
    return await (client or get_paypal_client()).post(ORDERS_PATH, json=body, request_id=request_id)


async def capture_order(
    paypal_order_id: str, *, request_id: str, client: PayPalClient | None = None
) -> dict[str, Any]:
    """Capture an approved order. Returns the full order representation."""
    if not paypal_order_id or not paypal_order_id.isalnum():
        raise ValueError(f"invalid PayPal order id: {paypal_order_id!r}")
    return await (client or get_paypal_client()).post(
        f"{ORDERS_PATH}/{paypal_order_id}/capture", json={}, request_id=request_id, headers=PREFER_FULL
    )


async def get_order(paypal_order_id: str, *, client: PayPalClient | None = None) -> dict[str, Any]:
    if not paypal_order_id or not paypal_order_id.isalnum():
        raise ValueError(f"invalid PayPal order id: {paypal_order_id!r}")
    return await (client or get_paypal_client()).get(f"{ORDERS_PATH}/{paypal_order_id}")


@dataclass(frozen=True)
class CaptureResult:
    paypal_order_id: str
    order_status: str | None
    capture_id: str | None
    capture_status: str | None
    custom_id: str | None
    payer_email: str | None
    shipping_address: dict | None


def parse_capture(order: dict[str, Any]) -> CaptureResult:
    pu = (order.get("purchase_units") or [{}])[0]
    captures = ((pu.get("payments") or {}).get("captures")) or []
    cap = captures[0] if captures else {}
    payer_email = (order.get("payer") or {}).get("email_address") or (
        (order.get("payment_source") or {}).get("paypal") or {}
    ).get("email_address")
    return CaptureResult(
        paypal_order_id=order["id"],
        order_status=order.get("status"),
        capture_id=cap.get("id"),
        capture_status=cap.get("status"),
        custom_id=cap.get("custom_id") or pu.get("custom_id"),
        payer_email=payer_email,
        shipping_address=(pu.get("shipping") or {}).get("address"),
    )

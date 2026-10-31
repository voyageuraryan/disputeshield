"""Add shipment tracking to a captured order.

Uses the current Orders v2 endpoint POST /v2/checkout/orders/{id}/track
(operationId orders.track.create; body order_tracker_request with required
capture_id, tracking_number, carrier). The older /v1/shipping/trackers API
still exists but PayPal's Orders v2 docs point to orders/{id}/track.
Reading trackers: GET /v1/shipping/trackers/{id} returns 403 NOT_AUTHORIZED for this
app (seen live 2026-10-31), so trackers are read from the order instead
(purchase_units[].shipping.trackers: id "<capture_id>-<tracking_number>", status
SHIPPED/CANCELLED; no carrier field).
The spec documents no PayPal-Request-Id here, so the client won't re-send it
after a 5xx; a duplicate tracker returns 200 rather than 201.
"""

from __future__ import annotations

from typing import Any

from app.paypal.client import PayPalClient, get_paypal_client

# Subset of the spec's shipment_carrier enum (1,451 values) that the demo uses.
# Anything else must be sent as OTHER + carrier_name_other.
KNOWN_CARRIERS = {"USPS", "UPS", "FEDEX", "DHL", "DHL_API"}


def build_tracker_request(
    *, capture_id: str, tracking_number: str, carrier: str, notify_payer: bool = False
) -> dict[str, Any]:
    if not capture_id or not tracking_number:
        raise ValueError("capture_id and tracking_number are required")
    if len(tracking_number) > 64:
        raise ValueError("tracking_number max length is 64")
    body: dict[str, Any] = {
        "capture_id": capture_id,
        "tracking_number": tracking_number,
        "notify_payer": notify_payer,
    }
    carrier_up = carrier.upper()
    if carrier_up in KNOWN_CARRIERS:
        body["carrier"] = carrier_up
    else:
        body["carrier"] = "OTHER"
        body["carrier_name_other"] = carrier[:64]
    return body


async def add_tracking(
    paypal_order_id: str,
    *,
    capture_id: str,
    tracking_number: str,
    carrier: str,
    notify_payer: bool = False,
    client: PayPalClient | None = None,
) -> dict[str, Any]:
    if not paypal_order_id or not paypal_order_id.isalnum():
        raise ValueError(f"invalid PayPal order id: {paypal_order_id!r}")
    body = build_tracker_request(
        capture_id=capture_id, tracking_number=tracking_number, carrier=carrier, notify_payer=notify_payer
    )
    return await (client or get_paypal_client()).post(
        f"/v2/checkout/orders/{paypal_order_id}/track", json=body
    )


def trackers_from_order(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Trackers on an Orders v2 order, with tracking_number split out of the tracker id."""
    out = []
    for pu in order.get("purchase_units") or []:
        for t in ((pu.get("shipping") or {}).get("trackers")) or []:
            tid = t.get("id") or ""
            capture_id, _, number = tid.partition("-")
            out.append({"id": tid, "capture_id": capture_id or None, "tracking_number": number or None,
                        "status": t.get("status"), "create_time": t.get("create_time"),
                        "update_time": t.get("update_time")})
    return out

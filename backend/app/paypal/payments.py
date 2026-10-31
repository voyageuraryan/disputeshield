"""PayPal Payments v2 (read-only).

Checked against developer.paypal.com/api/payments/v2/schema.json:
- GET /v2/payments/captures/{capture_id} -> capture-2
  (status, status_details.reason, amount, invoice_id, custom_id, seller_protection,
  supplementary_data.related_ids.order_id, create_time, update_time)
Seen live: a disputed capture reads status PENDING, status_details.reason BUYER_COMPLAINT.
"""

from __future__ import annotations

from typing import Any

from app.paypal.client import PayPalClient, get_paypal_client


async def get_capture(capture_id: str, *, client: PayPalClient | None = None) -> dict[str, Any]:
    if not capture_id or not capture_id.isalnum():
        raise ValueError(f"invalid capture id: {capture_id!r}")
    return await (client or get_paypal_client()).get(f"/v2/payments/captures/{capture_id}")

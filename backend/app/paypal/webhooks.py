"""Webhook signature verification via PayPal's postback endpoint.

Checked against developer.paypal.com/api/webhooks/v1/schema.json and
developer.paypal.com/api/rest/webhooks/rest (Postback method):
- POST /v1/notifications/verify-webhook-signature (verify-webhook-signature.post)
- body: auth_algo, cert_url, transmission_id, transmission_sig, transmission_time,
  webhook_id, webhook_event (all required)
- response: {"verification_status": "SUCCESS" | "FAILURE"}
- values come from the PAYPAL-AUTH-ALGO, PAYPAL-CERT-URL, PAYPAL-TRANSMISSION-ID,
  PAYPAL-TRANSMISSION-SIG and PAYPAL-TRANSMISSION-TIME headers.
- Mock events from the webhook simulator cannot be verified by postback.

The event is spliced into the request byte-for-byte: re-serialising it can change
it, and PayPal verifies the signature against the exact payload it sent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from app.paypal.client import PayPalClient, get_paypal_client

log = logging.getLogger("app.paypal")

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"
SIGNATURE_HEADERS = {
    "auth_algo": "PAYPAL-AUTH-ALGO",
    "cert_url": "PAYPAL-CERT-URL",
    "transmission_id": "PAYPAL-TRANSMISSION-ID",
    "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
    "transmission_time": "PAYPAL-TRANSMISSION-TIME",
}
DISPUTE_EVENTS = {"CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"}


class WebhookVerificationError(Exception):
    pass


def build_verify_body(raw_event: bytes, headers: Mapping[str, str], webhook_id: str) -> bytes:
    """Build the verify request with the raw event embedded verbatim."""
    lower = {k.lower(): v for k, v in headers.items()}
    fields: dict[str, str] = {}
    for field, header in SIGNATURE_HEADERS.items():
        value = lower.get(header.lower())
        if not value:
            raise WebhookVerificationError(f"missing {header} header")
        fields[field] = value
    fields["webhook_id"] = webhook_id
    try:
        json.loads(raw_event)
    except ValueError as exc:
        raise WebhookVerificationError("event body is not valid JSON") from exc
    head = json.dumps(fields, separators=(",", ":"))[:-1]  # drop closing brace
    # No strip()/re-encode: the event goes to PayPal exactly as received
    # (surrounding JSON whitespace is still valid JSON).
    return head.encode() + b',"webhook_event":' + raw_event + b"}"


async def verify_webhook(
    raw_event: bytes, headers: Mapping[str, str], webhook_id: str, *, client: PayPalClient | None = None
) -> bool:
    """True only if PayPal answers verification_status == SUCCESS."""
    if not webhook_id:
        raise WebhookVerificationError("PAYPAL_WEBHOOK_ID is not configured")
    body = build_verify_body(raw_event, headers, webhook_id)
    resp = await (client or get_paypal_client()).post(
        VERIFY_PATH,
        content=body,
        headers={"Content-Type": "application/json"},
        retry_safe=True,  # read-only check
    )
    status = (resp or {}).get("verification_status")
    if status != "SUCCESS":
        log.warning("webhook verification failed: status=%s transmission_id=%s",
                    status, {k.lower(): v for k, v in headers.items()}.get("paypal-transmission-id"))
    return status == "SUCCESS"

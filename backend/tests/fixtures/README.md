# Webhook fixtures

## Real recordings (PayPal sandbox, 2026-10-31)
Captured by `WEBHOOK_RECORD_DIR` from real deliveries for case PP-R-ZWL-10195422
(item not received, $89, on store order 31 / capture 4S541016HD390220V).
Bodies are **byte-for-byte** as PayPal sent them; do not re-format.

- `real_dispute_created.json` / `.headers.json`: CUSTOMER.DISPUTE.CREATED
- `real_dispute_updated.json` / `.headers.json`: CUSTOMER.DISPUTE.UPDATED (delivered *before* CREATED)
- `real_get_dispute.json`: GET /v1/customer/disputes/PP-R-ZWL-10195422 right after both events

Observed: the event `resource` is a snapshot (update_time 11:06:07) older than GET
(11:06:43), which is why the handler always re-fetches. PayPal also sends
`paypal-auth-version: v2`, which verify-webhook-signature does not take.
All accounts and emails are sandbox test data.

## Sample (hand-built, kept for edge-case tests)
`webhook_dispute_created.json` / `.headers.json` were built before any real event
existed, from PayPal's documented envelope + Disputes v1 `dispute` schema.

To record more: set WEBHOOK_RECORD_DIR in backend/.env, trigger events in the sandbox,
and copy the saved `*.body.json` / `*.headers.json` here with `cp`.
- `real_capture.json`: GET /v2/payments/captures/4S541016HD390220V (status PENDING / BUYER_COMPLAINT)
- `real_order.json`: GET /v2/checkout/orders/2WB80238HM8587143 (has the USPS tracker)

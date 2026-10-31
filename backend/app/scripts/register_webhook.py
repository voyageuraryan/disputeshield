"""Register (or re-point) this app's PayPal webhook for dispute events.

Checked against developer.paypal.com/api/webhooks/v1/schema.json:
- GET   /v1/notifications/webhooks               -> {"webhooks": [webhook]}
- POST  /v1/notifications/webhooks               body {url, event_types: [{name}]} -> 201 webhook
- PATCH /v1/notifications/webhooks/{webhook_id}  JSON Patch, e.g. replace /url

Quick-tunnel URLs change on every restart, so an existing webhook that already
points at <something>/webhooks/paypal is re-pointed instead of creating another.

Usage:
  cd backend && python -m app.scripts.register_webhook                    # list
  cd backend && python -m app.scripts.register_webhook --url https://HOST/webhooks/paypal [--write-env]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from app.config import ENV_FILE
from app.paypal.client import PayPalClient, PayPalError, close_paypal_client, get_paypal_client
from app.paypal.webhooks import DISPUTE_EVENTS

WEBHOOKS_PATH = "/v1/notifications/webhooks"
LISTENER_PATH = "/webhooks/paypal"


async def list_webhooks(client: PayPalClient) -> list[dict]:
    data = await client.get(WEBHOOKS_PATH)
    return (data or {}).get("webhooks", [])


async def ensure_webhook(url: str, client: PayPalClient) -> tuple[str, str]:
    """Return (webhook_id, action) where action is existing / updated / created."""
    if not url.startswith("https://") or not url.endswith(LISTENER_PATH):
        raise ValueError(f"url must be https://<host>{LISTENER_PATH}")
    hooks = await list_webhooks(client)
    for h in hooks:
        if h.get("url") == url:
            return h["id"], "existing"
    for h in hooks:
        if (h.get("url") or "").endswith(LISTENER_PATH):
            await client.request("PATCH", f"{WEBHOOKS_PATH}/{h['id']}",
                                 json=[{"op": "replace", "path": "/url", "value": url}])
            return h["id"], "updated"
    created = await client.post(WEBHOOKS_PATH, json={
        "url": url, "event_types": [{"name": n} for n in sorted(DISPUTE_EVENTS)]})
    return created["id"], "created"


def write_env(webhook_id: str, env_file: Path = ENV_FILE) -> None:
    text = env_file.read_text() if env_file.exists() else ""
    line = f"PAYPAL_WEBHOOK_ID={webhook_id}"
    if re.search(r"^PAYPAL_WEBHOOK_ID=.*$", text, flags=re.M):
        text = re.sub(r"^PAYPAL_WEBHOOK_ID=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    env_file.write_text(text)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help=f"public https URL ending in {LISTENER_PATH}")
    ap.add_argument("--write-env", action="store_true", help="save PAYPAL_WEBHOOK_ID to backend/.env")
    args = ap.parse_args(argv)
    client = get_paypal_client()
    try:
        if not args.url:
            hooks = await list_webhooks(client)
            for h in hooks:
                print(h["id"], h.get("url"), [e.get("name") for e in h.get("event_types", [])])
            print(f"{len(hooks)} webhook(s)")
            return 0
        webhook_id, action = await ensure_webhook(args.url, client)
    except (PayPalError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await close_paypal_client()
    print(f"{action}: PAYPAL_WEBHOOK_ID={webhook_id} -> {args.url}")
    if args.write_env:
        write_env(webhook_id)
        print(f"saved to {ENV_FILE}; restart the API to load it")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

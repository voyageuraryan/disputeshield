"""POST /webhooks/paypal. Every event is verified with PayPal before it is used."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.engine import Engine
from sqlmodel import Session

from urllib.parse import urlparse

from app import ratelimit
from app.agents.runner import run_agents
from app.config import get_settings
from app.db import get_engine, get_session
from app.ingest import already_processed, ingest_dispute
from app.paypal.client import PayPalClient, PayPalError, PayPalNotConfiguredError, get_paypal_client
from app.paypal.webhooks import DISPUTE_EVENTS, WebhookVerificationError, verify_webhook

log = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])

# Status codes: PayPal re-delivers any non-2xx up to 25 times over 3 days. Bad or
# unverified signatures get 4xx (never processed). Temporary failures on our side or
# PayPal's get 5xx so the event is re-delivered. Irrelevant events get 200.


MAX_BODY = 64 * 1024  # real dispute events are ~1-4 KB
PAYPAL_CERT_HOSTS = {"api.paypal.com", "api.sandbox.paypal.com", "api-m.paypal.com", "api-m.sandbox.paypal.com"}


def precheck_problem(headers) -> str | None:  # noqa: ANN001
    """Cheap checks before asking PayPal to verify, so junk never costs a PayPal call. Not a replacement for the
    signature check. No freshness window: PayPal re-delivers the same transmission for up to 3 days."""
    h = {k.lower(): v for k, v in headers.items()}
    for name in ("paypal-transmission-id", "paypal-transmission-sig", "paypal-cert-url", "paypal-auth-algo",
                 "paypal-transmission-time"):
        if not h.get(name):
            return f"missing {name}"
    cert = urlparse(h["paypal-cert-url"])
    if cert.scheme != "https" or (cert.hostname or "").lower() not in PAYPAL_CERT_HOSTS:
        return "certificate URL is not on a PayPal host"
    try:
        datetime.fromisoformat(h["paypal-transmission-time"].replace("Z", "+00:00"))
    except ValueError:
        return "bad transmission time"
    return None


def _maybe_record(raw: bytes, headers) -> None:
    """Dev aid: save what PayPal actually sent so it can become a recorded test fixture."""
    target = get_settings().webhook_record_dir
    if not target:
        return
    try:
        d = Path(target)
        d.mkdir(parents=True, exist_ok=True)
        stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        (d / f"{stem}.body.json").write_bytes(raw)
        keep = {k: v for k, v in headers.items() if k.lower().startswith("paypal-")}
        (d / f"{stem}.headers.json").write_text(json.dumps(keep, indent=2))
    except OSError:
        log.exception("could not record webhook")


@router.post("/webhooks/paypal", dependencies=[Depends(ratelimit.limit("webhook"))])
async def paypal_webhook(
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    engine: Engine = Depends(get_engine),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> dict:
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise HTTPException(413, "webhook body too large")
    raw = b""
    async for chunk in request.stream():  # also caps bodies sent without a Content-Length (chunked)
        raw += chunk
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "webhook body too large")
    webhook_id = get_settings().paypal_webhook_id
    if not webhook_id:
        log.error("PAYPAL_WEBHOOK_ID not configured; rejecting webhook")
        raise HTTPException(503, "webhook verification not configured")

    if problem := precheck_problem(request.headers):
        raise HTTPException(400, f"unverifiable webhook: {problem}")
    try:
        verified = await verify_webhook(raw, request.headers, webhook_id, client=paypal)
    except WebhookVerificationError as exc:
        raise HTTPException(400, f"unverifiable webhook: {exc}") from exc
    except PayPalNotConfiguredError as exc:
        log.error("webhook received but PayPal credentials are not configured")
        raise HTTPException(503, "PayPal credentials not configured") from exc
    except PayPalError as exc:
        raise HTTPException(502, f"could not verify with PayPal (debug_id={exc.debug_id})") from exc
    if not verified:
        raise HTTPException(401, "webhook signature verification failed")
    _maybe_record(raw, request.headers)  # dev recorder: only events PayPal confirmed (never strangers' junk)

    event = json.loads(raw)
    event_id, event_type = event.get("id"), event.get("event_type")
    if event_type not in DISPUTE_EVENTS:
        return {"status": "ignored", "event_type": event_type}
    if not event_id:
        raise HTTPException(400, "event has no id")
    event_key = f"evt:{event_id}"
    if already_processed(session, event_key):
        return {"status": "duplicate", "event_id": event_id}

    dispute_id = (event.get("resource") or {}).get("dispute_id")
    if not dispute_id:
        log.error("dispute event %s has no resource.dispute_id", event_id)
        return {"status": "ignored", "reason": "no dispute_id"}

    try:
        result = await ingest_dispute(
            session, dispute_id, client=paypal, event_key=event_key,
            event_detail={"event_type": event_type, "create_time": event.get("create_time")},
        )
    except PayPalError as exc:
        session.rollback()
        raise HTTPException(502, f"could not fetch dispute (debug_id={exc.debug_id})") from exc

    if result.run_key:
        background.add_task(run_agents, result.dispute_row_id, result.run_key, engine)
    return {"status": "processed", "event_id": event_id, "dispute_id": dispute_id,
            "order_id": result.order_id, "agent_run_queued": bool(result.run_key)}

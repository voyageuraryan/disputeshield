"""Demo + trust API (Phase 16).

  GET  /api/trust                       guardrails (with code locations) + live decision counters   [merchant]
  GET  /api/demo/status                 {enabled}: whether the Scenarios drawer is available        [merchant]
  GET  /api/demo/scenarios              scenarios + checklists                   [merchant + X-Admin-Token]
  POST /api/demo/scenarios/{key}        create a real sandbox order + store data [merchant + X-Admin-Token]
Demo endpoints answer 404 unless DEMO_MODE=true AND ADMIN_TOKEN is set (off by default, so off in production).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import Session

from app import demo_scenarios as demo
from app.catalog import PRODUCTS
from app import ratelimit
from app.config import get_settings
from app.db import get_session
from app.paypal.client import PayPalClient, get_paypal_client
from app.trust import GUARDRAILS, counters

router = APIRouter(tags=["demo"])


@router.get("/api/trust")
def trust(session: Session = Depends(get_session)) -> dict:
    return {"guardrails": GUARDRAILS, "counters": counters(session)}


@router.get("/api/demo/status")
def demo_status() -> dict:
    return {"enabled": demo.enabled()}


def require_demo(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    if not demo.enabled():
        raise HTTPException(404, "not found")  # demo mode off: the endpoints don't exist
    expected = get_settings().admin_token.get_secret_value()
    ratelimit.blocked(request, "admin_fail")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        ratelimit.failed(request, "admin_fail")  # only wrong tokens count: 5 guesses a minute per IP
        raise HTTPException(403, "admin token required")


def _store_url(paypal_order_id: str, sku: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/store?pay={paypal_order_id}&sku={sku}"


@router.get("/api/demo/scenarios", dependencies=[Depends(require_demo)])
def list_scenarios() -> list[dict]:
    return [{**demo.describe(s), "checklist": demo.checklist(s, store_url=f"{get_settings().frontend_url}/store")}
            for s in demo.SCENARIOS.values()]


@router.post("/api/demo/scenarios/{key}", dependencies=[Depends(require_demo)], status_code=201)
async def create_scenario(key: str, session: Session = Depends(get_session),
                          paypal: PayPalClient = Depends(get_paypal_client)) -> dict:
    sc = demo.SCENARIOS.get(key)
    if sc is None:
        raise HTTPException(404, "unknown scenario")
    from app.api.store import open_paypal_order

    order = await open_paypal_order(session, PRODUCTS[sc.sku], 1, paypal=paypal, demo_scenario=sc.key)
    demo.seed_before_payment(session, sc, order)
    url = _store_url(order.paypal_order_id, sc.sku)
    return {**demo.describe(sc), "order_id": order.id, "paypal_order_id": order.paypal_order_id, "store_url": url,
            "checklist": demo.checklist(sc, store_url=url)}

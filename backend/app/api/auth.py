"""Log in with PayPal endpoints.

GET /api/auth/config            -> {"mode": "paypal"|"off"} (public; the UI decides whether to show login)
GET /api/auth/paypal/start      -> 302 to PayPal's consent page (signed single-use state carries `next`)
GET /api/auth/paypal/callback   -> exchanges the code, checks the merchant allowlist, then 302 to
                                   FRONTEND_URL/auth/callback#token=... (fragment: never sent to servers/logs)
GET /api/auth/me                -> the logged-in merchant
"""

from __future__ import annotations

import re

import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from app import ratelimit
from app.auth import allowed_emails, auth_disabled, consume_state, new_session, new_state, require_merchant
from app.config import get_settings
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.paypal.identity import authorize_url, exchange_code, userinfo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


# A same-app path: starts with ONE "/", then only URL-safe characters. Browsers treat a backslash like "/" and drop
# tabs and newlines, so "/<backslash>evil.com" or "/<TAB>/evil.com" would become "//evil.com" (an open redirect);
# none of those characters are allowed. The frontend applies the same rule (pages/AuthCallback.tsx safeNext).
SAFE_NEXT = re.compile(r"/(?![/\\])[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#]{0,500}")


def _safe_next(path: str | None) -> str:
    return path if path and SAFE_NEXT.fullmatch(path) else "/queue"


def _to_frontend(fragment: dict[str, str]) -> RedirectResponse:
    base = get_settings().frontend_url.rstrip("/")
    return RedirectResponse(f"{base}/auth/callback#{urlencode(fragment, quote_via=quote)}", status_code=302)


@router.get("/config")
def auth_config() -> dict:
    return {"mode": "off" if auth_disabled() else "paypal"}


@router.get("/paypal/start", dependencies=[Depends(ratelimit.limit("login"))])
def start(next: str | None = Query(default=None)) -> RedirectResponse:
    return RedirectResponse(authorize_url(new_state(_safe_next(next))), status_code=302)


@router.get("/paypal/callback", dependencies=[Depends(ratelimit.limit("login"))])
async def callback(code: str | None = None, state: str | None = None, error: str | None = None,
                   error_description: str | None = None,
                   paypal: PayPalClient = Depends(get_paypal_client)) -> RedirectResponse:
    if error:  # e.g. the merchant pressed Cancel on PayPal's page
        log.info("Log in with PayPal returned an error: %.80s", error)
        # Never reflect PayPal's (attacker-settable) error text onto our domain: fixed code + fixed message only.
        return _to_frontend({"error": "Login was cancelled or refused at PayPal", "code": "cancelled"})
    if not code or not state:
        return _to_frontend({"error": "PayPal did not return a login code", "code": "no_code"})
    try:
        st = consume_state(state)
    except ValueError as exc:
        return _to_frontend({"error": f"login link {exc}; please start again", "code": "state"})
    try:
        tokens = await exchange_code(code, client=paypal)
        user = await userinfo(tokens["access_token"], client=paypal)
    except (PayPalError, KeyError) as exc:
        log.warning("Log in with PayPal failed: %s", exc)
        return _to_frontend({"error": "PayPal login failed; please try again", "code": "paypal_failed", "next": st["next"]})
    email = (user.get("email") or "").lower()
    allow = allowed_emails()
    if not allow:
        return _to_frontend({"error": "no merchant is configured: set MERCHANT_LOGIN_EMAILS on the server",
                             "code": "not_configured"})
    if email not in allow:
        log.warning("login refused for %s (not in MERCHANT_LOGIN_EMAILS)", email or "<no email>")
        return _to_frontend({"error": f"{email or 'this PayPal account'} is not allowed to manage this store",
                             "code": "not_allowed"})
    sub = user.get("user_id") or user.get("sub") or email
    session = new_session({"sub": sub, "email": email, "name": user.get("name")})
    log.info("merchant %s logged in", email)
    return _to_frontend({"token": session, "next": st["next"]})


@router.get("/me")
def me(merchant: dict = Depends(require_merchant)) -> dict:
    return {"email": merchant.get("email"), "name": merchant.get("name"), "sub": merchant.get("sub"),
            "expires_at": merchant.get("exp")}

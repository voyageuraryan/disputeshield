"""Log in with PayPal (OpenID Connect), sandbox only.

From developer.paypal.com/docs/log-in-with-paypal/integrate (2026-10-31):
- Authorize (browser): https://www.sandbox.paypal.com/connect?flowEntry=static&client_id=..&response_type=code
  &scope=openid%20email&redirect_uri=..&state=..
- Exchange: POST /v1/oauth2/token, Basic <client_id:secret>, form grant_type=authorization_code&code=..
  -> access_token (expires_in default 28800), refresh_token, ...
- User info: the Identity v1 OpenAPI spec lists GET /v1/identity/openidconnect/userinfo; the guide shows
  /v1/identity/oauth2/userinfo. We call the spec's path and fall back to the guide's on 404.
Requires "Log in with PayPal" enabled on the app with this redirect URI as a Return URL.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from app.config import SANDBOX_WEB_URL, get_settings
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client

log = logging.getLogger(__name__)
SCOPES = "openid email"
# Seen live 2026-11-01: the spec's path without a schema answers 401 (empty body) to a valid sandbox token, so we
# try the documented variants in order. The guide's oauth2 path with schema=paypalv1.1 comes first.
USERINFO_CANDIDATES = (
    ("/v1/identity/oauth2/userinfo", {"schema": "paypalv1.1"}),
    ("/v1/identity/openidconnect/userinfo", {"schema": "openid"}),
    ("/v1/identity/oauth2/userinfo", None),
    ("/v1/identity/openidconnect/userinfo", None),
)
RETRY_NEXT_ON = {400, 401, 403, 404}


def authorize_url(state: str) -> str:
    s = get_settings()
    q = {"flowEntry": "static", "client_id": s.paypal_client_id, "response_type": "code", "scope": SCOPES,
         "redirect_uri": s.auth_redirect_uri, "state": state}
    return f"{SANDBOX_WEB_URL}/connect?{urlencode(q)}"


async def exchange_code(code: str, *, client: PayPalClient | None = None) -> dict:
    s = get_settings()
    return await (client or get_paypal_client()).call_without_app_token(
        "POST", "/v1/oauth2/token", idempotent=False,  # a code works once: never re-send after a 5xx
        data={"grant_type": "authorization_code", "code": code},
        auth=(s.paypal_client_id, s.paypal_client_secret.get_secret_value()),
        headers={"Accept": "application/json"})


async def userinfo(access_token: str, *, client: PayPalClient | None = None) -> dict:
    """The user's profile. Tries each documented variant; 4xx (wrong path/schema for this account) -> next one.
    5xx and network errors are not retried here (they surface as a failed login the user can retry)."""
    c = client or get_paypal_client()
    last: PayPalError | None = None
    for path, params in USERINFO_CANDIDATES:
        try:
            info = await c.call_without_app_token(
                "GET", path, idempotent=True, params=params,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        except PayPalError as exc:
            if exc.status_code not in RETRY_NEXT_ON:
                raise
            log.info("userinfo %s %s -> %s; trying the next variant", path, params, exc.status_code)
            last = exc
            continue
        log.info("userinfo answered by %s %s", path, params)
        return _normalise(info or {})
    raise last  # type: ignore[misc]


def _normalise(info: dict) -> dict:
    """The paypalv1.1 schema returns emails as a list; openid returns `email`. Return one shape."""
    email = info.get("email")
    if not email:
        emails = info.get("emails") or []
        primary = next((e for e in emails if e.get("primary")), emails[0] if emails else {})
        email = primary.get("value")
    return {**info, "email": email, "user_id": info.get("user_id") or info.get("payer_id") or info.get("sub")}

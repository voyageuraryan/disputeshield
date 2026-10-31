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

from urllib.parse import urlencode

from app.config import SANDBOX_WEB_URL, get_settings
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client

SCOPES = "openid email"
USERINFO_PATHS = ("/v1/identity/openidconnect/userinfo", "/v1/identity/oauth2/userinfo")


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
    c = client or get_paypal_client()
    last: PayPalError | None = None
    for path in USERINFO_PATHS:
        try:
            return await c.call_without_app_token(
                "GET", path, idempotent=True,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        except PayPalError as exc:
            if exc.status_code != 404:
                raise
            last = exc
    raise last  # type: ignore[misc]

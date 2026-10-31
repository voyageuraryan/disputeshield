"""Shared async httpx client for every PayPal call (hard rule 3).

- OAuth2 client_credentials token (POST /v1/oauth2/token), cached and refreshed
  60s before expiry.
- Retries twice with exponential backoff. Idempotency policy:
    * 429 is always retried (the request was rejected, not processed).
    * 5xx and transport errors are retried only for safe requests: GET, the
      token call, or a POST sent with a PayPal-Request-Id (idempotency key).
      Only pass request_id to endpoints whose spec documents PayPal-Request-Id
      (e.g. Orders v2 create/capture). The Disputes v1 spec documents no
      idempotency header, so money-moving dispute POSTs are never re-sent
      after an ambiguous failure; callers must reconcile via get_dispute().
    * retry_safe=True marks a read-only POST (e.g. verify-webhook-signature).
- On a 401 the cached token is dropped and the request retried once.
- Logs the Paypal-Debug-Id response header (or body debug_id) on errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("app.paypal")

TOKEN_PATH = "/v1/oauth2/token"
TOKEN_REFRESH_MARGIN_S = 60
MAX_RETRIES = 2
BACKOFF_BASE_S = 0.5
RETRY_STATUSES = {429, 500, 502, 503, 504}
REQUEST_ID_HEADER = "PayPal-Request-Id"


class PayPalNotConfiguredError(RuntimeError):
    """PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing."""


class PayPalError(Exception):
    def __init__(self, status_code: int, method: str, path: str, debug_id: str | None, body: Any):
        self.status_code = status_code
        self.method = method
        self.path = path
        self.debug_id = debug_id
        self.body = body
        name = body.get("name") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        super().__init__(
            f"PayPal {method} {path} -> {status_code} {name or ''} {message or ''} (debug_id={debug_id})".strip()
        )


def _debug_id(resp: httpx.Response, body: Any) -> str | None:
    hdr = resp.headers.get("Paypal-Debug-Id")  # httpx headers are case-insensitive
    if hdr:
        return hdr
    return body.get("debug_id") if isinstance(body, dict) else None


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


class PayPalClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings or get_settings()
        self._http = httpx.AsyncClient(
            base_url=self._settings.paypal_base_url, timeout=30.0, transport=transport
        )
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- auth -------------------------------------------------------------

    def _token_valid(self) -> bool:
        return self._token is not None and self._clock() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S

    async def get_token(self) -> str:
        if self._token_valid():
            return self._token  # type: ignore[return-value]
        async with self._token_lock:
            if self._token_valid():
                return self._token  # type: ignore[return-value]
            s = self._settings
            if not s.paypal_client_id or not s.paypal_client_secret.get_secret_value():
                raise PayPalNotConfiguredError(
                    "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set (see backend/.env.example)"
                )
            resp = await self._send_with_retries(
                "POST",
                TOKEN_PATH,
                data={"grant_type": "client_credentials"},
                auth=(s.paypal_client_id, s.paypal_client_secret.get_secret_value()),
                headers={"Accept": "application/json"},
                idempotent=True,
            )
            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expires_at = self._clock() + int(payload["expires_in"])
            return self._token

    def invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    # ---- transport --------------------------------------------------------

    async def _send_with_retries(
        self, method: str, path: str, *, idempotent: bool, **kwargs: Any
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= MAX_RETRIES or not idempotent:
                    log.error("PayPal %s %s transport error after %d retries: %s", method, path, attempt, exc)
                    raise
                log.warning("PayPal %s %s transport error (%s), retrying", method, path, exc)
            else:
                if resp.status_code < 400:
                    return resp
                body = _body(resp)
                debug_id = _debug_id(resp, body)
                retryable = resp.status_code == 429 or (idempotent and resp.status_code in RETRY_STATUSES)
                if retryable and attempt < MAX_RETRIES:
                    log.warning(
                        "PayPal %s %s -> %s, retrying (attempt %d) Paypal-Debug-Id=%s",
                        method, path, resp.status_code, attempt + 1, debug_id,
                    )
                else:
                    log.error("PayPal %s %s -> %s Paypal-Debug-Id=%s body=%s", method, path, resp.status_code, debug_id, body)
                    raise PayPalError(resp.status_code, method, path, debug_id, body)
            await self._sleep(BACKOFF_BASE_S * (2**attempt))
            attempt += 1

    async def request(
        self, method: str, path: str, *, request_id: str | None = None, retry_safe: bool = False, **kwargs: Any
    ) -> Any:
        extra_headers = dict(kwargs.pop("headers", None) or {})
        if request_id:
            extra_headers[REQUEST_ID_HEADER] = request_id
        idempotent = method.upper() == "GET" or bool(request_id) or retry_safe
        for auth_attempt in range(2):
            token = await self.get_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **extra_headers}
            try:
                # A 401 means the request was rejected, so re-sending with a new token is safe.
                resp = await self._send_with_retries(method, path, headers=headers, idempotent=idempotent, **kwargs)
            except PayPalError as exc:
                if exc.status_code == 401 and auth_attempt == 0:
                    self.invalidate_token()
                    continue
                raise
            return resp.json() if resp.content else None
        raise AssertionError("unreachable")

    async def call_without_app_token(self, method: str, path: str, *, idempotent: bool, **kwargs: Any) -> Any:
        """A PayPal call that authenticates itself (e.g. Basic client credentials for an authorization-code
        exchange, or a user's own access token for userinfo) instead of the app's client-credentials token.
        Same retry / error / debug-id handling as every other call."""
        resp = await self._send_with_retries(method, path, idempotent=idempotent, **kwargs)
        return resp.json() if resp.content else None

    async def get(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        return await self.request("GET", path, params=params, **kwargs)

    async def post(
        self,
        path: str,
        json: Any | None = None,
        *,
        request_id: str | None = None,
        retry_safe: bool = False,
        **kwargs: Any,
    ) -> Any:
        """POST. Pass request_id only where the endpoint documents PayPal-Request-Id.
        retry_safe=True only for read-only POSTs (e.g. verify-webhook-signature)."""
        return await self.request("POST", path, json=json, request_id=request_id, retry_safe=retry_safe, **kwargs)


_client: PayPalClient | None = None


def get_paypal_client() -> PayPalClient:
    global _client
    if _client is None:
        _client = PayPalClient()
    return _client


async def close_paypal_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

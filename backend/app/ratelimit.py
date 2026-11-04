"""Per-client-IP rate limits for the public endpoints (security audit, Group B).

In memory, like the buyer page's limiter: Render runs one worker. Requests from this machine (127.0.0.1, ::1, the
test client) are never limited, so dev.sh, e2e, evals and pytest run as before; requests through the local tunnel
carry Cloudflare's client IP and are limited like any other.

Two kinds:
- check(): every request takes one (store orders, login, webhooks, Ask)
- failures only (admin / demo tokens, buyer help links): blocked() before the check, failed() after a wrong
  token, so a real user is never slowed down, only someone guessing.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.signals import client_ip
from app.troubleshooting.access import RateLimiter

EXEMPT = {"127.0.0.1", "::1", "localhost", "testclient"}

LIMITS: dict[str, RateLimiter] = {
    "store_order": RateLimiter(capacity=10, refill_per_s=10 / 60),   # 10 new orders a minute per IP
    "login": RateLimiter(capacity=20, refill_per_s=20 / 60),         # login start + callback
    "webhook": RateLimiter(capacity=120, refill_per_s=2.0),          # far above PayPal's real rate
    "ask": RateLimiter(capacity=10, refill_per_s=10 / 60),           # "Ask your disputes" (LLM call)
    "admin_fail": RateLimiter(capacity=5, refill_per_s=5 / 60),      # wrong X-Admin-Token
    "help_fail": RateLimiter(capacity=20, refill_per_s=20 / 60),     # wrong buyer link token
}


def ip_of(request: Request) -> str | None:
    return client_ip(request.headers, request.client.host if request.client else None)


def _key(request: Request) -> str | None:
    ip = ip_of(request)
    return None if not ip or ip in EXEMPT else ip


def _too_many(wait: float) -> HTTPException:
    return HTTPException(429, "Too many requests; please wait a moment and try again",
                         headers={"Retry-After": str(max(1, int(wait + 0.999)))})


def check(request: Request, name: str) -> None:
    key = _key(request)
    if key and (wait := LIMITS[name].hit(key)):
        raise _too_many(wait)


def blocked(request: Request, name: str) -> None:
    key = _key(request)
    if key and (wait := LIMITS[name].peek(key)):
        raise _too_many(wait)


def failed(request: Request, name: str) -> None:
    key = _key(request)
    if key:
        LIMITS[name].hit(key)


def limit(name: str):
    """FastAPI dependency: Depends(limit("store_order"))."""
    def dep(request: Request) -> None:
        check(request, name)
    return dep


def reset() -> None:
    for lim in LIMITS.values():
        lim.reset()

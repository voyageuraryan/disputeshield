"""Private access links for the buyer troubleshooting page.

The link carries a random 32-byte token; only its SHA-256 is stored. Every buyer request checks the
token (constant-time), the expiry and that the dispute is still open. Buyer and merchant credentials
never cross: buyer routes refuse merchant credentials and merchant routes don't accept buyer tokens
(require_merchant only accepts HMAC-signed session tokens).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.config import get_settings

TOKEN_BYTES = 32
DUE_MARGIN = timedelta(hours=48)       # the buyer must finish this long before the merchant's deadline
NO_DUE_DATE_TTL = timedelta(days=5)    # when PayPal gives no seller_response_due


def new_token() -> tuple[str, str]:
    """(token for the link, sha256 hex to store)."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str | None, stored_hash: str | None) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_expiry(seller_response_due: datetime | None, invited_at: datetime) -> datetime:
    due = _aware(seller_response_due)
    return due - DUE_MARGIN if due else _aware(invited_at) + NO_DUE_DATE_TTL


def is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    return _aware(expires_at) <= (now or datetime.now(timezone.utc))


def build_link(session_id: int, token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/help/{session_id}?t={token}"


# ---- rate limiting (in-memory token bucket per session; single process) ------------------

@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class RateLimiter:
    def __init__(self, capacity: int = 30, refill_per_s: float = 0.5, clock=time.monotonic, max_keys: int = 10_000):
        self.capacity, self.refill, self.clock, self.max_keys = capacity, refill_per_s, clock, max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Requests for made-up session ids create buckets too; forget the ones that have refilled
        (a full bucket is the same as no bucket), so memory stays bounded."""
        full_after = self.capacity / self.refill
        for k in [k for k, b in self._buckets.items() if now - b.updated >= full_after]:
            del self._buckets[k]

    def hit(self, key: str) -> float:
        """Take one request; returns 0 when allowed, else seconds until the next request is allowed."""
        now = self.clock()
        with self._lock:
            if len(self._buckets) >= self.max_keys:
                self._prune(now)
            b = self._buckets.get(key)
            if b is None:
                b = self._buckets[key] = _Bucket(tokens=self.capacity, updated=now)
            b.tokens = min(self.capacity, b.tokens + (now - b.updated) * self.refill)
            b.updated = now
            if b.tokens >= 1:
                b.tokens -= 1
                return 0.0
            return (1 - b.tokens) / self.refill

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


buyer_limiter = RateLimiter()


def rate_limit(session_id: int) -> None:
    wait = buyer_limiter.hit(f"ts:{session_id}")
    if wait:
        raise HTTPException(429, "Too many requests; please wait a moment and try again",
                            headers={"Retry-After": str(max(1, int(wait + 0.999)))})

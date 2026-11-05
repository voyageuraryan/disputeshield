"""Merchant sessions for DisputeShield (after Log in with PayPal).

Tokens are compact HMAC-SHA256 signed JSON (base64url payload "." base64url signature) with an expiry.
OAuth `state` uses the same signing plus a single-use nonce, so a login link can't be forged or replayed.
`require_merchant` protects every merchant endpoint; it accepts `Authorization: Bearer <token>` only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.config import get_settings
from app.db import get_engine

log = logging.getLogger(__name__)
STATE_TTL_S = 600
_used_nonces: dict[str, float] = {}  # nonce -> expiry (single process)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> bytes:
    secret = get_settings().auth_secret.get_secret_value()
    if len(secret) < 32:
        raise HTTPException(503, "login is not configured: set AUTH_SECRET (32+ characters) in backend/.env")
    return secret.encode()


def sign(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(token: str, kind: str) -> dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        data = json.loads(_unb64(body))
    except HTTPException:
        raise
    except Exception as exc:  # malformed, bad signature, bad json
        raise ValueError("invalid token") from exc
    if data.get("kind") != kind:
        raise ValueError("wrong token kind")
    if data.get("exp", 0) < time.time():
        raise ValueError("expired")
    return data


def new_session(user: dict[str, Any]) -> str:
    hours = get_settings().session_hours
    now = time.time()
    return sign({"kind": "session", "sub": user["sub"], "email": user.get("email"), "name": user.get("name"),
                 "iat": now, "exp": int(now + hours * 3600)})


def new_state(next_path: str) -> str:
    return sign({"kind": "state", "nonce": secrets.token_urlsafe(16), "next": next_path,
                 "exp": int(time.time() + STATE_TTL_S)})


def consume_state(state: str) -> dict[str, Any]:
    data = verify(state, "state")
    now = time.time()
    for n, exp in list(_used_nonces.items()):
        if exp < now:
            del _used_nonces[n]
    if data["nonce"] in _used_nonces:
        raise ValueError("state already used")
    _used_nonces[data["nonce"]] = data["exp"]
    return data


def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in get_settings().merchant_login_emails.split(",") if e.strip()}


def auth_disabled() -> bool:
    """AUTH_MODE=off is a local-development escape hatch; never honoured on Render."""
    mode = get_settings().auth_mode.lower()
    if mode == "off":
        if os.environ.get("RENDER"):
            log.error("AUTH_MODE=off ignored on Render; login stays required")
            return False
        return True
    return False


def sessions_valid_after(eng) -> float | None:  # noqa: ANN001
    """G167: the moment the merchant last pressed Log out (every older session is refused), from the database."""
    from sqlmodel import Session as DBSession

    from app.models import MerchantSettings

    try:
        with DBSession(eng) as s:
            row = s.get(MerchantSettings, 1)
            at = row.sessions_valid_after if row else None
    except Exception:  # noqa: BLE001 - e.g. tables not created yet: nothing has been revoked
        return None
    if at is None:
        return None
    from datetime import timezone

    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()


def require_merchant(authorization: str | None = Header(default=None),
                     eng=Depends(get_engine)) -> dict[str, Any]:  # noqa: ANN001
    """Session from `Authorization: Bearer <token>` only. (Security review 2026-11-05: the `?token=` query form is
    gone; the dashboard reads live streams and files with fetch() + this header, so the session never sits in URLs,
    browser history or proxy logs.)"""
    if auth_disabled():
        return {"sub": "local-dev", "email": None, "name": "Local developer"}
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = verify(raw, "session")
    except ValueError as exc:
        raise HTTPException(401, f"session {exc}; please log in again",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    # Re-checked on every request: removing an email from MERCHANT_LOGIN_EMAILS (e.g. the judges' account after
    # judging) ends that account's sessions at once, instead of when the token expires.
    cutoff = sessions_valid_after(eng)
    if cutoff is not None and float(claims.get("iat") or 0) <= cutoff:
        raise HTTPException(401, "session ended by Log out; please log in again",
                            headers={"WWW-Authenticate": "Bearer"})
    if (claims.get("email") or "").lower() not in allowed_emails():
        raise HTTPException(401, "this account is no longer allowed to manage this store; please log in again",
                            headers={"WWW-Authenticate": "Bearer"})
    return claims

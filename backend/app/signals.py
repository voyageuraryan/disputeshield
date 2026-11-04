"""Deterministic buyer signals (Phase 15) for UNAUTHORISED and serial-dispute cases.

Privacy: the checkout IP and device fingerprint are stored ONLY as keyed hashes (HMAC-SHA256 with SIGNAL_HASH_KEY,
truncated to 128 bits): a plain hash of an IPv4 address could be reversed by trying all 4 billion. The browser
already hashes the fingerprint (user agent + screen + timezone) before sending it. The PayPal payer id is hashed the
same way. Shipping addresses are compared through the same normalised hash. Merchants see only which kinds of
identifiers matched ("device + address"), never values. Hashes are cleared SIGNAL_RETENTION_DAYS after the order
(retention.purge_signal_hashes; DEPLOY.md).

1. Prior undisputed history, MODELLED ON Visa Compelling Evidence 3.0 (reason 10.4: 2+ prior undisputed, settled
   transactions from the same merchant and card, 120-365 days before the disputed one, each matching on a primary
   element (IP or device) plus a second (IP, device, account, shipping address)). PayPal disputes are NOT Visa CE 3.0:
   this is presented as similar evidence, never as compliance. Here "same card" becomes "same buyer account" (email or
   PayPal payer id). Flags: ce3_like_match (the full pattern) or partial_match (an earlier undisputed order shares
   device / IP / address, but the full pattern isn't met), plus device_reused / ip_reused / address_reused.
2. Serial disputer: other disputes by the same buyer (email or payer id, across all orders) in the last 12 months;
   >= 2 -> buyer_serial_disputes on the buyer_history item. Context for the merchant, never a reason to FIGHT.
3. Address reuse: the shipping address was used on earlier paid, undisputed orders -> address_reused.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Dispute, Order

log = logging.getLogger(__name__)
DEV_KEY = "disputeshield-dev-signal-key"
CE_MIN_DAYS, CE_MAX_DAYS, CE_MIN_PRIOR = 120, 365, 2
SERIAL_WINDOW_DAYS, SERIAL_MIN = 365, 2
PRIMARY = ("ip", "device")
WORDS = {"ip": "IP address", "device": "device", "account": "account", "address": "shipping address"}


def _key() -> bytes:
    s = get_settings()
    k = s.signal_hash_key.get_secret_value() or s.auth_secret.get_secret_value()
    if not k and os.environ.get("RENDER"):  # the public dev key would make the stored hashes reversible
        raise RuntimeError("SIGNAL_HASH_KEY (or AUTH_SECRET) must be set on Render")
    return (k or DEV_KEY).encode()


def check_key() -> None:
    """Startup check (main.lifespan): refuse to run on Render with the built-in dev key."""
    _key()


def signal_hash(kind: str, value: str | None) -> str | None:
    v = (value or "").strip().lower()
    if not v:
        return None
    return hmac.new(_key(), f"{kind}:{v}".encode(), hashlib.sha256).hexdigest()[:32]


def client_ip(headers, client_host: str | None) -> str | None:  # noqa: ANN001
    """The real client's IP. Render sits behind Cloudflare, which sets CF-Connecting-IP itself (a client can't
    choose it), so that header wins when we're on Render or behind a local tunnel (peer is loopback). Otherwise
    the RIGHT-most X-Forwarded-For hop (the one our own proxy added; the left-most is whatever the client sent),
    else the socket peer."""
    peer = client_host or None
    if os.environ.get("RENDER") or peer in ("127.0.0.1", "::1"):
        cf = (headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
    hops = [h.strip() for h in (headers.get("x-forwarded-for") or "").split(",") if h.strip()]
    return hops[-1] if hops else peer


FP_RE = re.compile(r"^[0-9a-f]{64}$")


def capture_checkout(order: Order, *, ip: str | None, device_fp: str | None) -> None:
    """At checkout: keep only hashes. device_fp must be the browser's SHA-256 hex (anything else is ignored)."""
    order.ip_hash = signal_hash("ip", ip)
    order.device_hash = signal_hash("device", device_fp) if device_fp and FP_RE.match(device_fp) else None


def address_hash(addr: dict | None) -> str | None:
    if not addr or not addr.get("address_line_1"):
        return None
    norm = "|".join(re.sub(r"[^a-z0-9]", "", (addr.get(k) or "").lower())
                    for k in ("address_line_1", "admin_area_2", "postal_code", "country_code"))
    return signal_hash("address", norm)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dispute_payer_hash(d: Dispute) -> str | None:
    for tx in (d.raw or {}).get("disputed_transactions") or []:
        pid = (tx.get("buyer") or {}).get("payer_id")
        if pid:
            return signal_hash("payer", pid)
    return None


def _same_buyer(o: Order, email: str | None, payer: str | None) -> bool:
    return bool((email and o.buyer_email and o.buyer_email.lower() == email.lower())
                or (payer and o.payer_hash and o.payer_hash == payer))


def matched_elements(prior: Order, order: Order, payer: str | None) -> set[str]:
    m = set()
    if order.ip_hash and prior.ip_hash == order.ip_hash:
        m.add("ip")
    if order.device_hash and prior.device_hash == order.device_hash:
        m.add("device")
    if (payer and prior.payer_hash == payer) or (order.buyer_email and prior.buyer_email
                                                 and prior.buyer_email.lower() == order.buyer_email.lower()):
        m.add("account")
    a = address_hash(order.shipping_address)
    if a and address_hash(prior.shipping_address) == a:
        m.add("address")
    return m


@dataclass
class PriorHistory:
    priors: list[dict] = field(default_factory=list)   # [{order_id, days_before, matched: [...], in_window, qualifies}]
    flags: list[str] = field(default_factory=list)
    summary: str = ""


def prior_undisputed(session: Session, order: Order, dispute: Dispute) -> PriorHistory:
    payer = order.payer_hash or dispute_payer_hash(dispute)
    disputed_ids = {d.order_id for d in session.exec(select(Dispute).where(Dispute.order_id.is_not(None))).all()}
    when = _aware(order.created_at)
    cands = [o for o in session.exec(select(Order).where(Order.id != order.id, Order.capture_id.is_not(None))).all()
             if _aware(o.created_at) < when and o.id not in disputed_ids and _same_buyer(o, order.buyer_email, payer)]
    out = PriorHistory()
    for o in sorted(cands, key=lambda x: x.created_at):
        days = (when - _aware(o.created_at)).days
        m = matched_elements(o, order, payer)
        in_window = CE_MIN_DAYS <= days <= CE_MAX_DAYS
        out.priors.append({"order_id": o.id, "days_before": days, "matched": sorted(m), "in_window": in_window,
                           "qualifies": in_window and bool(m & set(PRIMARY)) and len(m) >= 2})
    if not out.priors:
        return out
    qualifying = [p for p in out.priors if p["qualifies"]]
    shared = {e for p in out.priors for e in p["matched"]} - {"account"}
    if len(qualifying) >= CE_MIN_PRIOR:
        out.flags.append("ce3_like_match")
    elif shared:
        out.flags.append("partial_match")
    out.flags += [f"{e}_reused" for e in ("device", "ip", "address") if e in shared]
    common = set.intersection(*(set(p["matched"]) for p in qualifying)) if qualifying else set()
    if "ce3_like_match" in out.flags:
        on = " + ".join(WORDS[e] for e in ("device", "ip", "address", "account") if e in common)
        out.summary = (f"{len(qualifying)} earlier paid, undisputed orders by this buyer, {CE_MIN_DAYS}-{CE_MAX_DAYS} "
                       f"days before this one, matched on {on}. This is similar to the evidence Visa Compelling "
                       f"Evidence 3.0 uses for card-not-present fraud claims (PayPal disputes don't follow Visa CE 3.0; "
                       f"it's supporting evidence, not a compliance claim).")
    else:
        on = " / ".join(WORDS[e] for e in ("device", "ip", "address") if e in shared) or "the account only"
        out.summary = (f"{len(out.priors)} earlier paid, undisputed order(s) by this buyer share {on} with this one "
                       f"(partial match: the Visa CE 3.0-like pattern needs 2+ orders {CE_MIN_DAYS}-{CE_MAX_DAYS} days "
                       f"earlier matching on device or IP plus one more identifier).")
    return out


def serial_disputes(session: Session, order: Order | None, dispute: Dispute, *,
                    now: datetime | None = None) -> list[int]:
    """Other disputes by the same buyer (order email / payer id, or the dispute's own buyer fields) in 12 months."""
    now = now or _opened(dispute) or datetime.now(timezone.utc)
    email = (order.buyer_email if order else None) or _dispute_email(dispute)
    payer = (order.payer_hash if order else None) or dispute_payer_hash(dispute)
    if not email and not payer:
        return []
    out = []
    for d in session.exec(select(Dispute).where(Dispute.id != dispute.id)).all():
        o = session.get(Order, d.order_id) if d.order_id else None
        same = (o is not None and _same_buyer(o, email, payer)) or \
               (email and (_dispute_email(d) or "").lower() == email.lower()) or (payer and dispute_payer_hash(d) == payer)
        opened = _opened(d)
        if same and opened is not None and now - timedelta(days=SERIAL_WINDOW_DAYS) <= opened <= now + timedelta(days=1):
            out.append(d.id)
    return out


def _dispute_email(d: Dispute) -> str | None:
    for tx in (d.raw or {}).get("disputed_transactions") or []:
        e = (tx.get("buyer") or {}).get("email")
        if e:
            return e
    return None


def _opened(d: Dispute) -> datetime | None:
    raw = (d.raw or {}).get("create_time")
    if raw:
        try:
            return _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            pass
    return _aware(d.refreshed_at) if d.refreshed_at else None

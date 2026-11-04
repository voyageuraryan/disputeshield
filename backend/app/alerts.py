"""Merchant alerts (Phase 17): email (SMTP) and Slack (incoming webhook) when
  - a decision is PROPOSED                     key alert:proposed:<decision id>
  - a buyer finishes troubleshooting           key alert:ts:<session id>:<status>
  - the seller response is due in <= 48 h / <= 12 h and nothing is approved yet
                                               key alert:due48|due12:<dispute id>:<deadline>
Each alert is claimed once in processed_events (kind "alert"), so a re-run, a redelivered webhook or the next loop
tick never sends it twice. Triggers only queue (status "pending"); alerts_loop delivers (and retries failures up to
MAX_ATTEMPTS), so an agent run or a buyer's request never waits on SMTP or Slack.

Channels: email when SMTP_HOST and the merchant's alert address are set; Slack when SLACK_WEBHOOK_URL is set and the
merchant hasn't turned it off (docs.slack.dev: POST application/json {"text": ...} -> 200 "ok"). With no channel
configured the alert is still recorded (status "recorded") and visible in Settings.
No buyer personal data goes into alerts: case id, reason, amount, recommendation, deadline, link.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Decision, DecisionStatus, Dispute, MerchantSettings, ProcessedEvent

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
WINDOWS = (("due12", timedelta(hours=12)), ("due48", timedelta(hours=48)))  # tightest first


def _row(session: Session) -> MerchantSettings | None:
    return session.get(MerchantSettings, 1)


def toggles(session: Session) -> dict:
    s, m = get_settings(), _row(session)
    on = lambda v: True if v is None else bool(v)  # noqa: E731
    return {
        "proposed": on(m.alert_on_proposed) if m else True,
        "troubleshooting": on(m.alert_on_troubleshooting) if m else True,
        "deadlines": on(m.alert_on_deadlines) if m else True,
        "email_to": (m.alert_email_to if m else None) or None,
        "email_ready": bool(s.smtp_host),
        "slack_ready": bool(s.slack_webhook_url.get_secret_value()),
        "slack": (on(m.alert_slack) if m else True) and bool(s.slack_webhook_url.get_secret_value()),
    }


def case_link(d: Dispute) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/cases/{d.paypal_dispute_id}"


def queue(session: Session, key: str, *, kind: str, dispute_id: int | None, subject: str, text: str) -> bool:
    """Claim + queue one alert. False if it was already queued/sent (dedupe) or this kind is turned off."""
    if not toggles(session).get(kind, True):
        return False
    from app.ingest import claim

    ok = claim(session, key, "alert", dispute_id, {"kind": kind, "subject": subject, "text": text,
                                                    "status": "pending", "attempts": 0})
    session.commit()
    return ok


def on_proposed(session: Session, d: Dispute, dec: Decision) -> bool:
    offer = f" of {dec.offer_amount} {d.currency}" if dec.offer_amount is not None else ""
    return queue(session, f"alert:proposed:{dec.id}", kind="proposed", dispute_id=d.id,
                 subject=f"DisputeShield: {dec.recommendation}{offer} recommended for {d.paypal_dispute_id}",
                 text=f"New recommendation for dispute {d.paypal_dispute_id} ({d.reason.replace('_', ' ').lower()}, "
                      f"{d.amount} {d.currency}): {dec.recommendation}{offer}, {dec.confidence} confidence. "
                      f"Nothing is sent to PayPal until you approve. {case_link(d)}")


def on_troubleshooting_finished(session: Session, d: Dispute, ts) -> bool:  # noqa: ANN001
    status = ts.status.value if hasattr(ts.status, "value") else str(ts.status)
    return queue(session, f"alert:ts:{ts.id}:{status}", kind="troubleshooting", dispute_id=d.id,
                 subject=f"DisputeShield: buyer finished troubleshooting on {d.paypal_dispute_id}",
                 text=f"The buyer finished the troubleshooting page for {d.paypal_dispute_id}: "
                      f"{status.replace('_', ' ').lower()}. The agents re-analyse the case. {case_link(d)}")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def queue_deadlines(session: Session, *, now: datetime | None = None) -> list[str]:
    """Open disputes due within 48 h / 12 h with no approved (or sent) decision: one alert per window and deadline."""
    now = now or datetime.now(timezone.utc)
    out = []
    for d in session.exec(select(Dispute).where(Dispute.status != "RESOLVED",
                                                Dispute.seller_response_due.is_not(None))).all():
        due = _aware(d.seller_response_due)
        left = due - now
        if left <= timedelta(0):
            continue
        answered = session.exec(select(Decision).where(Decision.dispute_id == d.id, Decision.status.in_(
            [DecisionStatus.APPROVED, DecisionStatus.EXECUTED]))).first()
        if answered:
            continue
        for name, window in WINDOWS:
            if left <= window:
                hours = max(1, int(left.total_seconds() // 3600))
                key = f"alert:{name}:{d.id}:{due.isoformat()}"
                if queue(session, key, kind="deadlines", dispute_id=d.id,
                         subject=f"DisputeShield: {d.paypal_dispute_id} is due in {hours} h",
                         text=f"Dispute {d.paypal_dispute_id} ({d.amount} {d.currency}) must be answered in about "
                              f"{hours} hours ({due:%d %b %H:%M} UTC) and nothing is approved yet. {case_link(d)}"):
                    out.append(key)
                break  # only the tightest window that applies
    return out


def _send_email(to: str, subject: str, text: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(text)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
        smtp.send_message(msg)


async def _send_slack(text: str, http: httpx.AsyncClient | None = None) -> None:
    url = get_settings().slack_webhook_url.get_secret_value()
    own = http is None
    http = http or httpx.AsyncClient(timeout=15)
    try:
        r = await http.post(url, json={"text": text})
        if r.status_code != 200:
            raise RuntimeError(f"Slack answered {r.status_code}: {r.text[:100]}")
    finally:
        if own:
            await http.aclose()


async def deliver_pending(session: Session, *, http: httpx.AsyncClient | None = None,
                          send_email=None) -> int:  # noqa: ANN001
    """Send every pending alert once (retry failed ones up to MAX_ATTEMPTS). Returns how many were handled."""
    t = toggles(session)
    rows = [r for r in session.exec(select(ProcessedEvent).where(ProcessedEvent.kind == "alert")).all()
            if (r.detail or {}).get("status") in ("pending", "failed")
            and (r.detail or {}).get("attempts", 0) < MAX_ATTEMPTS]
    for r in rows:
        det = dict(r.detail or {})
        channels, errors = [], []
        if t["email_ready"] and t["email_to"]:
            try:
                await asyncio.to_thread(send_email or _send_email, t["email_to"], det["subject"], det["text"])
                channels.append("email")
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; retried
                errors.append(f"email: {type(exc).__name__}: {str(exc)[:120]}")
        if t["slack"]:
            try:
                await _send_slack(det["text"], http)
                channels.append("slack")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"slack: {type(exc).__name__}: {str(exc)[:120]}")
        det["attempts"] = det.get("attempts", 0) + 1
        det["channels"] = channels
        det["status"] = "failed" if errors and not channels else ("sent" if channels else "recorded")
        det["errors"] = errors
        det["at"] = datetime.now(timezone.utc).isoformat()
        r.detail = det
        session.add(r)
        session.commit()
    return len(rows)


def recent(session: Session, limit: int = 20) -> list[dict]:
    rows = session.exec(select(ProcessedEvent).where(ProcessedEvent.kind == "alert")
                        .order_by(ProcessedEvent.id.desc()).limit(limit)).all()
    return [{"key": r.key, **{k: (r.detail or {}).get(k) for k in ("kind", "subject", "status", "channels", "at")}}
            for r in rows]


async def alerts_loop(engine: Engine, interval_s: float | None = None) -> None:
    interval = interval_s or get_settings().alerts_interval_s
    while True:
        try:
            with Session(engine) as s:
                queue_deadlines(s)
                await deliver_pending(s)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive
            log.exception("alerts: tick failed")
        await asyncio.sleep(interval)

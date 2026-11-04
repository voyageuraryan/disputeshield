"""Phase 9D: turn a finished troubleshooting session into dispute evidence and a new analysis.

record(db, ts)      -> the "troubleshooting_record" evidence (summary + flags) the Investigator stores
finished(db, ts)    -> called when a session reaches a final status: claims an idempotent agent run whose run
                       key includes the troubleshooting status (ingest.run_key), and schedules it
pdf_section(db, d)  -> the troubleshooting page (+ up to 3 proof photos) for the FIGHT evidence PDF

Flags (exactly one outcome flag, plus ts_proof_suspect when a proof check fired):
  ts_fixed             RESOLVED_FIXED (the buyer pressed "Yes, it works now")
  ts_defect_confirmed  DEFECT_CONFIRMED (every step tried, fault remains, proof uploaded)
  ts_safety_stop       SAFETY_STOP
  ts_not_attempted     EXPIRED after an invite that was sent, with the page never opened or no step answered
  ts_partial           anything else that ended (handed to the seller, expired midway)
  ts_proof_suspect     a proof is an exact duplicate (same case or another case of the buyer), was taken before
                       the dispute opened, or the advisory photo check said it doesn't show the step
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models import (AgentStep, Decision, DecisionStatus, Dispute, TroubleshootingSession,
                        TSProof, TSStatus, TSStep)

log = logging.getLogger(__name__)

FINAL = (TSStatus.RESOLVED_FIXED, TSStatus.DEFECT_CONFIRMED, TSStatus.SAFETY_STOP, TSStatus.EXPIRED,
         TSStatus.HANDED_TO_MERCHANT)
OUTCOME_FLAG = {TSStatus.RESOLVED_FIXED: "ts_fixed", TSStatus.DEFECT_CONFIRMED: "ts_defect_confirmed",
                TSStatus.SAFETY_STOP: "ts_safety_stop"}
STATUS_TEXT = {
    TSStatus.RESOLVED_FIXED: "fixed: the buyer confirmed the item works",
    TSStatus.DEFECT_CONFIRMED: "defect confirmed: every step tried, the fault remains, proof uploaded",
    TSStatus.SAFETY_STOP: "stopped for safety",
    TSStatus.EXPIRED: "ended without a result",
    TSStatus.HANDED_TO_MERCHANT: "handed to the seller by the buyer",
}
TOAST_TEXT = {
    TSStatus.RESOLVED_FIXED: "Fixed by troubleshooting", TSStatus.DEFECT_CONFIRMED: "Defect confirmed",
    TSStatus.SAFETY_STOP: "Stopped for safety", TSStatus.EXPIRED: "Ended without a result",
    TSStatus.HANDED_TO_MERCHANT: "Handed to you",
}
PDF_MAX_IMAGES = 3


def session_of(db: Session, dispute_id: int) -> TroubleshootingSession | None:
    return db.exec(select(TroubleshootingSession).where(TroubleshootingSession.dispute_id == dispute_id)).first()


def is_final(ts: TroubleshootingSession | None) -> bool:
    return ts is not None and ts.status in FINAL and ts.ended_reason != "dispute resolved"


def _steps(db: Session, ts: TroubleshootingSession) -> list[TSStep]:
    return list(db.exec(select(TSStep).where(TSStep.session_id == ts.id).order_by(TSStep.ordinal)).all())


def _proofs(db: Session, ts: TroubleshootingSession) -> list[TSProof]:
    return list(db.exec(select(TSProof).where(TSProof.session_id == ts.id).order_by(TSProof.id)).all())


def suspect_reasons(p: TSProof) -> list[str]:
    c, v = p.checks or {}, p.vision_verdict or {}
    out = []
    if c.get("reused_from_other_cases"):
        out.append(f"same file as case {', '.join(c['reused_from_other_cases'])}")
    if c.get("duplicate_of_steps"):
        out.append(f"same file as step {', '.join(map(str, c['duplicate_of_steps']))}")
    if c.get("taken_after_dispute") == "no":
        out.append("taken before the dispute was opened")
    if v.get("shows_step") == "no":
        out.append("the advisory photo check says it doesn't show the step")
    return out


@dataclass
class Record:
    summary: str
    flags: list[str]
    suspect: list[str] = field(default_factory=list)  # human-readable reasons (merchant only)


def record(db: Session, ts: TroubleshootingSession) -> Record | None:
    """The evidence for a finished session (None while it's still running)."""
    if not is_final(ts):
        return None
    steps, proofs = _steps(db, ts), _proofs(db, ts)
    answered = [s for s in steps if s.status != "PENDING"]
    if ts.status in OUTCOME_FLAG:
        flags = [OUTCOME_FLAG[ts.status]]
    elif ts.status == TSStatus.EXPIRED and ts.invited_at and (ts.buyer_last_seen_at is None or not answered):
        flags = ["ts_not_attempted"]
    else:
        flags = ["ts_partial"]
    suspect = [f"proof {i + 1} (step {next((s.ordinal for s in steps if s.id == p.step_id), '?')}): {r}"
               for i, p in enumerate(proofs) for r in suspect_reasons(p)]
    if suspect:
        flags.append("ts_proof_suspect")
    counts: dict[str, int] = {}
    for s in answered:
        counts[s.status] = counts.get(s.status, 0) + 1
    words = {"DONE_FIXED": "worked", "DONE_NOT_FIXED": "done but still not working", "CANT_DO": "couldn't do",
             "SKIPPED": "skipped"}
    photos = sum(p.mime.startswith("image/") for p in proofs)
    concerns = [(p.vision_verdict or {}).get("concerns") for p in proofs]
    looks = [f"{(p.vision_verdict or {}).get('shows_step')}" for p in proofs if (p.vision_verdict or {}).get("shows_step")]
    parts = [
        f"Buyer troubleshooting for the {ts.product_name}" + (f" ({ts.product_model})" if ts.product_model else "")
        + f": {STATUS_TEXT.get(ts.status, ts.status.value)}",
        (f"invited {ts.invited_at:%d %b} via {'PayPal message' if ts.invited_via == 'paypal_message' else 'email'}"
         if ts.invited_at else "not invited"),
        "the buyer opened the page" if ts.buyer_last_seen_at else "the buyer never opened the page",
        f"steps answered {len(answered)} of {len(steps)}"
        + (" (" + ", ".join(f"{n} {words.get(k, k.lower())}" for k, n in counts.items()) + ")" if counts else ""),
        f"proofs uploaded: {photos} photo(s), {len(proofs) - photos} video(s)",
    ]
    if ts.symptom:
        parts.append(f"problem described: {ts.symptom}")
    if looks:
        parts.append("advisory photo check (shows the step): " + ", ".join(looks))
    if any(concerns):
        parts.append("photo check concerns: " + "; ".join(c for c in concerns if c)[:200])
    return Record("; ".join(parts) + ".", flags, suspect)


# ---- a finished session -> one new agent run -----------------------------------------------------------

Scheduler = Callable[[int, str, Engine], None]
_scheduler: Scheduler | None = None


def install(loop: asyncio.AbstractEventLoop) -> None:
    """Called at app start-up: runs are scheduled on the app's event loop (from any thread)."""
    global _scheduler

    def schedule(dispute_id: int, key: str, engine: Engine) -> None:
        from app.agents.runner import run_agents

        asyncio.run_coroutine_threadsafe(run_agents(dispute_id, key, engine), loop)

    _scheduler = schedule


def set_scheduler(fn: Scheduler | None) -> None:  # tests
    global _scheduler
    _scheduler = fn


def rerun_key(dispute: Dispute, ts_status: str) -> str:
    """The same key ingest computes for this PayPal state + this troubleshooting result."""
    from app.ingest import run_key
    from app.paypal.disputes import Dispute as PayPalDispute

    raw = dict(dispute.raw or {})
    pp = PayPalDispute.model_validate({**raw, "dispute_id": dispute.paypal_dispute_id})
    pp._raw = raw
    return run_key(pp, ts_status=ts_status)


def finished(db: Session, ts: TroubleshootingSession) -> str | None:
    """The session just reached a final status: record when, then claim and schedule one new analysis.
    Idempotent: the run key includes the status, so the poller/webhook seeing the same PayPal state later
    won't run it again. Returns the claimed run key (None if nothing was started)."""
    from app.config import get_settings
    from app.ingest import claim

    if ts.finished_at is None:
        ts.finished_at = datetime.now(timezone.utc)
        db.add(ts)
        db.commit()
    if is_final(ts):  # Phase 17: tell the merchant (deduplicated per session + final status)
        try:
            from app.alerts import on_troubleshooting_finished

            d = db.get(Dispute, ts.dispute_id)
            if d is not None:
                on_troubleshooting_finished(db, d, ts)
        except Exception:  # never break the buyer's request over an alert
            log.exception("couldn't queue the troubleshooting alert for session %s", ts.id)
    if not is_final(ts) or _scheduler is None or not get_settings().troubleshooting_reanalyse:
        return None
    dispute = db.get(Dispute, ts.dispute_id)
    if dispute is None or dispute.status == "RESOLVED":
        return None
    sending = db.exec(select(Decision).where(Decision.dispute_id == dispute.id,
                                             Decision.status == DecisionStatus.APPROVED)).first()
    if sending:  # a response is being sent to PayPal right now; the next PayPal update re-analyses
        return None
    key = rerun_key(dispute, ts.status.value)
    if not claim(db, key, "agent_run", dispute.id, {"troubleshooting": ts.status.value}):
        db.commit()
        return None
    db.add(AgentStep(dispute_id=dispute.id, agent="orchestrator",
                     action=f"Buyer finished troubleshooting ({TOAST_TEXT[ts.status].lower()}): re-analysing the "
                            "case with the result",
                     detail={"run_key": key, "session_id": ts.id, "status": ts.status.value}))
    db.commit()
    try:
        _scheduler(dispute.id, key, db.get_bind())
    except Exception:  # never break the buyer's request over this; the merchant can re-analyse
        log.exception("couldn't schedule the troubleshooting re-analysis for dispute %s", dispute.id)
    return key


# ---- evidence PDF section ------------------------------------------------------------------------------

@dataclass
class PdfSection:
    title: str
    lines: list[str]
    steps: list[tuple[str, str, str]]   # (ordinal, title, buyer's answer)
    images: list[tuple[str, str]]       # (file path, caption)


def pdf_section(db: Session, dispute_id: int) -> PdfSection | None:
    ts = session_of(db, dispute_id)
    rec = record(db, ts) if ts else None
    if rec is None:
        return None
    steps = _steps(db, ts)
    answers = {"PENDING": "not answered", "DONE_FIXED": "it worked", "DONE_NOT_FIXED": "done, still not working",
               "CANT_DO": "couldn't do it", "SKIPPED": "skipped"}
    by_id = {s.id: s for s in steps}
    images = []
    for p in _proofs(db, ts):
        if p.mime in ("image/jpeg", "image/png") and len(images) < PDF_MAX_IMAGES and not suspect_reasons(p):
            st = by_id.get(p.step_id)
            when = (p.checks or {}).get("capture_time")
            images.append((p.file_path, f"Buyer's photo for step {st.ordinal if st else '?'}"
                           + (f" ({st.title})" if st else "") + (f", taken {when[:16].replace('T', ' ')}" if when else "")
                           + f", uploaded {p.created_at:%d %b %Y}"))
    lines = [rec.summary]
    if ts.finished_at:
        lines.append(f"Session ended {ts.finished_at:%d %b %Y %H:%M} UTC.")
    lines.append("Photos are the buyer's own uploads from the troubleshooting page (location data removed).")
    return PdfSection("Buyer troubleshooting record", lines,
                      [(str(s.ordinal), s.title, answers.get(s.status, s.status)) for s in steps], images)

"""Buyer proof files: retention and deletion (G44).

- Retention: PROOF_RETENTION_DAYS (default 90; 0 = keep) after the dispute is resolved, the files (stored copy and
  evidence copy) are deleted. The proof row and its check results stay for the merchant's records, marked
  checks.purged_at; the file endpoints answer 410. Runs every RETENTION_INTERVAL_H in the app (lifespan) and via
  `python -m app.scripts.purge_proofs`.
- Buyer deletion: while the troubleshooting page is open the buyer may delete their own upload (row and files).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.agents.steps import log_step
from app.config import get_settings
from app.models import TS_OPEN_STATUSES, Dispute, TroubleshootingSession, TSProof, TSStatus, TSStep

log = logging.getLogger(__name__)
RETENTION_INTERVAL_H = 6


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolved_at(d: Dispute) -> datetime | None:
    """When PayPal resolved the dispute (its update_time on the resolved copy; else when we last fetched it)."""
    if d.status != "RESOLVED":
        return None
    raw = (d.raw or {}).get("update_time")
    if raw:
        try:
            return _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            pass
    return _aware(d.refreshed_at) if d.refreshed_at else None


def purged(p: TSProof) -> bool:
    return bool((p.checks or {}).get("purged_at"))


def _remove(*paths: str | None) -> None:
    for path in {p for p in paths if p}:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.warning("couldn't delete %s", path)


def purge_expired_proofs(db: Session, *, now: datetime | None = None, days: int | None = None) -> list[int]:
    """Delete proof files of disputes resolved more than `days` ago. Returns the purged proof ids."""
    days = get_settings().proof_retention_days if days is None else days
    if days <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    rows = db.exec(select(TSProof, TroubleshootingSession, Dispute)
                   .where(TSProof.session_id == TroubleshootingSession.id,
                          TroubleshootingSession.dispute_id == Dispute.id, Dispute.status == "RESOLVED")).all()
    done: dict[int, list[int]] = {}
    for proof, ts, d in rows:
        when = resolved_at(d)
        if purged(proof) or when is None or when + timedelta(days=days) > now:
            continue
        _remove(proof.file_path, proof.evidence_path)
        proof.checks = {**(proof.checks or {}), "purged_at": now.isoformat(), "purged_reason":
                        f"deleted {days} days after the dispute was resolved (retention policy)"}
        proof.file_path, proof.evidence_path = "", None
        db.add(proof)
        done.setdefault(d.id, []).append(proof.id)
    db.commit()
    for dispute_id, ids in done.items():
        log_step(db, dispute_id, "troubleshooter", f"Deleted {len(ids)} buyer proof file(s) under the retention "
                                                   f"policy ({days} days after the dispute was resolved)",
                 {"proof_ids": ids})
    return [i for ids in done.values() for i in ids]


class DeleteRefused(ValueError):
    pass


def buyer_delete(db: Session, ts: TroubleshootingSession, proof_id: int) -> TSStep | None:
    """The buyer removes their own upload while the page is open. Returns its step."""
    proof = db.get(TSProof, proof_id)
    if proof is None or proof.session_id != ts.id:
        raise LookupError(proof_id)
    if ts.status not in TS_OPEN_STATUSES:
        raise DeleteRefused("This troubleshooting session has ended, so uploads can't be changed. Ask the seller.")
    step = db.get(TSStep, proof.step_id) if proof.step_id else None
    _remove(proof.file_path, proof.evidence_path)
    db.delete(proof)
    if step is not None and step.requires_proof and step.status != "PENDING" and ts.status == TSStatus.ACTIVE:
        others = db.exec(select(TSProof).where(TSProof.step_id == step.id, TSProof.id != proof_id)).first()
        if others is None:
            ts.status = TSStatus.AWAITING_PROOF  # the step still needs a photo/video
            db.add(ts)
    db.commit()
    log_step(db, ts.dispute_id, "troubleshooter",
             "The buyer deleted an upload" + (f" for step {step.ordinal}" if step else ""),
             {"session_id": ts.id, "proof_id": proof_id})
    return step


async def retention_loop(engine: Engine, interval_h: float = RETENTION_INTERVAL_H) -> None:
    while True:
        try:
            with Session(engine) as db:
                ids = purge_expired_proofs(db)
            if ids:
                log.info("retention: purged %d proof file(s)", len(ids))
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive
            log.exception("retention: purge failed")
        await asyncio.sleep(interval_h * 3600)

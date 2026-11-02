"""In-process wake-ups for the SSE streams.

When a transaction that inserted AgentStep or Dispute rows commits, waiting streams are woken at once instead
of discovering the rows on their next poll. Streams still poll slowly as a safety net (rows written by another
process, e.g. a CLI script). Single-process only by design (see PROGRESS G12); a multi-process deploy would
swap this for Postgres LISTEN/NOTIFY or Redis pub/sub behind the same notify()/wait() calls.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import event
from sqlmodel import Session

_loop: asyncio.AbstractEventLoop | None = None
_waiters: dict[str, set[asyncio.Event]] = defaultdict(set)


def steps_key(dispute_id: int) -> str:
    return f"steps:{dispute_id}"


DISPUTES_KEY = "disputes"


def help_key(session_id: int) -> str:
    """Buyer troubleshooting page stream (new chat messages, step / status / proof changes)."""
    return f"help:{session_id}"


def _wake(key: str) -> None:
    for ev in list(_waiters.get(key, ())):
        ev.set()


def notify(key: str) -> None:
    """Thread-safe: may be called from any thread (agent background tasks, request handlers)."""
    loop = _loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(_wake, key)


async def wait(key: str, timeout: float) -> bool:
    """Sleep until notify(key) or timeout. Returns True if woken by a notification."""
    global _loop
    _loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    _waiters[key].add(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        _waiters[key].discard(ev)
        if not _waiters[key]:
            _waiters.pop(key, None)


# ---- hook into SQLAlchemy: collect inserted rows at flush, notify after the commit is visible ----

@event.listens_for(Session, "after_flush")
def _collect(session, flush_context):  # noqa: ANN001
    from app.models import AgentStep, Dispute, TroubleshootingSession, TSMessage, TSProof, TSSource, TSStep

    keys = session.info.setdefault("ds_notify", set())
    for obj in session.new:
        if isinstance(obj, AgentStep):
            keys.add(steps_key(obj.dispute_id))
        elif isinstance(obj, Dispute):
            keys.add(DISPUTES_KEY)
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, (TSMessage, TSStep, TSProof, TSSource)) and obj.session_id:
            keys.add(help_key(obj.session_id))
        elif isinstance(obj, TroubleshootingSession) and obj.id:
            keys.add(help_key(obj.id))
            if obj.finished_at is not None:  # 9D: "buyer finished troubleshooting" toast on the merchant's app
                keys.add(DISPUTES_KEY)


@event.listens_for(Session, "after_commit")
def _publish(session):  # noqa: ANN001
    for key in session.info.pop("ds_notify", set()):
        notify(key)


@event.listens_for(Session, "after_rollback")
def _discard(session):  # noqa: ANN001
    session.info.pop("ds_notify", None)

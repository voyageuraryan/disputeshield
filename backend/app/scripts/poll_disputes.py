"""Poll PayPal for disputes and ingest any new or changed ones.

Runs as a startup background loop (every POLL_INTERVAL_S, default 60s) as a safety
net for slow or missed webhooks, and as a one-off script:

    cd backend && python -m app.scripts.poll_disputes [--loop]
    cd backend && python -m app.scripts.poll_disputes --id PP-R-ZWL-10195422   # ingest one case

--id exists because the sandbox list/search endpoint can lag far behind (seen
2026-10-31: a new case was readable via GET /disputes/{id} while every list
filter, even disputed_transaction_id, returned nothing).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.agents.runner import run_agents
from app.ingest import IngestResult, ingest_dispute
from app.models import Dispute
from app.paypal.client import PayPalClient, PayPalError
from app.paypal.disputes import DisputeSummary, list_disputes

log = logging.getLogger(__name__)
PAGE_SIZE = 50  # spec maximum
MAX_PAGES = 50  # spec maximum for `page`


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def needs_ingest(session: Session, summary: DisputeSummary) -> bool:
    row = session.exec(select(Dispute).where(Dispute.paypal_dispute_id == summary.dispute_id)).first()
    if row is None:
        return True
    stored = _parse((row.raw or {}).get("update_time"))
    return summary.update_time is None or stored is None or summary.update_time > stored


async def poll_once(engine: Engine, client: PayPalClient | None = None) -> list[IngestResult]:
    results: list[IngestResult] = []
    for page in range(1, MAX_PAGES + 1):
        listing = await list_disputes(page=page, page_size=PAGE_SIZE, client=client)
        for summary in listing.items:
            with Session(engine) as session:
                if not needs_ingest(session, summary):
                    continue
                try:
                    result = await ingest_dispute(session, summary.dispute_id, client=client)
                except PayPalError as exc:
                    session.rollback()
                    log.error("poll: failed to ingest %s: %s", summary.dispute_id, exc)
                    continue
            results.append(result)
            if result.run_key:
                await run_agents(result.dispute_row_id, result.run_key, engine)
        if len(listing.items) < PAGE_SIZE:
            break
    return results


async def ingest_by_id(engine: Engine, dispute_id: str, client: PayPalClient | None = None) -> IngestResult:
    """Ingest one dispute by case ID (same path as webhook/poll) and run agents if a new run was claimed."""
    with Session(engine) as session:
        result = await ingest_dispute(session, dispute_id, client=client)
    if result.run_key:
        await run_agents(result.dispute_row_id, result.run_key, engine)
    return result


async def poll_forever(engine: Engine, interval_s: int, client: PayPalClient | None = None) -> None:
    while True:
        try:
            results = await poll_once(engine, client)
            if results:
                log.info("poll: ingested %d dispute(s)", len(results))
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive; next tick retries
            log.exception("poll: tick failed")
        await asyncio.sleep(interval_s)


async def _main(argv: list[str] | None = None) -> int:
    from app.config import get_settings
    from app.db import create_db_and_tables, engine
    from app.paypal.client import close_paypal_client

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="keep polling every POLL_INTERVAL_S")
    ap.add_argument("--id", dest="dispute_ids", action="append", default=[],
                    help="ingest this dispute/case ID directly (repeatable)")
    args = ap.parse_args(argv)
    create_db_and_tables()
    try:
        if args.dispute_ids:
            results = [await ingest_by_id(engine, d) for d in args.dispute_ids]
        else:
            if args.loop:
                await poll_forever(engine, get_settings().poll_interval_s)
            results = await poll_once(engine)
    except (PayPalError, RuntimeError, ValueError) as exc:
        print(f"poll failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await close_paypal_client()
    for r in results:
        print(f"{r.paypal_dispute_id}: order={r.order_id} agent_run_queued={bool(r.run_key)}")
    print(f"{len(results)} dispute(s) ingested")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_main()))

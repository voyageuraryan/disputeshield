"""Remove abandoned storefront checkouts: orders sent to PayPal but never captured, with no dispute.

PayPal orders that are never approved simply expire, so these rows are clutter (they inflate the store's
order list and buyer history). Dry run by default.

Usage: cd backend && python -m app.scripts.cleanup_orders [--min-age-hours 3] [--apply]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models import Dispute, Message, Order, Shipment


def abandoned(session: Session, min_age_hours: float, now: datetime | None = None) -> list[Order]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=min_age_hours)
    rows = session.exec(select(Order).where(Order.paypal_order_id.is_not(None), Order.capture_id.is_(None))).all()
    keep_ids = {d.order_id for d in session.exec(select(Dispute).where(Dispute.order_id.is_not(None))).all()}
    out = []
    for o in rows:
        created = o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=timezone.utc)
        if o.id not in keep_ids and created <= cutoff and not o.demo_scenario:  # demo orders stay for the demo
            out.append(o)
    return out


def delete_orders(session: Session, orders: list[Order]) -> int:
    for o in orders:
        for model in (Shipment, Message):
            for row in session.exec(select(model).where(model.order_id == o.id)).all():
                session.delete(row)
        session.delete(o)
    session.commit()
    return len(orders)


def main(argv: list[str] | None = None) -> int:
    from app.db import create_db_and_tables, engine

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-age-hours", type=float, default=3.0, help="only orders older than this (default 3)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args(argv)
    create_db_and_tables()
    with Session(engine) as s:
        rows = abandoned(s, args.min_age_hours)
        for o in rows:
            print(f"order #{o.id}  PayPal {o.paypal_order_id}  {o.amount} {o.currency}  created {o.created_at:%Y-%m-%d %H:%M}")
        if not args.apply:
            print(f"{len(rows)} abandoned checkout(s) would be deleted (dry run; add --apply)")
            return 0
        print(f"deleted {delete_orders(s, rows)} abandoned checkout(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

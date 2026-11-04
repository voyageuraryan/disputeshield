"""Delete buyer proof files past the retention period (G44): python -m app.scripts.purge_proofs [--days N]
The app also does this every 6 hours (app/troubleshooting/retention.py)."""

from __future__ import annotations

import argparse

from sqlmodel import Session

from app.db import create_db_and_tables, engine
from app.troubleshooting.retention import purge_expired_proofs, purge_expired_uploads, purge_signal_hashes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=None, help="override PROOF_RETENTION_DAYS")
    args = ap.parse_args(argv)
    create_db_and_tables()
    with Session(engine) as db:
        ids = purge_expired_proofs(db, days=args.days)
        ups = purge_expired_uploads(db, days=args.days)
        sig = purge_signal_hashes(db)
    print(f"purged {len(ids)} proof file(s), {len(ups)} merchant upload file(s), signal hashes of {sig} order(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

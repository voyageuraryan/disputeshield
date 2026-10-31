"""Print the disputes in your PayPal sandbox account.

Usage: cd backend && python -m app.scripts.check_paypal [--state REQUIRED_ACTION] [--page-size 20]
"""

import argparse
import asyncio
import logging
import sys

from app.paypal.client import PayPalError, close_paypal_client
from app.paypal.disputes import list_disputes


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", help="dispute_state filter, comma-separated")
    ap.add_argument("--page-size", type=int, default=50)
    args = ap.parse_args(argv)

    try:
        result = await list_disputes(page_size=args.page_size, dispute_state=args.state)
    except PayPalError as exc:
        print(f"PayPal error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await close_paypal_client()

    if not result.items:
        print("No disputes found in this sandbox account.")
        return 0
    print(f"{'DISPUTE ID':<24} {'REASON':<40} {'STATUS':<28} {'STAGE':<12} {'AMOUNT':>12}  SELLER DUE")
    for d in result.items:
        amt = f"{d.dispute_amount.value} {d.dispute_amount.currency_code}" if d.dispute_amount else "-"
        due = d.seller_response_due_date.isoformat() if d.seller_response_due_date else "-"
        print(
            f"{d.dispute_id:<24} {d.reason or '-':<40} {d.status or '-':<28} "
            f"{d.dispute_life_cycle_stage or '-':<12} {amt:>12}  {due}"
        )
    print(f"\n{len(result.items)} dispute(s)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))

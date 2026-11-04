"""Seed store-side demo data: 16 orders with shipments and buyer/seller messages.

Only store tables (orders, shipments, messages) are written. No PayPal data is
faked: paypal_order_id / capture_id stay empty, and no disputes are created.
Real disputes come from sandbox purchases on the Store page.

Demo cases (look them up by buyer_email):
  A) case-a@demo.test  $89 lamp, tracked, delivered to the buyer's address, "got it, thanks"
  B) case-b@demo.test  $12 phone case, no tracking, buyer says item differs from listing
  C) case-c@demo.test  $45 mug, tracked, delivered to a different address
  D) case-d@demo.test  $59 wireless earbuds (electronics), delivered, buyer says one earbud won't charge

Usage: cd backend && python -m app.scripts.seed [--reset]
       python -m app.scripts.seed --demo-history | --clear-demo-history   (Analytics demo data only)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, delete, select

from app.catalog import PRODUCTS, order_item
from app.models import Dispute, Message, MessageSender, Order, Shipment

DEMO_DOMAIN = "demo.test"


def _addr(line1: str, city: str, state: str, postal: str) -> dict:
    # Same shape as Orders v2 address_portable, so seeded and real orders look alike.
    return {"address_line_1": line1, "admin_area_2": city, "admin_area_1": state,
            "postal_code": postal, "country_code": "US"}


ADDR = {
    "a": _addr("12 Oak Street", "Portland", "OR", "97205"),
    "b": _addr("401 Pine Ave", "Austin", "TX", "78701"),
    "c": _addr("88 Lake Road", "Madison", "WI", "53703"),
    "c_wrong": _addr("1540 Industrial Pkwy", "Milwaukee", "WI", "53204"),
}

# (key, sku, buyer, address, days_ago, shipment, messages)
#   shipment: None or (carrier, tracking, status, delivered_days_after, delivered_address_key|None)
#   messages: list of (sender, body, hours_after_order)
SPEC: list[tuple] = [
    ("A", "LAMP-01", "case-a", "a", 20,
     ("USPS", "9400111899223344556677", "DELIVERED", 4, "a"),
     [("seller", "Thanks for your order! Your lamp ships today with USPS tracking.", 2),
      ("buyer", "got it, thanks", 24 * 5)]),
    ("B", "CASE-01", "case-b", "b", 18,
     None,
     [("seller", "Your case is on its way by standard mail.", 3),
      ("buyer", "The case I received is not what was in the listing. It's a frosted/matte case and it "
                "doesn't fit my iPhone 15 - the listing said clear and made for iPhone 15.", 24 * 9)]),
    ("C", "MUG-01", "case-c", "c", 16,
     ("UPS", "1Z999AA10123456784", "DELIVERED", 5, "c_wrong"),
     [("seller", "Your mug has shipped with UPS.", 4),
      ("buyer", "Tracking says delivered but nothing arrived at my house.", 24 * 6)]),
    ("D", "EARBUDS-01", "case-d", "a", 9,
     ("UPS", "1Z999AA10123456787", "DELIVERED", 3, "a"),
     [("seller", "Your SoundPeak E5 earbuds have shipped with UPS.", 3),
      ("buyer", "The left earbud won't charge in the case - the light never comes on. The right one is fine.",
       24 * 5)]),
    # Filler orders: a realistic mix of happy paths and noise.
    ("", "LAMP-01", "maria", "a", 30, ("FEDEX", "771234567890", "DELIVERED", 3, "same"),
     [("buyer", "Beautiful lamp, love the grain!", 24 * 4)]),
    ("", "MUG-01", "devon", "b", 28, ("USPS", "9400111899220000000001", "DELIVERED", 4, "same"), []),
    ("", "EBOOK-01", "lee", None, 27, None,
     [("buyer", "Download worked fine, thank you.", 1)]),
    ("", "CASE-01", "sam", "c", 25, None, []),
    ("", "MUG-01", "priya", "a", 22, ("UPS", "1Z999AA10123456785", "DELIVERED", 5, "same"),
     [("buyer", "Is it microwave safe?", 30), ("seller", "Yes, microwave and dishwasher safe.", 32)]),
    ("", "EBOOK-01", "tom", None, 15, None, []),
    ("", "LAMP-01", "nina", "b", 12, ("USPS", "9400111899220000000002", "IN_TRANSIT", None, None),
     [("buyer", "Any update on shipping?", 24 * 5), ("seller", "It's in transit, should arrive in 2 days.", 24 * 5 + 3)]),
    ("", "CASE-01", "omar", "c", 10, None,
     [("buyer", "Arrived, fits perfectly.", 24 * 7)]),
    ("", "MUG-01", "jules", "a", 8, ("FEDEX", "771234567891", "DELIVERED", 3, "same"), []),
    ("", "EBOOK-01", "kai", None, 6, None,
     [("buyer", "I never got the download email.", 2), ("seller", "Resent it just now - please check spam.", 5)]),
    ("", "LAMP-01", "rosa", "c", 4, ("UPS", "1Z999AA10123456786", "SHIPPED", None, None), []),
    ("", "CASE-01", "ben", "b", 2, None, []),
]


def seed(session: Session, *, reset: bool = False, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    if session.exec(select(Order).limit(1)).first() is not None:
        if not reset:
            raise RuntimeError("orders already exist; re-run with --reset to replace store-side demo data")
        if session.exec(select(Dispute).where(Dispute.order_id.is_not(None)).limit(1)).first() is not None:
            raise RuntimeError("refusing to reset: disputes are linked to orders")
        session.exec(delete(Message))
        session.exec(delete(Shipment))
        session.exec(delete(Order))
        session.commit()

    ids: dict[str, int] = {}
    for key, sku, buyer, addr_key, days_ago, ship, msgs in SPEC:
        product = PRODUCTS[sku]
        created = now - timedelta(days=days_ago)
        address = ADDR[addr_key] if addr_key else None
        order = Order(
            buyer_email=f"{buyer}@{DEMO_DOMAIN}",
            items=[order_item(product, 1)],
            amount=product.price,
            currency=product.currency,
            shipping_address=address,
            product_listing=product.listing,
            created_at=created,
        )
        session.add(order)
        session.flush()
        if ship:
            carrier, tracking, status, delivered_after, deliv_key = ship
            delivered_addr = None
            if deliv_key == "same":
                delivered_addr = address
            elif deliv_key:
                delivered_addr = ADDR[deliv_key]
            session.add(Shipment(
                order_id=order.id, carrier=carrier, tracking_number=tracking, status=status,
                delivered_at=created + timedelta(days=delivered_after) if delivered_after else None,
                delivered_address=delivered_addr,
            ))
        for sender, body, hours in msgs:
            session.add(Message(order_id=order.id, sender=MessageSender(sender), body=body,
                                sent_at=created + timedelta(hours=hours)))
        if key:
            ids[key] = order.id
    session.commit()
    return ids


def main(argv: list[str] | None = None) -> int:
    from app.db import create_db_and_tables, engine

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete existing orders/shipments/messages first")
    ap.add_argument("--demo-history", action="store_true",
                    help="ONLY (re)write the labelled demo outcome history for the Analytics page (is_demo rows, "
                         "never mixed with real cases); nothing else is seeded")
    ap.add_argument("--clear-demo-history", action="store_true", help="remove the demo outcome history")
    args = ap.parse_args(argv)
    create_db_and_tables()
    if args.demo_history or args.clear_demo_history:
        from app.analytics.demo import clear_demo_history, replace_demo_history

        with Session(engine) as session:
            if args.clear_demo_history:
                print(f"Removed {clear_demo_history(session)} demo outcome(s).")
            else:
                print(f"Wrote {replace_demo_history(session)} demo outcomes (labelled DEMO-####, is_demo).")
        return 0
    with Session(engine) as session:
        try:
            ids = seed(session, reset=args.reset)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"Seeded {len(SPEC)} orders. Demo cases: " + ", ".join(f"{k}=order {v}" for k, v in ids.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

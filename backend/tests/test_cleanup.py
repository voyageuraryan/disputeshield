from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session, create_engine, select

from app.db import create_db_and_tables
from app.models import Dispute, Order, Shipment
from app.scripts.cleanup_orders import abandoned, delete_orders

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_only_old_unpaid_undisputed_checkouts_are_removed():
    eng = create_engine("sqlite://")
    create_db_and_tables(eng)
    with Session(eng) as s:
        mk = lambda **kw: Order(amount=Decimal("1"), currency="USD", created_at=NOW - timedelta(hours=5), **kw)  # noqa: E731
        stale = mk(paypal_order_id="A")
        paid = mk(paypal_order_id="B", capture_id="CAP")
        seeded = mk()  # store-only seed data, never sent to PayPal
        fresh = Order(amount=Decimal("1"), currency="USD", created_at=NOW - timedelta(minutes=10), paypal_order_id="C")
        disputed = mk(paypal_order_id="D")
        s.add_all([stale, paid, seeded, fresh, disputed]); s.commit()
        s.add(Dispute(paypal_dispute_id="PP-1", order_id=disputed.id, reason="X", status="OPEN", amount=Decimal("1"),
                      currency="USD"))
        s.add(Shipment(order_id=stale.id, carrier="USPS")); s.commit()
        found = abandoned(s, 3, now=NOW)
        assert [o.paypal_order_id for o in found] == ["A"]
        assert delete_orders(s, found) == 1
        assert {o.paypal_order_id for o in s.exec(select(Order)).all()} == {"B", None, "C", "D"}
        assert s.exec(select(Shipment)).all() == []

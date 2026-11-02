from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, create_engine, select

from app.db import create_db_and_tables
from app.models import Dispute, Message, MessageSender, Order, Shipment
from app.scripts.seed import seed

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    create_db_and_tables(eng)
    with Session(eng) as s:
        yield s


def _order(s, oid):
    o = s.get(Order, oid)
    return o, s.exec(select(Shipment).where(Shipment.order_id == oid)).all(), \
        s.exec(select(Message).where(Message.order_id == oid)).all()


def test_seed_counts_and_no_fake_paypal_data(session):
    seed(session, now=NOW)
    orders = session.exec(select(Order)).all()
    assert len(orders) == 16
    assert {o.items[0]["sku"] for o in orders} == {"LAMP-01", "EARBUDS-01", "CASE-01", "EBOOK-01", "MUG-01"}
    # category + model are stored with each line at order time
    assert all(o.items[0]["category"] in ("electronics", "accessory", "digital", "home") for o in orders)
    assert {o.items[0]["model"] for o in orders if o.items[0]["category"] == "electronics"} == {"Lumina LD-200", "SoundPeak E5"}
    assert all(o.paypal_order_id is None and o.capture_id is None for o in orders)
    assert session.exec(select(Dispute)).all() == []
    assert session.exec(select(Shipment)).all()
    senders = {m.sender for m in session.exec(select(Message)).all()}
    assert senders == {MessageSender.buyer, MessageSender.seller}


def test_case_a_lamp_delivered_to_buyer(session):
    ids = seed(session, now=NOW)
    o, ships, msgs = _order(session, ids["A"])
    assert o.amount == Decimal("89.00") and o.items[0]["sku"] == "LAMP-01"
    assert len(ships) == 1 and ships[0].tracking_number and ships[0].status == "DELIVERED"
    assert ships[0].delivered_address == o.shipping_address
    assert ships[0].delivered_at is not None
    assert any(m.sender == MessageSender.buyer and m.body == "got it, thanks" for m in msgs)


def test_case_b_phone_case_untracked_not_as_described(session):
    ids = seed(session, now=NOW)
    o, ships, msgs = _order(session, ids["B"])
    assert o.amount == Decimal("12.00") and o.items[0]["sku"] == "CASE-01"
    assert ships == []
    buyer = [m.body for m in msgs if m.sender == MessageSender.buyer]
    assert any("not what was in the listing" in b for b in buyer)


def test_case_c_mug_delivered_elsewhere(session):
    ids = seed(session, now=NOW)
    o, ships, _ = _order(session, ids["C"])
    assert o.amount == Decimal("45.00") and o.items[0]["sku"] == "MUG-01"
    assert ships[0].tracking_number and ships[0].status == "DELIVERED"
    assert ships[0].delivered_address != o.shipping_address


def test_seed_refuses_twice_and_resets(session):
    seed(session, now=NOW)
    with pytest.raises(RuntimeError, match="--reset"):
        seed(session, now=NOW)
    seed(session, reset=True, now=NOW)
    assert len(session.exec(select(Order)).all()) == 16


def test_reset_refuses_when_disputes_linked(session):
    ids = seed(session, now=NOW)
    session.add(Dispute(paypal_dispute_id="PP-D-1", order_id=ids["A"], reason="X", status="OPEN",
                        amount=Decimal("1"), currency="USD"))
    session.commit()
    with pytest.raises(RuntimeError, match="linked"):
        seed(session, reset=True, now=NOW)


def test_case_d_earbuds_electronics(session):
    ids = seed(session, now=NOW)
    o, ships, msgs = _order(session, ids["D"])
    item = o.items[0]
    assert (item["sku"], item["category"], item["model"]) == ("EARBUDS-01", "electronics", "SoundPeak E5")
    assert ships[0].status == "DELIVERED"
    assert any("won't charge" in m.body for m in msgs if m.sender == MessageSender.buyer)

"""After an empty-database restart, the investigator rebuilds this store's order from PayPal (capture -> order)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.db import create_db_and_tables

from app.agents.investigator import Investigator
from app.models import Dispute, Order, Shipment

CAPTURE = {
    "id": "CAP123", "status": "PENDING", "amount": {"currency_code": "USD", "value": "12.00"},
    "invoice_id": "DS-44-cb1ffa73", "create_time": "2026-11-02T10:00:00Z",
    "supplementary_data": {"related_ids": {"order_id": "ORDER9"}},
    "seller_protection": {"status": "ELIGIBLE", "dispute_categories": ["ITEM_NOT_RECEIVED"]},
}


def _order(invoice: str = "DS-44-cb1ffa73") -> dict:
    return {
        "id": "ORDER9", "status": "COMPLETED", "create_time": "2026-11-02T09:59:00Z",
        "payer": {"email_address": "buyer@personal.example.com", "payer_id": "PAYER1"},
        "purchase_units": [{
            "invoice_id": invoice, "custom_id": "44", "amount": {"currency_code": "USD", "value": "12.00"},
            "items": [{"name": "Clear phone case", "sku": "CASE-01", "quantity": "1",
                       "unit_amount": {"currency_code": "USD", "value": "12.00"}, "category": "PHYSICAL_GOODS"}],
            "shipping": {"address": {"address_line_1": "1 Main St", "admin_area_2": "Austin", "admin_area_1": "TX",
                                     "postal_code": "78701", "country_code": "US"},
                         "trackers": [{"id": "CAP123-9400111899223397846521", "status": "SHIPPED"}]},
        }],
    }


@pytest.fixture
def session():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    with Session(eng) as s:
        yield s


class FakePayPal:
    def __init__(self, order: dict):
        self.order = order
        self.calls: list[str] = []

    async def get(self, path: str, **_kw):
        self.calls.append(path)
        if path.startswith("/v2/payments/captures/"):
            return CAPTURE
        if path.startswith("/v2/checkout/orders/"):
            return self.order
        raise AssertionError(path)


def _dispute(session) -> Dispute:
    d = Dispute(paypal_dispute_id="PP-R-TEST-1", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", stage="CHARGEBACK",
                status="UNDER_REVIEW", amount="12.00", currency="USD", transaction_id="CAP123",
                raw={"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"})
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


@pytest.mark.asyncio
async def test_rebuilds_missing_order_and_tracking(session):
    d = _dispute(session)
    inv = Investigator(session, d, FakePayPal(_order()), fresh=False)
    order = await inv._recover_order(CAPTURE)
    assert order is not None and d.order_id == order.id
    assert order.capture_id == "CAP123" and order.invoice_id == "DS-44-cb1ffa73"
    assert order.items[0]["sku"] == "CASE-01" and str(order.amount) == "12.00"
    assert order.shipping_address["postal_code"] == "78701" and order.buyer_email.endswith("example.com")
    ships = session.exec(select(Shipment).where(Shipment.order_id == order.id)).all()
    assert [(s.tracking_number, s.carrier) for s in ships] == [("9400111899223397846521", "USPS")]
    # idempotent: a second run reuses the row
    again = await inv._recover_order(CAPTURE)
    assert again.id == order.id
    assert len(session.exec(select(Order)).all()) == 1


@pytest.mark.asyncio
async def test_never_imports_orders_from_another_store(session):
    d = _dispute(session)
    inv = Investigator(session, d, FakePayPal(_order(invoice="OTHER-SHOP-1")), fresh=False)
    assert await inv._recover_order(CAPTURE) is None
    assert d.order_id is None
    assert session.exec(select(Order)).all() == []

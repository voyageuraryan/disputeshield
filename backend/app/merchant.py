"""Merchant settings stored in the database (defaults come from config)."""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import Session

from app.config import get_settings
from app.models import MerchantSettings, utcnow


def get_merchant_settings(session: Session) -> MerchantSettings:
    row = session.get(MerchantSettings, 1)
    if row is None:
        row = MerchantSettings(id=1, settle_threshold=get_settings().settle_threshold)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def settle_threshold(session: Session) -> Decimal:
    return Decimal(get_merchant_settings(session).settle_threshold)


def update_settle_threshold(session: Session, value: Decimal) -> MerchantSettings:
    row = get_merchant_settings(session)
    row.settle_threshold = value
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row

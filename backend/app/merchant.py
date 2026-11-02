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


def update_settings(session: Session, *, settle_threshold: Decimal | None = None,
                    refund_policy: str | None = None, clear_refund_policy: bool = False,
                    auto_invite_troubleshooting: bool | None = None) -> MerchantSettings:
    row = get_merchant_settings(session)
    if settle_threshold is not None:
        row.settle_threshold = settle_threshold
    if clear_refund_policy:
        row.refund_policy = None
    elif refund_policy is not None:
        row.refund_policy = refund_policy.strip() or None
    if auto_invite_troubleshooting is not None:
        row.auto_invite_troubleshooting = auto_invite_troubleshooting
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row

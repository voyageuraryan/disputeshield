"""Merchant settings stored in the database (defaults come from config)."""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import Session

from app.config import get_settings
from app.models import MerchantSettings, utcnow
from app.rules.money import MoneySettings


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


def money_settings(session: Session) -> MoneySettings:
    """Calculator inputs from the merchant's settings (defaults: app.rules.money.MoneySettings)."""
    row, d = get_merchant_settings(session), MoneySettings()
    return MoneySettings(
        dispute_fee=Decimal(row.dispute_fee) if row.dispute_fee is not None else d.dispute_fee,
        handling_minutes=Decimal(row.handling_minutes) if row.handling_minutes is not None else d.handling_minutes,
        hourly_value=Decimal(row.hourly_value) if row.hourly_value is not None else d.hourly_value,
        max_offer_pct=Decimal(row.max_offer_pct) if row.max_offer_pct is not None else d.max_offer_pct)


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
                    auto_invite_troubleshooting: bool | None = None, money: dict | None = None) -> MerchantSettings:
    row = get_merchant_settings(session)
    for k, v in (money or {}).items():  # Phase 13: dispute_fee, handling_minutes, hourly_value, max_offer_pct
        if v is not None:
            setattr(row, k, v)
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

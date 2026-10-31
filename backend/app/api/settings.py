"""Merchant settings API (the settings panel)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.db import get_session
from app.merchant import get_merchant_settings, update_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    settle_threshold: Decimal
    refund_policy: str | None
    currency: str = "USD"
    updated_at: datetime


class SettingsIn(BaseModel):
    # Disputes under this amount with a weak case are recommended for SETTLE (the merchant still approves).
    settle_threshold: Decimal | None = Field(default=None, ge=0, le=10000, max_digits=7, decimal_places=2)
    # Store refund policy text ("" clears it). Used as refund_policy evidence for new investigations.
    refund_policy: str | None = Field(default=None, max_length=4000)


@router.get("", response_model=SettingsOut)
def read_settings(session: Session = Depends(get_session)) -> SettingsOut:
    s = get_merchant_settings(session)
    return SettingsOut(settle_threshold=s.settle_threshold, refund_policy=s.refund_policy, updated_at=s.updated_at)


@router.put("", response_model=SettingsOut)
def write_settings(body: SettingsIn, session: Session = Depends(get_session)) -> SettingsOut:
    s = update_settings(session, settle_threshold=body.settle_threshold, refund_policy=body.refund_policy,
                        clear_refund_policy=body.refund_policy == "")
    return SettingsOut(settle_threshold=s.settle_threshold, refund_policy=s.refund_policy, updated_at=s.updated_at)

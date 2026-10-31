"""Merchant settings API (the settings panel)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.db import get_session
from app.merchant import get_merchant_settings, update_settle_threshold

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    settle_threshold: Decimal
    currency: str = "USD"
    updated_at: datetime


class SettingsIn(BaseModel):
    # Disputes under this amount with a weak case are recommended for SETTLE (the merchant still approves).
    settle_threshold: Decimal = Field(ge=0, le=10000, max_digits=7, decimal_places=2)


@router.get("", response_model=SettingsOut)
def read_settings(session: Session = Depends(get_session)) -> SettingsOut:
    s = get_merchant_settings(session)
    return SettingsOut(settle_threshold=s.settle_threshold, updated_at=s.updated_at)


@router.put("", response_model=SettingsOut)
def write_settings(body: SettingsIn, session: Session = Depends(get_session)) -> SettingsOut:
    s = update_settle_threshold(session, body.settle_threshold)
    return SettingsOut(settle_threshold=s.settle_threshold, updated_at=s.updated_at)

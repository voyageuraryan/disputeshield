"""Merchant settings API (the settings panel)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.db import get_session
from app.alerts import recent, toggles
from app.mcp_server import issue_token, resource_url, revoke_token
from app.merchant import get_merchant_settings, money_settings, update_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    settle_threshold: Decimal
    refund_policy: str | None
    auto_invite_troubleshooting: bool = False
    currency: str = "USD"
    updated_at: datetime
    # Phase 13: money calculator inputs (effective values: the merchant's, else the defaults)
    dispute_fee: Decimal = Decimal("15")
    handling_minutes: int = 30
    hourly_value: Decimal = Decimal("25")
    max_offer_pct: int = 70
    fee_sources: list[dict] = []
    # Phase 17
    alerts: dict = {}
    mcp_token_created_at: datetime | None = None
    mcp_url: str = ""


# Shown as help text in Settings. Checked 2026-11-03 (both pages: "We may charge a Dispute Fee when a buyer files a
# dispute claim or chargeback"; inquiries aren't mentioned; whether the fee is returned when you win isn't stated).
FEE_SOURCES = [
    {"country": "US", "standard": "15.00 USD", "high_volume": "30.00 USD",
     "url": "https://www.paypal.com/us/business/paypal-business-fees"},
    {"country": "IN", "standard": "580.00 INR or 8.00 USD", "high_volume": "1,160.00 INR or 16.00 USD",
     "url": "https://www.paypal.com/in/webapps/mpp/merchant-fees"},
]


class SettingsIn(BaseModel):
    # Disputes under this amount with a weak case are recommended for SETTLE (the merchant still approves).
    settle_threshold: Decimal | None = Field(default=None, ge=0, le=10000, max_digits=7, decimal_places=2)
    # Store refund policy text ("" clears it). Used as refund_policy evidence for new investigations.
    refund_policy: str | None = Field(default=None, max_length=4000)
    # Invite eligible buyers (faulty electronics) to guided troubleshooting automatically after investigation.
    auto_invite_troubleshooting: bool | None = None
    # Phase 13: money calculator
    dispute_fee: Decimal | None = Field(default=None, ge=0, le=100000, max_digits=8, decimal_places=2)
    handling_minutes: int | None = Field(default=None, ge=0, le=6000)
    hourly_value: Decimal | None = Field(default=None, ge=0, le=100000, max_digits=8, decimal_places=2)
    max_offer_pct: int | None = Field(default=None, ge=10, le=90)
    # Phase 17: alerts ("" clears the address)
    alert_email_to: str | None = Field(default=None, max_length=254, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    alert_slack: bool | None = None
    alert_on_proposed: bool | None = None
    alert_on_troubleshooting: bool | None = None
    alert_on_deadlines: bool | None = None


ALERT_FIELDS = ("alert_slack", "alert_on_proposed", "alert_on_troubleshooting", "alert_on_deadlines")
MONEY_FIELDS = ("dispute_fee", "handling_minutes", "hourly_value", "max_offer_pct")


def _out(s, session: Session) -> SettingsOut:
    m = money_settings(session)
    return SettingsOut(settle_threshold=s.settle_threshold, refund_policy=s.refund_policy,
                       auto_invite_troubleshooting=bool(s.auto_invite_troubleshooting), updated_at=s.updated_at,
                       dispute_fee=m.dispute_fee, handling_minutes=int(m.handling_minutes),
                       hourly_value=m.hourly_value, max_offer_pct=int(m.max_offer_pct), fee_sources=FEE_SOURCES,
                       alerts=toggles(session), mcp_token_created_at=s.mcp_token_created_at, mcp_url=resource_url())


@router.get("", response_model=SettingsOut)
def read_settings(session: Session = Depends(get_session)) -> SettingsOut:
    return _out(get_merchant_settings(session), session)


@router.put("", response_model=SettingsOut)
def write_settings(body: SettingsIn, session: Session = Depends(get_session)) -> SettingsOut:
    s = update_settings(session, settle_threshold=body.settle_threshold, refund_policy=body.refund_policy,
                        clear_refund_policy=body.refund_policy == "",
                        auto_invite_troubleshooting=body.auto_invite_troubleshooting,
                        money={**{k: getattr(body, k) for k in MONEY_FIELDS + ALERT_FIELDS},
                               **({"alert_email_to": body.alert_email_to or None}
                                  if body.alert_email_to is not None else {})})
    return _out(s, session)


class TokenOut(BaseModel):
    token: str
    mcp_url: str
    note: str = "Shown once. Store it in your MCP client; create a new one to replace it."


@router.post("/mcp-token", response_model=TokenOut, status_code=201)
def create_mcp_token(session: Session = Depends(get_session)) -> TokenOut:
    """Phase 17: a bearer token for the MCP server (replaces the previous one). Only its hash is stored."""
    return TokenOut(token=issue_token(session), mcp_url=resource_url())


@router.delete("/mcp-token", status_code=204)
def delete_mcp_token(session: Session = Depends(get_session)) -> None:
    revoke_token(session)


@router.get("/alerts")
def recent_alerts(session: Session = Depends(get_session)) -> list[dict]:
    return recent(session)

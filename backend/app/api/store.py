"""Demo storefront API: catalog, PayPal checkout (create/capture), shipment tracking."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.catalog import PRODUCTS, Product
from app.config import get_settings
from app.db import get_session
from app.models import Order, Shipment
from app.paypal.client import PayPalClient, PayPalError, get_paypal_client
from app.paypal.orders import LineItem, capture_order, create_order, parse_capture
from app.paypal.tracking import add_tracking

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/store", tags=["store"])


def _require_paypal_configured() -> None:
    s = get_settings()
    if not s.paypal_client_id or not s.paypal_client_secret.get_secret_value():
        raise HTTPException(503, "PayPal sandbox credentials are not configured on the server (backend/.env)")


def _paypal_http_error(exc: PayPalError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"error": "paypal_error", "paypal_status": exc.status_code, "debug_id": exc.debug_id,
                "paypal": exc.body if isinstance(exc.body, dict) else None},
    )


class StoreConfig(BaseModel):
    client_id: str
    currency: str
    sdk_url: str


@router.get("/config", response_model=StoreConfig)
def store_config() -> StoreConfig:
    # The client ID is public (PayPal: "safe to include in front-end code"); the secret never leaves the server.
    s = get_settings()
    return StoreConfig(client_id=s.paypal_client_id, currency="USD",
                       sdk_url="https://www.sandbox.paypal.com/web-sdk/v6/core")


@router.get("/products", response_model=list[Product])
def list_products() -> list[Product]:
    return list(PRODUCTS.values())


class CreateOrderIn(BaseModel):
    sku: str
    quantity: int = Field(default=1, ge=1, le=10)


class CreateOrderOut(BaseModel):
    id: str  # PayPal order id (the JS SDK wants this)
    order_id: int


@router.post("/orders", response_model=CreateOrderOut)
async def create_store_order(
    body: CreateOrderIn,
    session: Session = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> CreateOrderOut:
    product = PRODUCTS.get(body.sku)
    if product is None:
        raise HTTPException(404, f"unknown sku {body.sku}")
    _require_paypal_configured()
    order = Order(
        items=[{"sku": product.sku, "name": product.name, "quantity": body.quantity,
                "unit_amount": str(product.price), "category": product.category}],
        amount=product.price * body.quantity,
        currency=product.currency,
        product_listing=product.listing,
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    # invoice_id must be unique per PayPal account; the nonce keeps it unique across DB resets.
    nonce = uuid.uuid4().hex[:8]
    order.invoice_id = f"DS-{order.id}-{nonce}"
    try:
        pp_order = await create_order(
            internal_order_id=order.id,
            invoice_id=order.invoice_id,
            currency=product.currency,
            items=[LineItem(product.name, product.sku, product.price, body.quantity, product.category)],
            request_id=f"ds-create-{order.id}-{nonce}",
            client=paypal,
        )
    except Exception as exc:
        # No PayPal order exists for this row, so drop it rather than leave an orphan.
        session.delete(order)
        session.commit()
        if isinstance(exc, PayPalError):
            raise _paypal_http_error(exc) from exc
        raise
    order.paypal_order_id = pp_order["id"]
    session.add(order)
    session.commit()
    return CreateOrderOut(id=pp_order["id"], order_id=order.id)


class CaptureOut(BaseModel):
    order_id: int
    paypal_order_id: str
    capture_id: str
    status: str


@router.post("/orders/{paypal_order_id}/capture", response_model=CaptureOut)
async def capture_store_order(
    paypal_order_id: str,
    session: Session = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> CaptureOut:
    order = session.exec(select(Order).where(Order.paypal_order_id == paypal_order_id)).first()
    if order is None:
        raise HTTPException(404, "unknown order")
    _require_paypal_configured()
    if order.capture_id:  # already captured: idempotent
        return CaptureOut(order_id=order.id, paypal_order_id=paypal_order_id,
                          capture_id=order.capture_id, status="COMPLETED")
    try:
        # Deterministic key: a repeated capture of the same order is deduplicated by PayPal.
        raw = await capture_order(paypal_order_id, request_id=f"ds-capture-{paypal_order_id}", client=paypal)
    except PayPalError as exc:
        raise _paypal_http_error(exc) from exc

    result = parse_capture(raw)
    if result.custom_id != str(order.id):
        log.error("capture custom_id %s does not match order %s", result.custom_id, order.id)
        raise HTTPException(409, "captured order does not match our record")
    if not result.capture_id or result.capture_status not in {"COMPLETED", "PENDING"}:
        raise HTTPException(402, f"capture not completed (status={result.capture_status})")

    order.capture_id = result.capture_id
    order.buyer_email = result.payer_email
    order.shipping_address = result.shipping_address
    session.add(order)
    session.commit()
    return CaptureOut(order_id=order.id, paypal_order_id=paypal_order_id,
                      capture_id=result.capture_id, status=result.capture_status)


class ShipmentIn(BaseModel):
    carrier: str = Field(min_length=1, max_length=64)
    tracking_number: str = Field(min_length=1, max_length=64)
    notify_payer: bool = False


class ShipmentOut(BaseModel):
    id: int
    order_id: int
    carrier: str | None
    tracking_number: str | None
    status: str | None
    delivered_at: datetime | None
    paypal_tracker_added: bool


@router.post("/orders/{order_id}/shipments", response_model=ShipmentOut)
async def add_shipment(
    order_id: int,
    body: ShipmentIn,
    session: Session = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> ShipmentOut:
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "unknown order")
    if not (order.paypal_order_id and order.capture_id):
        raise HTTPException(409, "order is not captured in PayPal; tracking can only be added to captured orders")
    try:
        await add_tracking(order.paypal_order_id, capture_id=order.capture_id,
                           tracking_number=body.tracking_number, carrier=body.carrier,
                           notify_payer=body.notify_payer, client=paypal)
    except PayPalError as exc:
        raise _paypal_http_error(exc) from exc
    shipment = Shipment(order_id=order.id, carrier=body.carrier, tracking_number=body.tracking_number,
                        status="SHIPPED")
    session.add(shipment)
    session.commit()
    session.refresh(shipment)
    return ShipmentOut(**shipment.model_dump(include={"id", "order_id", "carrier", "tracking_number",
                                                      "status", "delivered_at"}),
                       paypal_tracker_added=True)


class OrderOut(BaseModel):
    id: int
    paypal_order_id: str | None
    capture_id: str | None
    buyer_email: str | None
    amount: Decimal
    currency: str
    items: list[dict] | None
    created_at: datetime


@router.get("/orders", response_model=list[OrderOut])
def list_orders(session: Session = Depends(get_session)) -> list[Order]:
    return list(session.exec(select(Order).order_by(Order.id.desc())).all())

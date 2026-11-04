"""Demo storefront API: catalog, PayPal checkout (create/capture), shipment tracking.

Catalog and checkout are public (buyers never log in); listing orders and adding tracking are merchant-only."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timezone

from app.signals import capture_checkout, client_ip, signal_hash
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from app import ratelimit
from app.auth import require_merchant
from app.catalog import PRODUCTS, Product, digital_only, order_item
from app.config import get_settings
from app.db import get_session
from app.api.disputes import OrderOut, order_out
from app.models import AgentStep, Dispute, Order, Shipment
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
    # Phase 15: SHA-256 hex the browser computed from user agent + screen + timezone (never the raw values)
    device_fp: str | None = Field(default=None, max_length=64)


class CreateOrderOut(BaseModel):
    id: str  # PayPal order id (the JS SDK wants this)
    order_id: int


async def open_paypal_order(session: Session, product, quantity: int, *, paypal: PayPalClient,  # noqa: ANN001
                            ip: str | None = None, device_fp: str | None = None,
                            demo_scenario: str | None = None) -> Order:
    """Create the store order row + the real PayPal (sandbox) order. Shared by the Store and demo scenarios."""
    _require_paypal_configured()
    order = Order(
        items=[order_item(product, quantity)],
        amount=product.price * quantity,
        currency=product.currency,
        product_listing=product.listing,
        demo_scenario=demo_scenario,
    )
    # Phase 15: only keyed hashes of the IP and the device fingerprint are kept (app/signals.py)
    capture_checkout(order, ip=ip, device_fp=device_fp)
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
            items=[LineItem(product.name, product.sku, product.price, quantity, product.paypal_category)],
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
    session.refresh(order)
    return order


@router.post("/orders", response_model=CreateOrderOut, dependencies=[Depends(ratelimit.limit("store_order"))])
async def create_store_order(
    body: CreateOrderIn,
    request: Request,
    session: Session = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> CreateOrderOut:
    product = PRODUCTS.get(body.sku)
    if product is None:
        raise HTTPException(404, f"unknown sku {body.sku}")
    order = await open_paypal_order(session, product, body.quantity, paypal=paypal, device_fp=body.device_fp,
                                    ip=client_ip(request.headers, request.client.host if request.client else None))
    return CreateOrderOut(id=order.paypal_order_id, order_id=order.id)


class CaptureOut(BaseModel):
    order_id: int
    paypal_order_id: str
    capture_id: str
    status: str


class CaptureIn(BaseModel):
    device_fp: str | None = Field(default=None, max_length=64)  # Phase 16: the paying browser's hashed fingerprint


@router.post("/orders/{paypal_order_id}/capture", response_model=CaptureOut)
async def capture_store_order(
    paypal_order_id: str,
    request: Request,
    body: CaptureIn | None = None,
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
    order.payer_hash = signal_hash("payer", result.payer_id)  # Phase 15: PayPal payer.payer_id, hashed
    if not (order.ip_hash or order.device_hash):  # e.g. a demo scenario order created by the merchant's click:
        capture_checkout(order, device_fp=body.device_fp if body else None,  # record the paying browser instead
                         ip=client_ip(request.headers, request.client.host if request.client else None))
    session.add(order)
    session.commit()
    if order.demo_scenario:  # Phase 16: a demo scenario's post-payment store data (delivery, history)
        from app.demo_scenarios import after_capture

        after_capture(session, order)
    return CaptureOut(order_id=order.id, paypal_order_id=paypal_order_id,
                      capture_id=result.capture_id, status=result.capture_status)


class ShipmentIn(BaseModel):
    carrier: str = Field(min_length=1, max_length=64)
    tracking_number: str = Field(min_length=1, max_length=64)
    notify_payer: bool = False

    @field_validator("carrier", "tracking_number")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class ShipmentOut(BaseModel):
    id: int
    order_id: int
    carrier: str | None
    tracking_number: str | None
    status: str | None
    delivered_at: datetime | None
    paypal_tracker_added: bool


def _note_on_disputes(session: Session, order: Order, action: str, detail: dict) -> None:
    """Show tracking/delivery changes on the timeline of every dispute about this order."""
    for d in session.exec(select(Dispute).where(Dispute.order_id == order.id)).all():
        session.add(AgentStep(dispute_id=d.id, agent="merchant", action=action, detail=detail))
    session.commit()


@router.post("/orders/{order_id}/shipments", response_model=ShipmentOut,
             dependencies=[Depends(require_merchant)])  # merchant-only: writes to PayPal
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
    if digital_only(order.items):
        raise HTTPException(409, "this order only has digital items; there is nothing to ship")
    dup = session.exec(select(Shipment).where(Shipment.order_id == order.id,
                                              Shipment.tracking_number == body.tracking_number)).first()
    if dup:
        raise HTTPException(409, f"tracking {body.tracking_number} is already on order #{order.id}")
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
    out = ShipmentOut(**shipment.model_dump(include={"id", "order_id", "carrier", "tracking_number",
                                                     "status", "delivered_at"}),
                      paypal_tracker_added=True)
    _note_on_disputes(session, order, f"Added tracking {body.tracking_number} ({body.carrier}) to order #{order.id} "
                      "and uploaded it to PayPal; re-analyse to include it",
                      {"shipment_id": shipment.id})
    return out


class DeliveryIn(BaseModel):
    """Delivery as confirmed by the carrier (the merchant copies it from the carrier's tracking page).
    address None = delivered to the order's shipping address."""
    delivered_on: date
    address: dict[str, str] | None = None

    @field_validator("address")
    @classmethod
    def _address(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        v = {k: (val or "").strip() for k, val in v.items() if k in ADDRESS_KEYS}
        missing = [k for k in ("address_line_1", "postal_code", "country_code") if not v.get(k)]
        if missing:
            raise ValueError(f"address needs {', '.join(missing)}")
        v["country_code"] = v["country_code"].upper()
        if len(v["country_code"]) != 2:
            raise ValueError("country_code must be 2 letters, e.g. US")
        return v


ADDRESS_KEYS = ("address_line_1", "address_line_2", "admin_area_2", "admin_area_1", "postal_code", "country_code")


class DeliveryOut(BaseModel):
    id: int
    order_id: int
    status: str | None
    delivered_at: datetime | None
    delivered_address: dict | None
    delivery_recorded_by: str | None


@router.post("/orders/{order_id}/shipments/{shipment_id}/delivery", response_model=DeliveryOut,
             dependencies=[Depends(require_merchant)])
def record_delivery(order_id: int, shipment_id: int, body: DeliveryIn,
                    session: Session = Depends(get_session)) -> DeliveryOut:
    """Record the carrier's delivery confirmation (date + address). Stored in our DB only: PayPal has no
    endpoint for a seller to mark a parcel delivered, and the sandbox carriers never report delivery."""
    order = session.get(Order, order_id)
    shipment = session.get(Shipment, shipment_id)
    if order is None or shipment is None or shipment.order_id != order.id:
        raise HTTPException(404, "unknown order or shipment")
    if not shipment.tracking_number:
        raise HTTPException(409, "add a tracking number before recording delivery")
    today = datetime.now(timezone.utc).date()
    ordered = order.created_at.date()
    if body.delivered_on > today:
        raise HTTPException(422, "delivery date can't be in the future")
    if body.delivered_on < ordered:
        raise HTTPException(422, f"delivery date can't be before the order date ({ordered})")
    address = body.address or order.shipping_address
    if not address:
        raise HTTPException(422, "the order has no shipping address; enter the delivery address")
    shipment.delivered_at = datetime.combine(body.delivered_on, time(12, 0), tzinfo=timezone.utc)
    shipment.delivered_address = address
    shipment.status = "DELIVERED"
    shipment.delivery_recorded_by = "merchant"
    session.add(shipment)
    session.commit()
    session.refresh(shipment)
    out = DeliveryOut(**shipment.model_dump(include=set(DeliveryOut.model_fields)))
    _note_on_disputes(session, order, f"Recorded delivery of {shipment.tracking_number} on {body.delivered_on} "
                      + ("to the order's shipping address" if body.address is None else "to a different address")
                      + "; re-analyse to include it", {"shipment_id": shipment.id})
    return out


class StoreOrderOut(OrderOut):
    disputes: list[str]  # PayPal dispute ids about this order


@router.get("/orders", response_model=list[StoreOrderOut],
            dependencies=[Depends(require_merchant)])  # merchant-only: lists buyer emails
def list_orders(session: Session = Depends(get_session)) -> list[StoreOrderOut]:
    # Hide abandoned checkouts (a PayPal order that was never captured); seeded/demo orders have no PayPal ids.
    abandoned = Order.paypal_order_id.is_not(None) & Order.capture_id.is_(None)
    orders = session.exec(select(Order).where(~abandoned).order_by(Order.id.desc())).all()
    out = []
    for o in orders:
        ids = session.exec(select(Dispute.paypal_dispute_id).where(Dispute.order_id == o.id)).all()
        out.append(StoreOrderOut(**order_out(session, o).model_dump(), disputes=list(ids)))
    return out

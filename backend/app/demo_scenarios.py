"""Demo mode (Phase 16): one-click scenarios for judges. ONLY when DEMO_MODE=true AND ADMIN_TOKEN is set (default off).

Each scenario creates a REAL sandbox PayPal order through the normal checkout code (store.open_paypal_order) plus the
store data around it (shipment, buyer message, purchase history). What can't be automated stays a hand step, listed
in a copyable checklist: the buyer pays in the PayPal sandbox (the Store page link pays this exact order) and opens
the dispute from the buyer's PayPal account. PayPal has no API for a buyer to open a dispute without special
partner scopes, so DisputeShield never fakes a dispute: the case arrives by the normal webhook / poller.

After the payment is captured, after_capture() adds the post-payment data: delivery to the address PayPal returned,
and (unauthorised scenario) two earlier paid orders by the same buyer, labelled DEMO-HIST, with the same hashed device /
IP / account so the Phase 15 CE 3.0-like signal can be shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from app.config import get_settings
from app.models import Message, MessageSender, Order, Shipment

SANDBOX_SITE = "https://www.sandbox.paypal.com"
SANDBOX_ACCOUNTS = "https://developer.paypal.com/dashboard/accounts"


def enabled() -> bool:
    s = get_settings()
    return bool(s.demo_mode and s.admin_token.get_secret_value())


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    sku: str
    expect: str
    why: str
    report: str                      # what the buyer picks in "Report a problem"
    message: str | None = None       # a buyer message in the store (before the dispute)
    shipment: bool = True
    history: bool = False
    notes: list[str] = field(default_factory=list)


SCENARIOS: dict[str, Scenario] = {s.key: s for s in [
    Scenario("inr_strong", "Strong 'item not received'", "LAMP-01", "FIGHT",
             "Tracked and delivered to the buyer's own address, and the buyer wrote 'got it, thanks'.",
             "I didn't receive it (item not received)", message="Got it, thanks!"),
    Scenario("snad_small", "$12 'not as described', weak", "CASE-01", "SETTLE",
             "Under the $25 settle threshold, no tracking and no refund policy: fighting costs more than it's worth.",
             "Item is significantly not as described", message="This isn't the case shown in the listing.",
             shipment=False, notes=["If you set a refund policy in Settings, the case gets a little stronger."]),
    Scenario("earbuds_ts", "Faulty earbuds 'not as described'", "EARBUDS-01", "Troubleshooting invite (then decide)",
             "An electronics item that 'doesn't work' is eligible for the buyer troubleshooting page.",
             "Item is significantly not as described: it doesn't work",
             message="One earbud won't charge.",
             notes=["While the case is an inquiry, use 'Invite via PayPal message' on the case page."]),
    Scenario("unauth_history", "Unauthorised, with earlier undisputed orders", "LAMP-01", "FIGHT",
             "Delivered to the buyer's address, and two earlier undisputed orders from the same device and account "
             "(a Visa CE 3.0-like pattern: similar evidence, not compliance).",
             "I didn't authorise this payment (unauthorised activity)", history=True,
             notes=["The two earlier orders are demo store data (invoice DEMO-HIST-…), dated 150 and 300 days ago.",
                    "If the sandbox doesn't offer 'unauthorised' for this payment, use 'item not received': the "
                    "earlier-orders evidence still shows on the case."]),
]}


def checklist(sc: Scenario, *, store_url: str) -> list[dict]:
    buyer = get_settings().demo_buyer_email or "your US sandbox personal (buyer) account"
    return [
        {"step": f"Open the Store link in a private window and pay with the sandbox buyer: {buyer}.",
         "link": store_url},
        {"step": "Wait for 'Paid'. DisputeShield then adds this scenario's store data by itself"
                 + (" (delivery to the buyer's address" + (", earlier orders" if sc.history else "") + ")."
                    if sc.shipment else ".")},
        {"step": f"Go to {SANDBOX_SITE}, log in as the same buyer, then Activity → this payment → Report a problem "
                 "(close the cookie banner first).", "link": SANDBOX_SITE},
        {"step": f"Choose: {sc.report}. Submit the report."},
        {"step": f"Within seconds (at most ~3 min through the poller) the case appears under Disputes. Expected: "
                 f"{sc.expect}. Why: {sc.why}"},
        *({"step": n} for n in sc.notes),
        {"step": "Need the buyer's login? Sandbox accounts are under Testing Tools → Sandbox Accounts.",
         "link": SANDBOX_ACCOUNTS},
    ]


def describe(sc: Scenario) -> dict:
    return {"key": sc.key, "title": sc.title, "sku": sc.sku, "expect": sc.expect, "why": sc.why}


def seed_before_payment(session: Session, sc: Scenario, order: Order) -> None:
    now = datetime.now(timezone.utc)
    if sc.shipment:
        session.add(Shipment(order_id=order.id, carrier="USPS", tracking_number=f"9400DEMO{order.id:08d}",
                             status="SHIPPED"))
    if sc.message:
        session.add(Message(order_id=order.id, sender=MessageSender.buyer, body=sc.message, sent_at=now))
    session.commit()


def after_capture(session: Session, order: Order) -> None:
    """Post-payment store data for a scenario order (called by the capture endpoint). Idempotent."""
    sc = SCENARIOS.get(order.demo_scenario or "")
    if sc is None:
        return
    from sqlmodel import select

    now = datetime.now(timezone.utc)
    for sh in session.exec(select(Shipment).where(Shipment.order_id == order.id)).all():
        if sh.status != "DELIVERED":
            sh.status, sh.delivered_at, sh.delivered_address = "DELIVERED", now, order.shipping_address
            session.add(sh)
    if sc.history and not session.exec(select(Order).where(Order.invoice_id.like(f"DEMO-HIST-{order.id}-%"))).first():
        for days in (150, 300):
            session.add(Order(buyer_email=order.buyer_email, items=order.items, amount=order.amount,
                              currency=order.currency, shipping_address=order.shipping_address,
                              product_listing=order.product_listing, invoice_id=f"DEMO-HIST-{order.id}-{days}",
                              capture_id=f"DEMOHIST{order.id}{days}", ip_hash=order.ip_hash,
                              device_hash=order.device_hash, payer_hash=order.payer_hash,
                              created_at=now - timedelta(days=days)))
    session.commit()

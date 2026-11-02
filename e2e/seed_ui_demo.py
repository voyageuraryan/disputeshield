"""Seed a scratch DB for the UI tests (run with the backend's Python, DATABASE_URL pointing at the scratch file)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session, select

from app.db import create_db_and_tables, engine
from app.models import AgentStep, Decision, DecisionStatus, Dispute, EvidenceItemRow, Order
from app.scripts.seed import seed

INR, SNAD = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
now = datetime.now(timezone.utc)
links = lambda *r: [{"rel": x, "href": "x", "method": "POST"} for x in ("self", *r)]  # noqa: E731
SPECS = [  # pid, buyer, reason, amount, due_h, status, rec, conf, decision status, evidence
    ("PP-DEMO-A", "case-a@demo.test", INR, "89.00", 20, "WAITING_FOR_SELLER_RESPONSE", "FIGHT", "high", "PROPOSED",
     [("tracking", "Shipment 9400111899223344556677 (USPS), status DELIVERED, delivered 15 Oct", ["delivered"]),
      ("delivery_confirmation", "Carrier delivered 15 Oct; matches the order's shipping address", ["address_match"]),
      ("buyer_message", 'Buyer wrote on 16 Oct: "got it, thanks"', []), ("order", "Store order #1: 1x lamp", [])]),
    ("PP-DEMO-B", "case-b@demo.test", SNAD, "12.00", 100, "WAITING_FOR_SELLER_RESPONSE", "SETTLE", "medium", "PROPOSED",
     [("listing", "Listing at time of sale: phone case", []), ("buyer_message", "Buyer: not as listed", [])]),
    ("PP-DEMO-C", "case-c@demo.test", INR, "45.00", 30, "WAITING_FOR_SELLER_RESPONSE", "GATHER_MORE", "low", "PROPOSED",
     [("tracking", "Shipment 1Z999AA10123456784 (UPS), delivered 20 Oct", ["delivered"]),
      ("delivery_confirmation", "Delivered to 1540 Industrial Pkwy; does NOT match the shipping address", ["address_mismatch"])]),
    ("PP-DEMO-R", "maria@demo.test", INR, "89.00", None, "RESOLVED", "FIGHT", "high", "EXECUTED", []),
]

create_db_and_tables()
with Session(engine) as s:
    seed(s)
    for pid, email, reason, amt, due, status, rec, conf, dst, evs in SPECS:
        o = s.exec(select(Order).where(Order.buyer_email == email)).first()
        d = Dispute(paypal_dispute_id=pid, order_id=o.id, reason=reason, status=status, stage="CHARGEBACK",
                    amount=Decimal(amt), currency="USD", seller_response_due=(now + timedelta(hours=due)) if due else None,
                    raw={"update_time": now.isoformat(), **({"links": links("provide_evidence", "accept_claim")} if due else {})},
                    agent_status="RESOLVED" if status == "RESOLVED" else "PROPOSED")
        s.add(d); s.commit(); s.refresh(d)
        ids = []
        for t, summ, fl in evs:
            e = EvidenceItemRow(dispute_id=d.id, type=t, source="store", source_ref=f"store:order/{o.id}", summary=summ, flags=fl)
            s.add(e); s.commit(); s.refresh(e); ids.append(e.id)
        s.add(Decision(dispute_id=d.id, recommendation=rec, confidence=conf, status=DecisionStatus(dst),
                       reasoning=f"Demo reasoning for {pid}.", cited_evidence_ids=ids[:2], economic_note="Demo economic note.",
                       missing_items=[] if rec != "GATHER_MORE" else ["proof of delivery to the buyer's address"],
                       draft_note="Demo seller note." if rec == "FIGHT" else None, analysed_threshold=Decimal("25")))
        s.add(AgentStep(dispute_id=d.id, agent="analyst", action="demo step"))
        s.commit()
    o = s.exec(select(Order).where(Order.buyer_email == "devon@demo.test")).first()
    s.add(Dispute(paypal_dispute_id="PP-DEMO-E", order_id=o.id, reason=INR, status="OPEN", amount=Decimal("45"),
                  currency="USD", raw={"update_time": now.isoformat()}))
    # Phase 9A: faulty earbuds, an inquiry with no send_message link (so the invite goes by the email stub;
    # nothing is ever sent to PayPal from the UI suite).
    o = s.exec(select(Order).where(Order.buyer_email == "case-d@demo.test")).first()
    s.add(Dispute(paypal_dispute_id="PP-DEMO-D", order_id=o.id, reason=SNAD, status="WAITING_FOR_SELLER_RESPONSE",
                  stage="INQUIRY", amount=Decimal("59.00"), currency="USD", seller_response_due=now + timedelta(hours=120),
                  raw={"update_time": now.isoformat(), "links": links("accept_claim", "escalate")},
                  agent_status="PROPOSED"))
    # Phase 9B: two more faulty-lamp inquiries so the chat tests don't depend on each other.
    for pid, email in (("PP-DEMO-F", "rosa@demo.test"), ("PP-DEMO-G", "nina@demo.test")):
        o = s.exec(select(Order).where(Order.buyer_email == email)).first()
        s.add(Dispute(paypal_dispute_id=pid, order_id=o.id, reason=SNAD, status="WAITING_FOR_SELLER_RESPONSE",
                      stage="INQUIRY", amount=Decimal("89.00"), currency="USD",
                      seller_response_due=now + timedelta(hours=120),
                      raw={"update_time": now.isoformat(), "links": links("accept_claim")}, agent_status="PROPOSED"))
    # Phase 9C: more faulty lamps (own orders): H for the full buyer flow to DEFECT_CONFIRMED, I for an
    # expired/tampered link, J for the phone layout.
    from app.catalog import PRODUCTS, order_item
    for pid, email in (("PP-DEMO-H", "hana@demo.test"), ("PP-DEMO-I", "ivan@demo.test"),
                       ("PP-DEMO-J", "jo@demo.test")):
        o = Order(buyer_email=email, invoice_id=f"INV-{pid[-1]}-9C", amount=Decimal("89.00"), currency="USD",
                  items=[order_item(PRODUCTS["LAMP-01"], 1)])
        s.add(o); s.commit(); s.refresh(o)
        s.add(Dispute(paypal_dispute_id=pid, order_id=o.id, reason=SNAD, status="WAITING_FOR_SELLER_RESPONSE",
                      stage="INQUIRY", amount=Decimal("89.00"), currency="USD",
                      seller_response_due=now + timedelta(hours=120),
                      raw={"update_time": now.isoformat(), "links": links("accept_claim")}, agent_status="PROPOSED"))
    s.commit()
print("seeded")

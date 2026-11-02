"""API data behind the phase-7 screens."""

import pytest
from sqlmodel import Session, select

from app.models import EvidenceItemRow
from tests.test_resolution import (  # noqa: F401  (fixtures)
    BASE,
    DISPUTES,
    SELLER_TURN,
    api,
    artifacts,
    demo,
    engine,
    start_case,
)


async def test_queue_row_has_buyer_and_decision(api, engine, demo):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    [row] = api.get("/api/disputes").json()
    assert row["buyer_email"] == "case-a@demo.test"
    assert row["latest_decision"]["recommendation"] == "FIGHT" and row["latest_decision"]["confidence"] == "medium"


async def test_detail_has_order_checklist_and_economic_note(api, engine, demo):
    await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C", amount="45.00")
    d = api.get("/api/disputes/PP-D-C").json()
    o = d["order"]
    assert o["buyer_email"] == "case-c@demo.test" and o["items"][0]["sku"] == "MUG-01"
    assert o["shipments"][0]["carrier"] == "UPS" and o["shipments"][0]["delivered_address"]["postal_code"] == "53204"
    cl = d["checklist"]
    assert cl["reason_label"] == "Item not received" and cl["rule_score"] == 0.53 and cl["baseline"] == "GATHER_MORE"
    req = {i["type"]: i for i in cl["items"] if i["required"]}
    assert set(req) == {"tracking", "delivery_confirmation"} and all(i["present"] for i in req.values())
    assert {i["type"] for i in cl["items"] if not i["present"]} == {"invoice"}
    assert cl["red_flags"] == [{"code": "address_mismatch",
                                "description": "Carrier delivered to a different address than the order's shipping address"}]
    dec = d["decisions"][0]
    assert dec["economic_note"].startswith("Amount 45.00") and dec["missing_items"] == ["evidence that resolves address_mismatch"]


async def test_unsupported_reason_checklist(api, engine, demo):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    from app.models import Dispute
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one(); d.reason = "CREDIT_NOT_PROCESSED"; s.add(d); s.commit()
    cl = api.get("/api/disputes/PP-D-A").json()["checklist"]
    assert cl["supported"] is False and cl["items"] == [] and cl["baseline"] is None


async def test_evidence_sources_store_records(api, engine, demo):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    with Session(engine) as s:
        rows = {r.source_ref: r.id for r in s.exec(select(EvidenceItemRow)).all()}
    seen = set()
    for ref, eid in rows.items():
        r = api.get(f"/api/evidence/{eid}/source")
        assert r.status_code == 200, (ref, r.text)
        body = r.json()
        assert body["source_ref"] == ref and body["origin"] == "Store database" and body["record"]
        seen.add(ref.split(":")[1].split("/")[0])
    assert seen >= {"order", "shipment", "message", "buyer"}
    listing = next(eid for ref, eid in rows.items() if ref.endswith("/listing"))
    assert "Lumina LD-200 smart LED desk lamp" in api.get(f"/api/evidence/{listing}/source").json()["record"]["product_listing"]
    assert api.get("/api/evidence/9999/source").status_code == 404


async def test_evidence_sources_paypal_records_live_and_errors(api, engine, demo):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    with Session(engine) as s:
        cap = EvidenceItemRow(dispute_id=1, type="invoice", source="paypal", source_ref="paypal:capture/CAP1", summary="s")
        trk = EvidenceItemRow(dispute_id=1, type="tracking", source="paypal",
                              source_ref="paypal:order/ORD1/tracker/CAP1-9400", summary="s")
        s.add_all([cap, trk]); s.commit(); s.refresh(cap); s.refresh(trk)
    api.mock.routes.pop("v2_default")  # the fixture's catch-all would match first
    cap_route = api.mock.get(f"{BASE}/v2/payments/captures/CAP1")
    cap_route.respond(200, json={"id": "CAP1", "status": "COMPLETED"})
    api.mock.get(f"{BASE}/v2/checkout/orders/ORD1").respond(200, json={"id": "ORD1", "purchase_units": [
        {"shipping": {"trackers": [{"id": "CAP1-9400", "status": "SHIPPED"}, {"id": "CAP1-OTHER", "status": "SHIPPED"}]}}]})
    c = api.get(f"/api/evidence/{cap.id}/source").json()
    assert c["origin"] == "PayPal (live)" and c["record"]["status"] == "COMPLETED"
    t = api.get(f"/api/evidence/{trk.id}/source").json()
    assert [x["tracking_number"] for x in t["record"]] == ["9400"]
    cap_route.respond(503, json={})
    assert api.get(f"/api/evidence/{cap.id}/source").status_code == 502

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.agents.investigator import investigate
from app.agents.runner import resume_run, run_agents
from app.db import create_db_and_tables, get_engine
from app.main import app
from app.models import AgentStep, Dispute, EvidenceItemRow, Order, Shipment
from app.scripts.seed import seed
from tests.conftest import BASE, token_response

FIX = Path(__file__).parent / "fixtures"
REAL_GET = json.loads((FIX / "real_get_dispute.json").read_text())
REAL_CAPTURE = json.loads((FIX / "real_capture.json").read_text())
REAL_ORDER = json.loads((FIX / "real_order.json").read_text())
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
DISPUTES = f"{BASE}/v1/customer/disputes"


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    return eng


@pytest.fixture
def demo(engine):
    with Session(engine) as s:
        return seed(s, now=NOW)


def make_dispute(engine, order_id, pid="PP-D-DEMO", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                 transaction_id=None, amount="89.00"):
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id=pid, order_id=order_id, reason=reason, status="OPEN", stage="INQUIRY",
                    amount=Decimal(amount), currency="USD", transaction_id=transaction_id,
                    raw={"dispute_id": pid, "reason": reason})
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def dispute_json(pid, reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", amount="89.00", messages=None):
    return {"dispute_id": pid, "reason": reason, "status": "UNDER_REVIEW", "dispute_life_cycle_stage": "INQUIRY",
            "dispute_amount": {"currency_code": "USD", "value": amount},
            "seller_response_due_date": "2026-11-20T10:00:00.000Z", "update_time": "2026-10-31T10:00:00.000Z",
            "messages": messages or []}


async def run_investigation(engine, dispute_row_id, pp):
    with Session(engine) as s:
        case = await investigate(s, s.get(Dispute, dispute_row_id), pp)
        steps = [a.action for a in s.exec(select(AgentStep).where(AgentStep.agent == "investigator")
                                          .order_by(AgentStep.id)).all()]
    return case, steps


def by_type(case):
    out = {}
    for e in case.evidence:
        out.setdefault(e.type, []).append(e)
    return out


# ---- demo cases A / B / C (store-side) --------------------------------------

@respx.mock
async def test_case_a_delivered_to_buyer(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-A").respond(200, json=dispute_json("PP-D-A"))
    did = make_dispute(engine, demo["A"], "PP-D-A")

    case, steps = await run_investigation(engine, did, pp)

    ev = by_type(case)
    assert set(ev) == {"order", "tracking", "delivery_confirmation", "buyer_message", "listing", "buyer_history"}
    assert all(e.source == "store" for e in case.evidence)
    assert "matches the order's shipping address" in ev["delivery_confirmation"][0].summary
    assert any('"got it, thanks"' in e.summary for e in ev["buyer_message"])
    assert case.order_id == demo["A"] and case.amount == Decimal("89.00")
    assert case.due_date is None or case.due_date.year == 2026
    # human-readable timeline, one step per lookup
    assert steps[0].startswith("Fetched dispute PP-D-A: MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    assert "Found tracking 9400111899223344556677 (USPS), delivered 15 Oct" in steps
    assert "Delivery address matches the shipping address" in steps
    assert "No PayPal order to check for tracking" in steps  # seeded orders never went through PayPal
    assert steps[-1] == f"Case file ready: {len(case.evidence)} evidence items"


@respx.mock
async def test_case_b_untracked_not_as_described(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-B").respond(200, json=dispute_json(
        "PP-D-B", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "12.00"))
    did = make_dispute(engine, demo["B"], "PP-D-B", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", amount="12.00")

    case, steps = await run_investigation(engine, did, pp)
    ev = by_type(case)
    assert "tracking" not in ev and "delivery_confirmation" not in ev
    assert f"No shipment or tracking recorded for order #{demo['B']}" in steps
    assert any("not what was in the listing" in e.summary for e in ev["buyer_message"])
    assert "Transparent TPU phone case for iPhone 15" in ev["listing"][0].summary


@respx.mock
async def test_case_c_delivered_elsewhere(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-C").respond(200, json=dispute_json("PP-D-C", amount="45.00"))
    did = make_dispute(engine, demo["C"], "PP-D-C", amount="45.00")

    case, steps = await run_investigation(engine, did, pp)
    conf = by_type(case)["delivery_confirmation"][0].summary
    assert "does NOT match" in conf and "1540 Industrial Pkwy" in conf and "88 Lake Road" in conf
    assert "Delivery address differs from the shipping address" in steps
    assert any(s.startswith("Found tracking 1Z999AA10123456784 (UPS), delivered") for s in steps)


# ---- real sandbox case (order 31) with recorded PayPal responses ------------

@pytest.fixture
def order31(engine):
    with Session(engine) as s:
        earlier = Order(buyer_email="sb-hizpg53637037@personal.example.com", amount=Decimal("12"), currency="USD",
                        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        later = Order(buyer_email="sb-hizpg53637037@personal.example.com", amount=Decimal("45"), currency="USD",
                      created_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
        o = Order(buyer_email="sb-hizpg53637037@personal.example.com", amount=Decimal("89"), currency="USD",
                  paypal_order_id="2WB80238HM8587143", capture_id="4S541016HD390220V", invoice_id="DS-31-19c56b70",
                  items=[{"sku": "LAMP-01", "name": "Handmade walnut lamp", "quantity": 1}],
                  product_listing="Hand-turned walnut table lamp",
                  shipping_address={"address_line_1": "2211 N First St", "admin_area_2": "San Jose",
                                    "admin_area_1": "CA", "postal_code": "95131", "country_code": "US"},
                  created_at=datetime(2026, 10, 31, 10, 50, tzinfo=timezone.utc))
        s.add_all([earlier, later, o]); s.commit(); s.refresh(o)
        s.add(Shipment(order_id=o.id, carrier="USPS", tracking_number="9400111899562837465012", status="SHIPPED"))
        s.commit()
        return o.id, earlier.id


def mock_real_paypal(router):
    router.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    router.get(f"{DISPUTES}/PP-R-ZWL-10195422").respond(200, json=REAL_GET)
    router.get(f"{BASE}/v2/payments/captures/4S541016HD390220V").respond(200, json=REAL_CAPTURE)
    router.get(f"{BASE}/v2/checkout/orders/2WB80238HM8587143").respond(200, json=REAL_ORDER)


@respx.mock
async def test_real_case_paypal_evidence(pp, engine, order31):
    oid, earlier_id = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, "PP-R-ZWL-10195422", transaction_id="4S541016HD390220V")

    case, steps = await run_investigation(engine, did, pp)
    ev = by_type(case)
    inv = ev["invoice"][0]
    assert inv.source == "paypal" and inv.source_ref == "paypal:capture/4S541016HD390220V"
    assert "status PENDING (BUYER_COMPLAINT)" in inv.summary and "seller protection ELIGIBLE" in inv.summary
    pp_track = [e for e in ev["tracking"] if e.source == "paypal"][0]
    assert pp_track.source_ref == ("paypal:order/2WB80238HM8587143/tracker/"
                                   "4S541016HD390220V-9400111899562837465012")
    assert "9400111899562837465012 (USPS)" in pp_track.summary  # carrier joined from store shipment
    assert "Found tracking 9400111899562837465012 (USPS) on PayPal, shipped" in steps
    assert "Found transaction 4S541016HD390220V: 89.00 USD, seller protection ELIGIBLE" in steps
    assert case.allowed_actions == ["provide_supporting_info"]  # recorded 11:06 version
    assert case.can_fight and not case.can_settle
    assert (case.status, case.stage, case.dispute_state) == ("UNDER_REVIEW", "CHARGEBACK", "UNDER_PAYPAL_REVIEW")
    assert "PayPal currently allows: provide_supporting_info" in steps
    hist = ev["buyer_history"][0].summary
    assert f"1 earlier order(s) with this store (#{earlier_id})" in hist  # the later order is excluded
    with Session(engine) as s:
        d = s.get(Dispute, did)
        assert d.stage == "CHARGEBACK" and d.raw == REAL_GET  # refreshed from PayPal


@respx.mock
async def test_rerun_does_not_duplicate_evidence(pp, engine, order31):
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, "PP-R-ZWL-10195422", transaction_id="4S541016HD390220V")
    first, _ = await run_investigation(engine, did, pp)
    second, _ = await run_investigation(engine, did, pp)
    assert [e.id for e in first.evidence] == [e.id for e in second.evidence]
    with Session(engine) as s:
        assert len(s.exec(select(EvidenceItemRow)).all()) == len(first.evidence)


@respx.mock
async def test_paypal_failures_are_logged_and_investigation_continues(pp, engine, order31):
    oid, _ = order31
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-R-ZWL-10195422").respond(503, json={})
    respx.get(f"{BASE}/v2/payments/captures/4S541016HD390220V").respond(404, json={"debug_id": "dbg-cap"})
    respx.get(f"{BASE}/v2/checkout/orders/2WB80238HM8587143").respond(500, json={})
    did = make_dispute(engine, oid, "PP-R-ZWL-10195422", transaction_id="4S541016HD390220V")

    case, steps = await run_investigation(engine, did, pp)
    assert "Could not refresh dispute from PayPal; using last stored copy" in steps
    assert "Could not fetch transaction 4S541016HD390220V from PayPal" in steps
    assert "Could not fetch PayPal order 2WB80238HM8587143 for tracking" in steps
    assert {"order", "tracking", "listing", "buyer_history"} <= set(by_type(case))  # store side still gathered


@respx.mock
async def test_unmatched_dispute(pp, engine):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-X").respond(200, json=dispute_json("PP-D-X"))
    did = make_dispute(engine, None, "PP-D-X")
    case, steps = await run_investigation(engine, did, pp)
    assert case.evidence == [] and case.order_id is None
    assert "No store order matched this dispute" in steps


# ---- graph: pause for approval, resume -----------------------------------------

@respx.mock
async def test_graph_pauses_for_approval_and_resumes(pp, engine, order31, tmp_path):
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, "PP-R-ZWL-10195422", transaction_id="4S541016HD390220V")
    ckpt = str(tmp_path / "ckpt.db")

    result = await run_agents(did, "run:PP-R:1", engine, client=pp, checkpoint_path=ckpt)
    assert result["__interrupt__"][0].value["dispute_row_id"] == did
    assert result["case_file"]["dispute_id"] == "PP-R-ZWL-10195422"
    with Session(engine) as s:
        actions = [(a.agent, a.action) for a in s.exec(select(AgentStep).order_by(AgentStep.id)).all()]
    assert any(a == "analyst" and act.startswith("Recommend GATHER_MORE") for a, act in actions)
    assert result["decision_id"] and result["recommendation"]["action"] == "GATHER_MORE"
    assert actions[-1] == ("orchestrator", "Waiting for merchant approval")
    assert not any(a[0] == "executor" for a in actions)

    posts_before = [c for c in respx.calls if c.request.method == "POST" and "oauth2" not in str(c.request.url)]
    # Resume in a "new process": fresh saver on the same checkpoint file.
    done = await resume_run("run:PP-R:1", {"approved": True, "by": "merchant"}, engine,
                            client=pp, checkpoint_path=ckpt)
    assert done["approval"] == {"approved": True, "by": "merchant"}
    # The decision row was never APPROVED (resume called directly, not via the API): rule 5 refuses.
    assert done["execution"]["status"] == "REFUSED" and "not APPROVED" in done["execution"]["reason"]
    posts_after = [c for c in respx.calls if c.request.method == "POST" and "oauth2" not in str(c.request.url)]
    assert posts_after == posts_before  # rule 5: nothing money-moving was sent
    with Session(engine) as s:
        last = s.exec(select(AgentStep).order_by(AgentStep.id.desc())).first()
        assert last.agent == "executor" and last.action.startswith("Refused to execute: decision is PROPOSED")
        inv_steps = s.exec(select(AgentStep).where(AgentStep.agent == "investigator")).all()
    n_inv = len(inv_steps)

    with pytest.raises(ValueError, match="not waiting for approval"):
        await resume_run("run:PP-R:1", {"approved": True}, engine, client=pp, checkpoint_path=ckpt)
    with Session(engine) as s:  # investigation was not re-run by resume
        assert len(s.exec(select(AgentStep).where(AgentStep.agent == "investigator")).all()) == n_inv


@respx.mock
async def test_rejected_approval_skips_execution(pp, engine, order31, tmp_path):
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, "PP-R-ZWL-10195422", transaction_id="4S541016HD390220V")
    ckpt = str(tmp_path / "ckpt.db")
    await run_agents(did, "run:PP-R:2", engine, client=pp, checkpoint_path=ckpt)
    done = await resume_run("run:PP-R:2", {"approved": False}, engine, client=pp, checkpoint_path=ckpt)
    assert done["execution"] == {"status": "SKIPPED"}


async def test_run_failure_is_logged(engine, tmp_path, monkeypatch):
    did = make_dispute(engine, None, "PP-D-BOOM")

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("app.agents.graph.investigate", boom)
    assert await run_agents(did, "run:boom", engine, checkpoint_path=str(tmp_path / "c.db")) is None
    with Session(engine) as s:
        last = s.exec(select(AgentStep).order_by(AgentStep.id.desc())).first()
        assert last.action == "run_failed" and "kaboom" in last.detail["error"]


# ---- GET /api/disputes/{id}/case ----------------------------------------------

@respx.mock
async def test_case_endpoint(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-A").respond(200, json=dispute_json("PP-D-A"))
    did = make_dispute(engine, demo["A"], "PP-D-A")
    case, _ = await run_investigation(engine, did, pp)

    app.dependency_overrides[get_engine] = lambda: engine
    try:
        with TestClient(app) as c:
            r = c.get("/api/disputes/PP-D-A/case")
            assert r.status_code == 200
            body = r.json()
            assert body["dispute_id"] == "PP-D-A" and body["order_id"] == demo["A"]
            assert [e["id"] for e in body["evidence"]] == [e.id for e in case.evidence]
            assert {e["source_ref"].split(":")[0] for e in body["evidence"]} == {"store"}
            assert c.get("/api/disputes/NOPE/case").status_code == 404
            assert c.get("/disputes/PP-D-A/case").json() == body  # spec path, same response
            assert c.get("/disputes/NOPE/case").status_code == 404
    finally:
        app.dependency_overrides.clear()


@respx.mock
async def test_investigator_refetches_when_copy_is_stale(pp, engine, order31):
    from datetime import timedelta
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, "PP-R-ZWL-10195422", transaction_id="4S541016HD390220V")
    with Session(engine) as s:
        d = s.get(Dispute, did); d.refreshed_at = datetime.now(timezone.utc) - timedelta(minutes=5); s.add(d); s.commit()
    _, steps = await run_investigation(engine, did, pp)
    assert steps[0].startswith("Fetched dispute")  # stale copy -> fetched again
    with Session(engine) as s:
        d = s.get(Dispute, did); d.refreshed_at = datetime.now(timezone.utc); s.add(d); s.commit()
    _, steps = await run_investigation(engine, did, pp)
    assert any(x.startswith("Using the dispute as fetched from PayPal") for x in steps)


# ---- phase 9A: troubleshooting eligibility is logged by the investigation -------------

@respx.mock
async def test_investigation_logs_troubleshooting_eligibility(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    snad = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
    respx.get(f"{DISPUTES}/PP-D-D").respond(200, json=dispute_json("PP-D-D", reason=snad, amount="59.00"))
    respx.get(f"{DISPUTES}/PP-D-B").respond(200, json=dispute_json("PP-D-B", reason=snad, amount="12.00"))
    earbuds = make_dispute(engine, demo["D"], "PP-D-D", reason=snad, amount="59.00")
    case_b = make_dispute(engine, demo["B"], "PP-D-B", reason=snad, amount="12.00")
    for did in (earbuds, case_b):
        await run_investigation(engine, did, pp)
    with Session(engine) as s:
        logged = {a.dispute_id: a for a in s.exec(select(AgentStep).where(AgentStep.agent == "troubleshooter"))}
    assert logged[earbuds].action == "Eligible for buyer troubleshooting: Wireless earbuds is an electrical/electronic item"
    assert logged[earbuds].detail == {"eligible": True, "product": "Wireless earbuds", "model": "SoundPeak E5"}
    assert logged[case_b].action.startswith("Not eligible for buyer troubleshooting: the order has no electrical")

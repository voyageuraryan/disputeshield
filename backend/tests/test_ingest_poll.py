import asyncio
import copy
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.db import create_db_and_tables
from app.ingest import match_order
from app.models import AgentStep, Dispute, Order
from app.paypal.disputes import Dispute as PPDispute
from app.scripts.poll_disputes import poll_once
from tests.conftest import BASE, mock_investigation_lookups, token_response
from tests.test_webhooks import DISPUTE

LIST = f"{BASE}/v1/customer/disputes"


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    return eng


def add_order(engine, **kw):
    with Session(engine) as s:
        o = Order(buyer_email="b@x.test", amount=Decimal("1"), currency="USD", **kw)
        s.add(o); s.commit(); s.refresh(o)
        return o.id


def pp_dispute(**tx):
    return PPDispute.model_validate({"dispute_id": "D1", "disputed_transactions": [tx]})


# ---- matching --------------------------------------------------------------

def test_match_by_capture_id_first(engine):
    a = add_order(engine, paypal_order_id="O1", capture_id="CAP1", invoice_id="INV-A")
    add_order(engine, paypal_order_id="O2", capture_id="CAP2", invoice_id="INV-B")
    with Session(engine) as s:
        assert match_order(s, pp_dispute(seller_transaction_id="CAP1", invoice_number="INV-B")) == a


def test_match_by_invoice_then_custom(engine):
    a = add_order(engine, paypal_order_id="O1", invoice_id="INV-A")
    b = add_order(engine, paypal_order_id="O2")
    with Session(engine) as s:
        assert match_order(s, pp_dispute(seller_transaction_id="UNKNOWN", invoice_number="INV-A")) == a
        assert match_order(s, pp_dispute(custom=str(b))) == b


def test_seeded_store_only_orders_never_match(engine):
    seeded = add_order(engine)  # no PayPal ids: seed data
    with Session(engine) as s:
        assert match_order(s, pp_dispute(custom=str(seeded), invoice_number="whatever")) is None
        assert match_order(s, pp_dispute()) is None


# ---- poller ----------------------------------------------------------------

def summary(dispute_id, update_time):
    return {"dispute_id": dispute_id, "update_time": update_time, "reason": "OTHER", "status": "OPEN",
            "dispute_amount": {"currency_code": "USD", "value": "5.00"}}


def detail(dispute_id, update_time):
    d = copy.deepcopy(DISPUTE)
    d.update(dispute_id=dispute_id, update_time=update_time)
    return d


@respx.mock
async def test_poll_ingests_new_and_changed_only(pp, engine):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    mock_investigation_lookups(respx)
    lst = respx.get(LIST)
    lst.side_effect = [
        httpx.Response(200, json={"items": [summary("PP-D-1", "2026-10-31T10:00:00Z"),
                                            summary("PP-D-2", "2026-10-31T11:00:00Z")]}),
        httpx.Response(200, json={"items": [summary("PP-D-1", "2026-10-31T10:00:00Z"),
                                            summary("PP-D-2", "2026-10-31T11:00:00Z")]}),
        httpx.Response(200, json={"items": [summary("PP-D-1", "2026-11-01T10:00:00Z"),
                                            summary("PP-D-2", "2026-10-31T11:00:00Z")]}),
    ]
    d1 = {"update_time": "2026-10-31T10:00:00Z"}  # PayPal serves the current version
    respx.get(f"{LIST}/PP-D-1").mock(side_effect=lambda req: httpx.Response(200, json=detail("PP-D-1", d1["update_time"])))
    g2 = respx.get(f"{LIST}/PP-D-2").respond(200, json=detail("PP-D-2", "2026-10-31T11:00:00Z"))

    first = await poll_once(engine, pp)
    assert {r.paypal_dispute_id for r in first} == {"PP-D-1", "PP-D-2"}
    assert all(r.run_key for r in first)
    assert lst.calls[0].request.url.params["page_size"] == "50"

    assert await poll_once(engine, pp) == []  # nothing changed: no detail fetches
    assert g2.call_count == 1  # ingest only (investigator reuses the fresh copy); second poll fetches nothing

    d1["update_time"] = "2026-11-01T10:00:00Z"  # PP-D-1 changes at PayPal
    third = await poll_once(engine, pp)
    assert [r.paypal_dispute_id for r in third] == ["PP-D-1"]
    with Session(engine) as s:
        assert len(s.exec(select(Dispute)).all()) == 2
        runs = s.exec(select(AgentStep).where(AgentStep.action == "run_started")).all()
        assert len(runs) == 3


@respx.mock
async def test_poll_paginates(pp, engine):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    mock_investigation_lookups(respx)
    page1 = [summary(f"PP-D-{i}", "2026-10-31T10:00:00Z") for i in range(50)]
    lst = respx.get(LIST)
    lst.side_effect = [httpx.Response(200, json={"items": page1}),
                       httpx.Response(200, json={"items": [summary("PP-D-X", "2026-10-31T10:00:00Z")]})]
    respx.get(url__regex=rf"{LIST}/PP-D-.+").mock(
        side_effect=lambda req: httpx.Response(200, json=detail(req.url.path.rsplit("/", 1)[1], "2026-10-31T10:00:00Z")))
    results = await poll_once(engine, pp)
    assert len(results) == 51
    assert [c.request.url.params["page"] for c in lst.calls] == ["1", "2"]


@respx.mock
async def test_poll_skips_failing_dispute_and_continues(pp, engine):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    mock_investigation_lookups(respx)
    respx.get(LIST).respond(200, json={"items": [summary("PP-D-BAD", "2026-10-31T10:00:00Z"),
                                                 summary("PP-D-OK", "2026-10-31T10:00:00Z")]})
    respx.get(f"{LIST}/PP-D-BAD").respond(403, json={"name": "NOT_AUTHORIZED"})
    respx.get(f"{LIST}/PP-D-OK").respond(200, json=detail("PP-D-OK", "2026-10-31T10:00:00Z"))
    assert [r.paypal_dispute_id for r in await poll_once(engine, pp)] == ["PP-D-OK"]


@respx.mock
async def test_webhook_and_poll_of_same_version_run_agents_once(pp, engine):
    from app.ingest import ingest_dispute
    from app.agents.runner import run_agents

    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    mock_investigation_lookups(respx)
    respx.get(f"{LIST}/PP-D-27803").respond(200, json=DISPUTE)
    with Session(engine) as s:
        r = await ingest_dispute(s, "PP-D-27803", client=pp, event_key="evt:WH-1")
    await run_agents(r.dispute_row_id, r.run_key, engine)
    # Poller sees the same update_time, so it skips the dispute entirely.
    respx.get(LIST).respond(200, json={"items": [summary("PP-D-27803", DISPUTE["update_time"])]})
    assert await poll_once(engine, pp) == []
    with Session(engine) as s:
        # Even a forced re-ingest of the same version claims no new run.
        r2 = await ingest_dispute(s, "PP-D-27803", client=pp)
    assert r2.run_key is None


# ---- startup loop ----------------------------------------------------------

def test_lifespan_starts_and_stops_poller(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.config import get_settings

    started, cancelled = asyncio.Event(), []

    async def fake_poll_forever(engine, interval):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(interval)
            raise

    monkeypatch.setenv("POLL_DISPUTES", "true")
    monkeypatch.setenv("POLL_INTERVAL_S", "60")
    monkeypatch.setattr("app.scripts.poll_disputes.poll_forever", fake_poll_forever)
    get_settings.cache_clear()
    try:
        with TestClient(main.app):
            pass
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert started.is_set()
    assert cancelled == [60]


def test_lifespan_no_poller_when_disabled(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    calls = []

    async def fake_poll_forever(engine, interval):
        calls.append(interval)

    monkeypatch.setattr("app.scripts.poll_disputes.poll_forever", fake_poll_forever)
    with TestClient(main.app):  # conftest sets POLL_DISPUTES=false
        pass
    assert calls == []


def test_lifespan_no_poller_without_credentials(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.config import get_settings

    calls = []

    async def fake_poll_forever(engine, interval):
        calls.append(interval)

    monkeypatch.setattr("app.scripts.poll_disputes.poll_forever", fake_poll_forever)
    monkeypatch.setenv("POLL_DISPUTES", "true")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "")
    get_settings.cache_clear()
    try:
        with TestClient(main.app):
            pass
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert calls == []


@respx.mock
async def test_ingest_by_id_links_order_and_runs_once(pp, engine):
    from app.scripts.poll_disputes import ingest_by_id

    oid = add_order(engine, paypal_order_id="2WB80238HM8587143", capture_id="4S541016HD390220V",
                    invoice_id="DS-31-19c56b70")
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    mock_investigation_lookups(respx)
    case = {**detail("PP-R-ZWL-10195422", "2026-10-31T11:04:13.805Z"), "status": "UNDER_REVIEW",
            "disputed_transactions": [{"seller_transaction_id": "4S541016HD390220V",
                                       "invoice_number": "DS-31-19c56b70", "custom": "31"}]}
    respx.get(f"{LIST}/PP-R-ZWL-10195422").respond(200, json=case)
    r1 = await ingest_by_id(engine, "PP-R-ZWL-10195422", pp)
    r2 = await ingest_by_id(engine, "PP-R-ZWL-10195422", pp)
    assert r1.order_id == oid and r1.run_key and r2.run_key is None
    with Session(engine) as s:
        assert len(s.exec(select(AgentStep).where(AgentStep.action == "run_started")).all()) == 1

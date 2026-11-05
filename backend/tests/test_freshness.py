"""Phase 11: run-scoped evidence, fresh re-analyse, full refresh, freshness on decisions, one run at a time."""

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import respx
from sqlmodel import Session, select

from app.agents import runner
from app.agents.analyst import freshness, stale_required, validate
from app.agents.investigator import investigate
from app.agents.runner import run_agents
from app.models import AgentStep, Dispute, EvidenceItemRow, Message, Order, Shipment
from app.rules.evidence_rules import UNAUTHORISED, economic_baseline, evaluate
from app.schemas import Recommendation
from tests.conftest import BASE, token_response
from tests.test_investigator import (DISPUTES, REAL_CAPTURE, REAL_GET, REAL_ORDER, dispute_json, make_dispute,
                                     mock_real_paypal, order31)  # noqa: F401  (order31 is a fixture)
from tests.test_resolution import SELLER_TURN, api, artifacts, demo, engine, start_case  # noqa: F401  (fixtures)

REAL_ID = "PP-R-ZWL-10195422"
CAPTURE = "4S541016HD390220V"


async def inv(engine, did, pp, *, fresh=True, run_key=None):
    with Session(engine) as s:
        case = await investigate(s, s.get(Dispute, did), pp, run_key=run_key, fresh=fresh)
        steps = [a.action for a in s.exec(select(AgentStep).where(AgentStep.agent == "investigator")
                                          .order_by(AgentStep.id)).all()]
    return case, steps


def rows(engine, did):
    with Session(engine) as s:
        return s.exec(select(EvidenceItemRow).where(EvidenceItemRow.dispute_id == did)).all()


def no_trackers(order):
    out = copy.deepcopy(order)
    for pu in out["purchase_units"]:
        (pu.get("shipping") or {}).pop("trackers", None)
    return out


# ---- (1) run-scoped evidence ------------------------------------------------------------------------------

@respx.mock
async def test_removed_tracker_goes_inactive_and_score_drops(pp, engine, order31):
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, REAL_ID, transaction_id=CAPTURE)
    first, _ = await inv(engine, did, pp)
    tracking = [e for e in first.evidence if e.type == "tracking"]
    assert {e.source for e in tracking} == {"store", "paypal"}
    before = evaluate(first).rule_score

    # the tracker was deleted on PayPal and the shipment removed from the store
    respx.get(f"{BASE}/v2/checkout/orders/2WB80238HM8587143").respond(200, json=no_trackers(REAL_ORDER))
    with Session(engine) as s:
        for sh in s.exec(select(Shipment).where(Shipment.order_id == oid)).all():
            s.delete(sh)
        s.commit()
    second, steps = await inv(engine, did, pp)

    assert not [e for e in second.evidence if e.type == "tracking"]
    assert evaluate(second).rule_score < before
    gone = {r.id: r for r in rows(engine, did) if r.id in {e.id for e in tracking}}
    assert len(gone) == 2 and all(r.active is False for r in gone.values())  # kept for history
    dropped = [x for x in steps if x.startswith("No longer found this run")]
    assert len(dropped) == 1 and "tracking" in dropped[0]


@respx.mock
async def test_deleted_message_leaves_the_case_file(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-A").respond(200, json=dispute_json("PP-D-A"))
    did = make_dispute(engine, demo["A"], "PP-D-A")
    first, _ = await inv(engine, did, pp)
    msgs = [e for e in first.evidence if e.type == "buyer_message"]
    assert any('"got it, thanks"' in e.summary for e in msgs)

    with Session(engine) as s:
        for m in s.exec(select(Message).where(Message.order_id == demo["A"])).all():
            if "got it" in m.body.lower():
                s.delete(m)
        s.commit()
    second, _ = await inv(engine, did, pp)
    assert not any('"got it, thanks"' in e.summary for e in second.evidence)
    # an unchanged rerun drops nothing more and keeps the same ids
    third, steps = await inv(engine, did, pp)
    assert [e.id for e in third.evidence] == [e.id for e in second.evidence]


@respx.mock
async def test_capture_fetch_failure_keeps_stale_copy_and_blocks_fight(pp, engine, order31):
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, REAL_ID, transaction_id=CAPTURE)
    first, _ = await inv(engine, did, pp)
    invoice = next(e for e in first.evidence if e.type == "invoice")
    assert first.stale_sources == [] and not any(f.startswith("stale:") for f in invoice.flags)

    respx.get(f"{BASE}/v2/payments/captures/{CAPTURE}").respond(503, json={})
    second, steps = await inv(engine, did, pp)
    kept = next(e for e in second.evidence if e.type == "invoice")
    assert kept.id == invoice.id and kept.summary == invoice.summary
    assert sum(f.startswith("stale:") for f in kept.flags) == 1
    assert second.stale_sources == ["paypal:capture"]
    assert any(x.startswith("Couldn't refresh paypal:capture; using the last good copy (stale)") for x in steps)
    assert "STALE" in freshness(second) and "paypal:capture" in freshness(second)

    # invoice is required for an unauthorised-transaction dispute: FIGHT on the stale copy is rejected
    unauth = second.model_copy(update={"reason": UNAUTHORISED})
    assert [e.id for e in stale_required(unauth)] == [kept.id]
    cited = [e.id for e in unauth.evidence][:2]
    fight = Recommendation(action="FIGHT", confidence="high", reasoning="Delivered and protected.",
                           cited_evidence_ids=cited, missing_items=[], economic_note="Worth it.")
    base = economic_baseline(evaluate(unauth), unauth.amount, Decimal("25"))
    assert any("stale" in p for p in validate(fight, unauth, base, evaluate(unauth)))
    # INR doesn't require the invoice: the stale copy alone doesn't block FIGHT there
    assert stale_required(second) == []

    # a stale flag survives a second failed run without stacking, and a successful run clears it
    third, _ = await inv(engine, did, pp)
    assert sum(f.startswith("stale:") for f in next(e for e in third.evidence if e.type == "invoice").flags) == 1
    respx.get(f"{BASE}/v2/payments/captures/{CAPTURE}").respond(200, json=REAL_CAPTURE)
    fourth, _ = await inv(engine, did, pp)
    assert fourth.stale_sources == [] and not any(
        f.startswith("stale:") for f in next(e for e in fourth.evidence if e.type == "invoice").flags)


async def test_stale_copy_covered_by_a_fresh_item_of_the_same_type_is_ok():
    from app.schemas import CaseFile, EvidenceItem
    items = [EvidenceItem(id=1, type="tracking", source="paypal", source_ref="paypal:order/X/tracker/1",
                          summary="t", flags=["stale:2026-11-01T00:00:00+00:00"]),
             EvidenceItem(id=2, type="tracking", source="store", source_ref="store:shipment/1", summary="t")]
    case = CaseFile(dispute_id="D", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", amount=Decimal("1"),
                    currency="USD", due_date=None, order_id=None, evidence=items, allowed_actions=[], status="OPEN")
    assert stale_required(case) == []
    assert stale_required(case.model_copy(update={"evidence": items[:1]}))[0].id == 1


# ---- (2) fresh mode / (3) full refresh ---------------------------------------------------------------------

@respx.mock
async def test_fresh_run_refetches_even_if_fetched_seconds_ago(pp, engine, order31):
    oid, _ = order31
    mock_real_paypal(respx)
    did = make_dispute(engine, oid, REAL_ID, transaction_id=CAPTURE)
    with Session(engine) as s:
        d = s.get(Dispute, did)
        d.refreshed_at, d.raw = datetime.now(timezone.utc) - timedelta(seconds=5), REAL_GET
        s.add(d); s.commit()
    route = respx.routes[1]  # GET dispute
    await inv(engine, did, pp, fresh=False)
    assert route.call_count == 0  # webhook/poll run: reuses the copy fetched 5 s ago
    _, steps = await inv(engine, did, pp, fresh=True)
    assert route.call_count == 1 and any(x.startswith(f"Fetched dispute {REAL_ID}") for x in steps)


async def test_reanalyze_runs_fresh(api, engine, demo, monkeypatch):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    seen = {}

    async def fake_run(dispute_row_id, run_key, engine_, **kw):
        seen.update(kw, did=dispute_row_id)
        runner.release(dispute_row_id)

    monkeypatch.setattr("app.api.disputes.run_agents", fake_run)
    assert api.post("/api/disputes/PP-D-A/reanalyze").status_code == 202
    assert seen["fresh"] is True and seen["claimed"] is True


@respx.mock
async def test_unmatched_dispute_is_matched_once_the_order_exists(pp, engine):
    mock_real_paypal(respx)
    did = make_dispute(engine, None, REAL_ID, transaction_id=CAPTURE)
    # 2026-11-05: the store row is missing (e.g. an empty database after a restart): the investigator rebuilds it from
    # PayPal's own copy (capture -> order, our DS- invoice) instead of leaving the case without its order
    case, steps = await inv(engine, did, pp)
    assert case.order_id is not None and "No store order matched this dispute" not in steps
    assert any(s.startswith(f"Rebuilt store order #{case.order_id} from PayPal order 2WB80238HM8587143") for s in steps)
    assert "order" in {e.type for e in case.evidence}
    with Session(engine) as s:
        o = s.get(Order, case.order_id)
        assert o.capture_id == CAPTURE and o.paypal_order_id == "2WB80238HM8587143" and o.invoice_id.startswith("DS-")
    again, steps2 = await inv(engine, did, pp)  # next run: the rebuilt row is used, not rebuilt again
    assert again.order_id == case.order_id and sum(s.startswith("Rebuilt store order") for s in steps2) == 1
    with Session(engine) as s:
        assert len(s.exec(select(Order)).all()) == 1


@respx.mock
async def test_due_date_change_propagates(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.get(f"{DISPUTES}/PP-D-A")
    route.respond(200, json=dispute_json("PP-D-A"))
    did = make_dispute(engine, demo["A"], "PP-D-A")
    first, _ = await inv(engine, did, pp)
    assert first.due_date.date().isoformat() == "2026-11-20"

    moved = {**dispute_json("PP-D-A"), "seller_response_due_date": "2026-11-27T10:00:00.000Z",
             "dispute_life_cycle_stage": "CHARGEBACK"}
    route.respond(200, json=moved)
    second, _ = await inv(engine, did, pp)
    assert second.due_date.date().isoformat() == "2026-11-27"
    with Session(engine) as s:
        d = s.get(Dispute, did)
        assert d.seller_response_due.date().isoformat() == "2026-11-27" and d.stage == "CHARGEBACK"
    assert second.data_as_of is not None


# ---- (4) freshness on the decision ---------------------------------------------------------------------

async def test_decision_records_data_freshness(api, engine, demo):
    _, dec_id, _ = await start_case(api, engine, demo["A"], SELLER_TURN)
    body = api.get(f"/api/decisions/{dec_id}").json()
    assert body["data_as_of"] is not None and body["stale_sources"] == []


def test_freshness_line():
    from app.schemas import CaseFile
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    case = CaseFile(dispute_id="D", reason="X", amount=Decimal("1"), currency="USD", due_date=None, order_id=None,
                    evidence=[], allowed_actions=[], status="OPEN", data_as_of=now - timedelta(minutes=7))
    assert freshness(case, now) == "Data freshness: PayPal data fetched 7 min ago; all sources refreshed this run."
    stale = case.model_copy(update={"stale_sources": ["paypal:capture"]})
    assert "STALE (couldn't refresh this run, last good copy used): paypal:capture" in freshness(stale, now)


# ---- (5) one run at a time -----------------------------------------------------------------------------

async def test_double_reanalyse_starts_one_run(api, engine, demo, monkeypatch):
    _, _, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    calls = []

    async def slow_run(dispute_row_id, run_key, engine_, **kw):  # the first run is still going
        calls.append(run_key)

    monkeypatch.setattr("app.api.disputes.run_agents", slow_run)
    try:
        assert api.post("/api/disputes/PP-D-A/reanalyze").status_code == 202
        r = api.post("/api/disputes/PP-D-A/reanalyze")
        assert r.status_code == 409 and "already analysing" in r.json()["detail"]
        assert len(calls) == 1
    finally:
        runner.release(did)
    assert api.post("/api/disputes/PP-D-A/reanalyze").status_code == 202  # finished -> allowed again
    runner.release(did)


async def test_run_started_without_an_end_marker_blocks_reanalyse(api, engine, demo):
    _, _, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    with Session(engine) as s:
        s.add(AgentStep(dispute_id=did, agent="orchestrator", action="run_started"))  # another process' run
        s.commit()
        assert runner.in_progress(s, did)
    assert api.post("/api/disputes/PP-D-A/reanalyze").status_code == 409
    with Session(engine) as s:  # a crashed run's marker expires
        st = s.exec(select(AgentStep).order_by(AgentStep.id.desc())).first()
        st.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        s.add(st); s.commit()
        assert not runner.in_progress(s, did)


async def test_concurrent_run_is_skipped(engine, demo, tmp_path):
    did = make_dispute(engine, demo["A"], "PP-D-A")
    assert runner.claim(did)
    try:
        assert await run_agents(did, "run:webhook", engine, checkpoint_path=str(tmp_path / "c.db")) is None
        assert runner.PENDING[did] == "run:webhook"  # G98: queued, not lost
    finally:
        runner.PENDING.pop(did, None)
        runner.release(did)
    with Session(engine) as s:
        last = s.exec(select(AgentStep).order_by(AgentStep.id.desc())).first()
        assert last.action == "run_skipped" and "runs again when that one finishes" in last.detail["reason"]


async def test_a_run_asked_for_during_a_run_follows_it(engine, demo, tmp_path, monkeypatch):
    """G98: the webhook run that arrives mid-run is run once, fresh, right after (several collapse into one)."""
    did = make_dispute(engine, demo["A"], "PP-D-A")
    calls = []

    async def fake_run(dispute_row_id, run_key, engine_, **kw):
        calls.append((run_key, kw["fresh"]))
        if len(calls) == 1:  # while the first run is going, two more requests arrive
            assert await runner.run_agents(dispute_row_id, "run:webhook-1", engine_) is None
            assert await runner.run_agents(dispute_row_id, "run:webhook-2", engine_) is None
        return {"ok": run_key}

    monkeypatch.setattr(runner, "_run", fake_run)
    out = await runner.run_agents(did, "run:first", engine)
    assert calls == [("run:first", False), ("run:webhook-2", True)] and out == {"ok": "run:webhook-2"}
    assert did not in runner.RUNNING and did not in runner.PENDING
    with Session(engine) as s:
        acts = [a.action for a in s.exec(select(AgentStep).order_by(AgentStep.id)).all()]
    assert acts.count("run_skipped") == 2 and "Running again: something changed during the last run" in acts


@respx.mock
async def test_dispute_refresh_failure_marks_paypal_messages_stale(pp, engine, demo):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.get(f"{DISPUTES}/PP-D-A")
    msg = [{"posted_by": "BUYER", "time_posted": "2026-10-30T10:00:00.000Z", "content": "Still nothing."}]
    route.respond(200, json=dispute_json("PP-D-A", messages=msg))
    did = make_dispute(engine, demo["A"], "PP-D-A")
    first, _ = await inv(engine, did, pp)
    pp_msg = next(e for e in first.evidence if e.source == "paypal" and e.type == "buyer_message")
    assert not pp_msg.flags and first.stale_sources == []

    route.respond(503, json={})
    second, _ = await inv(engine, did, pp)
    kept = next(e for e in second.evidence if e.id == pp_msg.id)
    assert [f for f in kept.flags if f.startswith("stale:")] and "paypal:dispute" in second.stale_sources
    assert all(not e.flags or not any(f.startswith("stale:") for f in e.flags)
               for e in second.evidence if e.source == "store")  # store data was read fresh

    route.respond(200, json=dispute_json("PP-D-A", messages=msg))
    third, _ = await inv(engine, did, pp)
    assert third.stale_sources == [] and not next(e for e in third.evidence if e.id == pp_msg.id).flags

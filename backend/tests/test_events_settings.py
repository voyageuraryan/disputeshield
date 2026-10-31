import json
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.models import AgentStep, Dispute
from tests.test_resolution import (  # noqa: F401  (fixtures)
    SELLER_TURN,
    api,
    artifacts,
    demo,
    start_case,
)


@pytest.fixture
def engine(tmp_path):
    """A real SQLite file, like production: each thread gets its own connection. (The in-memory StaticPool
    used elsewhere shares ONE connection between the test thread and the streaming server thread, which
    races when a test writes while a stream reads.)"""
    from sqlmodel import create_engine

    from app.db import create_db_and_tables

    eng = create_engine(f"sqlite:///{tmp_path / 'events.db'}", connect_args={"check_same_thread": False})
    create_db_and_tables(eng)
    return eng


@pytest.fixture
def fast_sse(monkeypatch):
    monkeypatch.setattr("app.api.events.POLL_S", 0.02)
    monkeypatch.setattr("app.api.events.HEARTBEAT_S", 0.05)


def read_events(resp_or_lines, until=None, limit=500):
    """Parse an SSE stream into a list of (event, id, data) until `until(events)` or the stream ends.
    Accepts a response or a shared line iterator (a stream can only be iterated once)."""
    lines = resp_or_lines.iter_lines() if hasattr(resp_or_lines, "iter_lines") else resp_or_lines
    events, cur, comments = [], {}, 0
    for line in lines:
        if line.startswith(":"):
            comments += 1
        elif line.startswith("id: "):
            cur["id"] = int(line[4:])
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
        elif line.startswith("retry: "):
            cur["retry"] = int(line[7:])
        elif line == "" and cur:
            events.append(cur); cur = {}
            if until and until(events):
                break
        if len(events) > limit:
            break
    return events, comments


def make_dispute(engine, pid="PP-D-EV"):
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id=pid, reason="X", status="OPEN", amount=Decimal("1"), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        for i in range(3):
            s.add(AgentStep(dispute_id=d.id, agent="investigator", action=f"step {i}"))
        s.commit()
        return d.id


def add_step(engine, did, action):
    with Session(engine) as s:
        st = AgentStep(dispute_id=did, agent="analyst", action=action)
        s.add(st); s.commit(); s.refresh(st)
        return st.id


def test_backlog_then_end_of_stream(api, engine, fast_sse):
    make_dispute(engine)
    with api.stream("GET", "/api/disputes/PP-D-EV/events?max_seconds=0.3") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        events, comments = read_events(r)
    assert events[0] == {"retry": 3000}
    steps = [e for e in events if e.get("event") == "step"]
    assert [e["data"]["action"] for e in steps] == ["step 0", "step 1", "step 2"]
    assert [e["id"] for e in steps] == [e["data"]["id"] for e in steps]
    assert steps[0]["data"]["agent"] == "investigator" and steps[0]["data"]["created_at"]
    assert events[4]["event"] == "ready" and events[4]["data"] == {"last_id": steps[-1]["id"]}  # after backlog
    assert events[-1]["event"] == "bye" and comments >= 1  # heartbeat pings while idle


def test_resume_after_and_last_event_id(api, engine, fast_sse):
    make_dispute(engine)
    with api.stream("GET", "/disputes/PP-D-EV/events?after=1&max_seconds=0.1") as r:
        ids = [e["id"] for e in read_events(r)[0] if e.get("event") == "step"]
    assert ids == [2, 3]
    with api.stream("GET", "/disputes/PP-D-EV/events?max_seconds=0.1", headers={"Last-Event-ID": "2"}) as r:
        ids = [e["id"] for e in read_events(r)[0] if e.get("event") == "step"]
    assert ids == [3]


def test_new_step_is_pushed_live(api, engine, fast_sse):
    did = make_dispute(engine)
    with api.stream("GET", "/api/disputes/PP-D-EV/events?max_seconds=5") as r:
        lines = r.iter_lines()
        read_events(lines, until=lambda ev: sum(e.get("event") == "step" for e in ev) == 3)
        new_id = add_step(engine, did, "Recommend FIGHT")
        more, _ = read_events(lines, until=lambda ev: any(e.get("event") == "step" for e in ev))
    assert more[0]["event"] == "ready"  # backlog finished before the new step arrived
    step = next(e for e in more if e.get("event") == "step")
    assert step["id"] == new_id and step["data"]["action"] == "Recommend FIGHT"


def test_unknown_dispute_and_bad_params(api):
    assert api.get("/api/disputes/NOPE/events").status_code == 404
    assert api.get("/api/disputes/NOPE/events?max_seconds=99999").status_code == 422


# ---- settings ----------------------------------------------------------------------------

def test_settings_default_and_update(api):
    s = api.get("/api/settings").json()
    assert Decimal(s["settle_threshold"]) == Decimal("25") and s["currency"] == "USD"
    r = api.put("/api/settings", json={"settle_threshold": "50"})
    assert r.status_code == 200 and Decimal(r.json()["settle_threshold"]) == Decimal("50")
    assert Decimal(api.get("/api/settings").json()["settle_threshold"]) == Decimal("50")


@pytest.mark.parametrize("bad", ["-1", "10000.01", "12.345", "abc"])
def test_settings_validation(api, bad):
    assert api.put("/api/settings", json={"settle_threshold": bad}).status_code == 422


async def test_threshold_changes_the_recommendation(api, engine, demo):
    """Case C ($45, weak): GATHER_MORE at the default $25; SETTLE once the merchant raises it to $50."""
    _, dec_c, _ = await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C", amount="45.00")
    assert api.get(f"/api/decisions/{dec_c}").json()["recommendation"] == "GATHER_MORE"
    api.put("/api/settings", json={"settle_threshold": "50"})
    assert api.get("/api/disputes/PP-D-C").json()["checklist"]["baseline"] == "SETTLE"
    _, dec_c2, _ = await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C2", amount="45.00")
    assert api.get(f"/api/decisions/{dec_c2}").json()["recommendation"] == "SETTLE"


async def test_decision_records_threshold_and_reanalyze_supersedes(api, engine, demo):
    _, dec_c, did = await start_case(api, engine, demo["C"], SELLER_TURN, pid="PP-D-C", amount="45.00")
    d1 = api.get(f"/api/decisions/{dec_c}").json()
    assert Decimal(d1["analysed_threshold"]) == Decimal("25") and d1["recommendation"] == "GATHER_MORE"
    api.put("/api/settings", json={"settle_threshold": "50"})
    r = api.post("/api/disputes/PP-D-C/reanalyze")
    assert r.status_code == 202 and r.json()["queued"] is True
    decs = api.get("/api/disputes/PP-D-C").json()["decisions"]
    assert decs[0]["recommendation"] == "SETTLE" and Decimal(decs[0]["analysed_threshold"]) == Decimal("50")
    assert decs[1]["id"] == dec_c and decs[1]["status"] == "SUPERSEDED"
    steps = [s["action"] for s in api.get("/api/disputes/PP-D-C").json()["timeline"]]
    assert "Asked the agents to re-analyse the case" in steps


async def test_reanalyze_refused_when_resolved_or_unknown(api, engine, demo):
    await start_case(api, engine, demo["A"], SELLER_TURN)
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one(); d.status = "RESOLVED"; s.add(d); s.commit()
    assert api.post("/api/disputes/PP-D-A/reanalyze").status_code == 409
    assert api.post("/api/disputes/NOPE/reanalyze").status_code == 404


def test_new_dispute_stream(api, engine, fast_sse):
    make_dispute(engine, "PP-OLD")
    with api.stream("GET", "/api/events/disputes?max_seconds=5") as r:
        lines = r.iter_lines()
        first, _ = read_events(lines, until=lambda ev: any(e.get("event") == "ready" for e in ev))
        assert not any(e.get("event") == "dispute" for e in first)  # no backlog of old disputes
        with Session(engine) as s:
            s.add(Dispute(paypal_dispute_id="PP-NEW", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", status="OPEN",
                          amount=Decimal("61.50"), currency="USD")); s.commit()
        more, _ = read_events(lines, until=lambda ev: any(e.get("event") == "dispute" for e in ev))
    ev = next(e for e in more if e.get("event") == "dispute")
    assert ev["data"]["paypal_dispute_id"] == "PP-NEW" and ev["data"]["amount"] == "61.50"


def test_new_dispute_stream_replays_after_reconnect(api, engine, fast_sse):
    old = make_dispute(engine, "PP-A")
    make_dispute(engine, "PP-B")
    with api.stream("GET", "/api/events/disputes?max_seconds=0.2", headers={"Last-Event-ID": str(old)}) as r:
        ids = [e["data"]["paypal_dispute_id"] for e in read_events(r)[0] if e.get("event") == "dispute"]
    assert ids == ["PP-B"]


def test_refund_policy_setting(api):
    r = api.put("/api/settings", json={"refund_policy": "  Returns accepted within 30 days, unused, in original packaging.  "})
    assert r.json()["refund_policy"] == "Returns accepted within 30 days, unused, in original packaging."
    assert Decimal(r.json()["settle_threshold"]) == Decimal("25")  # untouched when not sent
    assert api.put("/api/settings", json={"refund_policy": ""}).json()["refund_policy"] is None
    assert api.put("/api/settings", json={"refund_policy": "x" * 4001}).status_code == 422


async def test_refund_policy_becomes_evidence_and_completes_snad_checklist(api, engine, demo):
    api.put("/api/settings", json={"refund_policy": "Returns within 30 days for a full refund."})
    _, _, _ = await start_case(api, engine, demo["B"], SELLER_TURN, pid="PP-D-B", amount="12.00")
    with Session(engine) as s:
        d = s.exec(select(Dispute).where(Dispute.paypal_dispute_id == "PP-D-B")).one()
        d.reason = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"; s.add(d); s.commit()
    detail = api.get("/api/disputes/PP-D-B").json()
    rp = [e for e in detail["case"]["evidence"] if e["type"] == "refund_policy"]
    assert rp and "Returns within 30 days" in rp[0]["summary"] and rp[0]["source_ref"] == "store:settings/refund_policy"
    assert "refund_policy" not in [i["type"] for i in detail["checklist"]["items"] if not i["present"]]
    src = api.get(f"/api/evidence/{rp[0]['id']}/source").json()
    assert src["origin"] == "Store settings" and "30 days" in src["record"]["refund_policy"]


async def test_no_refund_policy_logged(api, engine, demo):
    _, _, did = await start_case(api, engine, demo["A"], SELLER_TURN)
    steps = [s["action"] for s in api.get("/api/disputes/PP-D-A").json()["timeline"]]
    assert "No refund policy on file (add it in Settings)" in steps


def test_commit_wakes_stream_without_waiting_for_poll(api, engine, monkeypatch):
    """With a 10 s safety-net poll, a committed step must still arrive almost at once."""
    import time as _t
    monkeypatch.setattr("app.api.events.POLL_S", 10.0)
    monkeypatch.setattr("app.api.events.HEARTBEAT_S", 10.0)
    did = make_dispute(engine)
    with api.stream("GET", "/api/disputes/PP-D-EV/events?max_seconds=8") as r:
        lines = r.iter_lines()
        read_events(lines, until=lambda ev: any(e.get("event") == "ready" for e in ev))
        t0 = _t.monotonic()
        add_step(engine, did, "woken")
        more, _ = read_events(lines, until=lambda ev: any(e.get("event") == "step" for e in ev))
        elapsed = _t.monotonic() - t0
    assert next(e for e in more if e.get("event") == "step")["data"]["action"] == "woken"
    assert elapsed < 2.0, elapsed

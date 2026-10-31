import json
from decimal import Decimal

import pytest
from sqlmodel import Session

from app.models import AgentStep, Dispute
from tests.test_resolution import (  # noqa: F401  (fixtures)
    SELLER_TURN,
    api,
    artifacts,
    demo,
    engine,
    start_case,
)


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

"""Phase 17: alerts (dedupe via processed_events, toggles, delivery) and the MCP server (auth, rule 5, confirm echo)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
import respx
from sqlmodel import Session, select

from app import alerts as A
from app import mcp_server
from app.config import get_settings
from app.models import (AgentStep, Decision, DecisionStatus, Dispute, MerchantSettings, ProcessedEvent,
                        TroubleshootingSession, TSStatus)
from tests.conftest import BASE, token_response
from tests.test_offers import DISPUTES as DISPUTES_URL, inquiry_raw, snad_case  # noqa: F401  (fixture)
from tests.test_resolution import api, artifacts, engine  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def dispute(s, pid="PP-D-AL", due_h=40, status="OPEN"):
    d = Dispute(paypal_dispute_id=pid, reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", status=status, amount=D("89"),
                currency="USD", seller_response_due=NOW + timedelta(hours=due_h) if due_h is not None else None)
    s.add(d); s.commit(); s.refresh(d)
    return d


def alert_rows(s):
    return s.exec(select(ProcessedEvent).where(ProcessedEvent.kind == "alert")).all()


# ---- alerts: dedupe + toggles ---------------------------------------------------------------------------

def test_proposal_alert_is_queued_once(engine):
    with Session(engine) as s:
        d = dispute(s)
        dec = Decision(dispute_id=d.id, recommendation="FIGHT", confidence="high", reasoning="r")
        s.add(dec); s.commit(); s.refresh(dec)
        did_ = dec.id
        assert A.on_proposed(s, d, dec) is True
        assert A.on_proposed(s, d, dec) is False          # same decision: deduplicated
        rows = alert_rows(s)
    assert len(rows) == 1 and rows[0].key == f"alert:proposed:{did_}"
    det = rows[0].detail
    assert det["status"] == "pending" and "FIGHT" in det["subject"] and "/cases/PP-D-AL" in det["text"]
    assert "@" not in det["text"]  # no buyer personal data in alerts


def test_toggles_turn_kinds_off(engine):
    with Session(engine) as s:
        s.add(MerchantSettings(id=1, settle_threshold=D("25"), alert_on_proposed=False)); s.commit()
        d = dispute(s)
        dec = Decision(dispute_id=d.id, recommendation="SETTLE", confidence="high", reasoning="r")
        s.add(dec); s.commit(); s.refresh(dec)
        assert A.on_proposed(s, d, dec) is False and alert_rows(s) == []
        assert A.queue_deadlines(s, now=NOW)                    # deadlines still on


def test_deadline_alerts_48h_then_12h_once_each(engine):
    with Session(engine) as s:
        d = dispute(s, due_h=40)
        dispute(s, "PP-FAR", due_h=100)                         # not within 48 h
        dispute(s, "PP-PAST", due_h=-1)                         # already overdue: nothing
        dispute(s, "PP-RES", due_h=10, status="RESOLVED")       # resolved: nothing
        first = A.queue_deadlines(s, now=NOW)
        assert first == [f"alert:due48:{d.id}:{(NOW + timedelta(hours=40)).isoformat()}"]
        assert A.queue_deadlines(s, now=NOW + timedelta(hours=1)) == []          # next tick: deduplicated
        later = A.queue_deadlines(s, now=NOW + timedelta(hours=30))              # 10 h left
        assert len(later) == 1 and later[0].startswith(f"alert:due12:{d.id}:")
        assert A.queue_deadlines(s, now=NOW + timedelta(hours=31)) == []
        assert "due in 10 h" in s.exec(select(ProcessedEvent).where(ProcessedEvent.key == later[0])).one().detail["subject"]


def test_no_deadline_alert_once_a_response_is_approved(engine):
    with Session(engine) as s:
        d = dispute(s, due_h=20)
        s.add(Decision(dispute_id=d.id, recommendation="FIGHT", confidence="high", reasoning="r",
                       status=DecisionStatus.EXECUTED))
        s.commit()
        assert A.queue_deadlines(s, now=NOW) == []


def test_troubleshooting_finished_alert_once(engine):
    from app.troubleshooting.outcome import finished

    with Session(engine) as s:
        d = dispute(s)
        ts = TroubleshootingSession(dispute_id=d.id, status=TSStatus.RESOLVED_FIXED, product_name="Lamp",
                                    expires_at=NOW + timedelta(days=5), token_hash="x" * 64)
        s.add(ts); s.commit(); s.refresh(ts)
        finished(s, ts)
        finished(s, ts)
        rows = alert_rows(s)
        tid = ts.id
    assert [r.key for r in rows] == [f"alert:ts:{tid}:RESOLVED_FIXED"]


# ---- alerts: delivery -----------------------------------------------------------------------------------

@pytest.fixture
def channels(monkeypatch):
    def set_(smtp=True, slack=True):
        monkeypatch.setenv("SMTP_HOST", "smtp.example" if smtp else "")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X" if slack else "")
        get_settings.cache_clear()
    yield set_
    get_settings.cache_clear()


@respx.mock
async def test_delivery_email_and_slack_then_not_again(engine, channels):
    channels(smtp=True, slack=True)
    slack = respx.post("https://hooks.slack.com/services/T/B/X").respond(200, text="ok")
    sent = []
    with Session(engine) as s:
        s.add(MerchantSettings(id=1, settle_threshold=D("25"), alert_email_to="me@shop.example")); s.commit()
        A.queue_deadlines(s, now=NOW) if dispute(s) else None
        assert await A.deliver_pending(s, send_email=lambda *a: sent.append(a)) == 1
        assert await A.deliver_pending(s, send_email=lambda *a: sent.append(a)) == 0   # sent: never again
        row = alert_rows(s)[0]
    assert row.detail["status"] == "sent" and row.detail["channels"] == ["email", "slack"]
    assert sent[0][0] == "me@shop.example" and "due in" in sent[0][1]
    assert json.loads(slack.calls[0].request.content) == {"text": row.detail["text"]}


@respx.mock
async def test_failed_delivery_is_retried_up_to_three_times(engine, channels):
    channels(smtp=False, slack=True)
    respx.post("https://hooks.slack.com/services/T/B/X").respond(404, text="no_service")
    with Session(engine) as s:
        dispute(s)
        A.queue_deadlines(s, now=NOW)
        for _ in range(5):
            await A.deliver_pending(s)
        det = alert_rows(s)[0].detail
    assert det["status"] == "failed" and det["attempts"] == 3 and "no_service" in det["errors"][0]


async def test_without_channels_the_alert_is_recorded(engine, channels):
    channels(smtp=False, slack=False)
    with Session(engine) as s:
        dispute(s)
        A.queue_deadlines(s, now=NOW)
        await A.deliver_pending(s)
        assert alert_rows(s)[0].detail["status"] == "recorded" and A.recent(s)[0]["status"] == "recorded"


def test_alert_settings_round_trip(api):
    got = api.get("/api/settings").json()["alerts"]
    assert got["proposed"] and got["deadlines"] and got["email_to"] is None and got["slack_ready"] is False
    put = api.put("/api/settings", json={"alert_email_to": "me@shop.example", "alert_on_deadlines": False}).json()
    assert put["alerts"]["email_to"] == "me@shop.example" and put["alerts"]["deadlines"] is False
    assert api.put("/api/settings", json={"alert_email_to": "not-an-email"}).status_code == 422
    assert api.put("/api/settings", json={"alert_email_to": ""}).json()["alerts"]["email_to"] is None


# ---- MCP: auth ----------------------------------------------------------------------------------------------

H = {"Accept": "application/json, text/event-stream"}


def rpc(client, method, params=None, token=None, rid=1):
    headers = {**H, **({"Authorization": f"Bearer {token}"} if token else {})}
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                       headers=headers)


def call(client, token, name, **args):
    r = rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    text = res["content"][0]["text"] if res.get("content") else ""
    sc = res.get("structuredContent")
    if isinstance(sc, dict) and set(sc) == {"result"}:
        sc = sc["result"]
    if sc is None and not res.get("isError"):
        sc = json.loads(text) if text else None
        if isinstance(sc, dict) and set(sc) == {"result"}:
            sc = sc["result"]
    return res.get("isError", False), sc, text


@pytest.fixture
def mcp(api, engine):
    mcp_server.ENGINE_OVERRIDE, mcp_server.PAYPAL_OVERRIDE = engine, api.pp
    yield api
    mcp_server.ENGINE_OVERRIDE = mcp_server.PAYPAL_OVERRIDE = None


def test_mcp_requires_the_settings_token(mcp):
    assert rpc(mcp, "tools/list").status_code == 401
    assert rpc(mcp, "tools/list", token="dsm_wrong").status_code == 401
    created = mcp.post("/api/settings/mcp-token").json()
    token = created["token"]
    assert token.startswith("dsm_") and created["mcp_url"].endswith("/mcp")
    tools = {t["name"] for t in rpc(mcp, "tools/list", token=token).json()["result"]["tools"]}
    assert tools == {"list_open_disputes", "get_case", "explain_recommendation", "approve_decision", "reject_decision"}
    assert mcp.get("/api/settings").json()["mcp_token_created_at"]
    new = mcp.post("/api/settings/mcp-token").json()["token"]           # a new token replaces the old one
    assert rpc(mcp, "tools/list", token=token).status_code == 401
    assert mcp.delete("/api/settings/mcp-token").status_code == 204    # revoke
    assert rpc(mcp, "tools/list", token=new).status_code == 401


def test_only_the_token_hash_is_stored(mcp, engine):
    token = mcp.post("/api/settings/mcp-token").json()["token"]
    with Session(engine) as s:
        row = s.get(MerchantSettings, 1)
        assert row.mcp_token_hash == mcp_server.token_hash(token) and token not in json.dumps(row.model_dump(mode="json"))


# ---- MCP: approve obeys rule 5 + the confirmation echo -----------------------------------------------------

async def _proposed_partial(mcp, engine, did):
    from tests.test_offers import _analyse
    dec = await _analyse(mcp, engine, did, inquiry_raw())
    assert dec.recommendation == "SETTLE_PARTIAL" and dec.status == DecisionStatus.PROPOSED
    return dec


async def test_approve_via_mcp_needs_the_exact_confirmation(mcp, engine, snad_case):
    dec = await _proposed_partial(mcp, engine, snad_case)
    token = mcp.post("/api/settings/mcp-token").json()["token"]
    send = mcp.mock.post(f"{DISPUTES_URL}/PP-D-OFFER/make-offer").respond(200, json={"links": []})

    err, info, _ = call(mcp, token, "explain_recommendation", dispute_id="PP-D-OFFER")
    phrase = info["confirmation_phrase"]
    assert not err and phrase == f"APPROVE SETTLE_PARTIAL PP-D-OFFER {dec.offer_amount:.2f} USD"
    assert info["decision_id"] == dec.id and info["options"]
    assert info["draft_note"] == dec.draft_note and "draft_note" in info["how_to_approve"]  # merchant sees what's sent

    for wrong in ("yes", "APPROVE FIGHT PP-D-OFFER 200.00 USD", f"APPROVE SETTLE_PARTIAL PP-D-OFFER 200.00 USD", ""):
        err, _, text = call(mcp, token, "approve_decision", decision_id=dec.id, confirm=wrong)
        assert err and "confirmation doesn't match" in text and phrase in text
    assert send.call_count == 0
    with Session(engine) as s:
        assert s.get(Decision, dec.id).status == DecisionStatus.PROPOSED            # nothing happened

    err, out, _ = call(mcp, token, "approve_decision", decision_id=dec.id, confirm=phrase.lower(), note=None)
    assert not err and out["status"] == "EXECUTED" and out["endpoint"] == "make-offer"
    assert send.call_count == 1
    with Session(engine) as s:
        acts = [a.action for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == snad_case)).all()]
        assert s.get(Decision, dec.id).approved_at is not None
    assert any("Approval requested from an assistant (MCP)" in a for a in acts)
    assert any(a.startswith("Approved SETTLE_PARTIAL") for a in acts)

    # rule 5: the same decision can't be approved twice, and nothing more is sent
    err, _, text = call(mcp, token, "approve_decision", decision_id=dec.id, confirm=phrase)
    assert err and "only PROPOSED decisions can be approved" in text and send.call_count == 1


async def test_mcp_cannot_approve_gather_more_or_rejected(mcp, engine, snad_case):
    token = mcp.post("/api/settings/mcp-token").json()["token"]
    with Session(engine) as s:
        g = Decision(dispute_id=snad_case, recommendation="GATHER_MORE", confidence="low", reasoning="r")
        s.add(g); s.commit(); s.refresh(g)
    err, _, text = call(mcp, token, "approve_decision", decision_id=g.id, confirm="APPROVE GATHER_MORE PP-D-OFFER 200.00 USD")
    assert err and "nothing to approve" in text
    err, out, _ = call(mcp, token, "reject_decision", decision_id=g.id, reason="Not now")
    assert not err and out["status"] == "REJECTED"
    err, _, text = call(mcp, token, "approve_decision", decision_id=g.id, confirm="x")
    assert err and "REJECTED" in text


async def test_mcp_read_tools(mcp, engine, snad_case):
    await _proposed_partial(mcp, engine, snad_case)
    token = mcp.post("/api/settings/mcp-token").json()["token"]
    err, rows, _ = call(mcp, token, "list_open_disputes")
    assert not err and rows[0]["dispute_id"] == "PP-D-OFFER" and rows[0]["recommendation"] == "SETTLE_PARTIAL"
    err, case, _ = call(mcp, token, "get_case", dispute_id="PP-D-OFFER")
    assert not err and case["decision"]["recommendation"] == "SETTLE_PARTIAL" and case["evidence"]
    for e in case["evidence"]:  # buyer-written text is labelled as data for the assistant
        assert e["trust"] == ("untrusted_buyer_text" if e["type"] in ("buyer_message", "troubleshooting_record")
                              else "system")
    err, _, text = call(mcp, token, "get_case", dispute_id="NOPE")
    assert err and "unknown dispute" in text


def test_buyer_written_evidence_is_labelled_untrusted_for_assistants():
    assert mcp_server._trust("buyer_message") == mcp_server._trust("troubleshooting_record") == "untrusted_buyer_text"
    assert mcp_server._trust("tracking") == mcp_server._trust("listing") == "system"

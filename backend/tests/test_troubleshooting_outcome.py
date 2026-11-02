"""Phase 9D: troubleshooting results -> evidence, rules, a new analysis, the evidence PDF, the merchant panel."""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import respx
from sqlmodel import Session, select

from app.agents import troubleshooter as T
from app.agents.analyst import build_prompt, validate
from app.agents.investigator import Investigator, build_case_file
from app.agents.resolution import build_evidence_pdf
from app.ingest import material_state, run_key
from app.models import (AgentStep, Dispute, EvidenceItemRow, ProcessedEvent, TroubleshootingSession, TSMessage,
                        TSProof, TSStatus, TSStep)
from app.paypal.disputes import Dispute as PPDispute
from app.rules.evidence_rules import INR, SAFETY_WHY, SNAD, TS_FIXED_NOTE, economic_baseline, evaluate
from app.schemas import CaseFile, EvidenceItem, Recommendation
from app.troubleshooting import outcome
from tests import media
from tests.test_proof_uploads import clean_state, open_session, upload, view  # noqa: F401  (fixture + helpers)
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc)


def pdf_pages(path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", Path(path).read_bytes()))


def pdf_images(path) -> int:
    return len(re.findall(rb"/Subtype\s*/Image", Path(path).read_bytes()))


def set_status(engine, sid, status, **kw):
    with Session(engine) as s:
        ts = s.get(TroubleshootingSession, sid)
        ts.status = status
        for k, v in kw.items():
            setattr(ts, k, v)
        s.add(ts); s.commit()


def answer(engine, sid, statuses):
    with Session(engine) as s:
        for st, status in zip(s.exec(select(TSStep).where(TSStep.session_id == sid).order_by(TSStep.ordinal)), statuses):
            st.status = status
            s.add(st)
        s.commit()


def rec(engine, sid):
    with Session(engine) as s:
        return outcome.record(s, s.get(TroubleshootingSession, sid))


def actions(engine):
    with Session(engine) as s:
        return [a.action for a in s.exec(select(AgentStep).order_by(AgentStep.id))]


# ---- the record: one outcome flag per final status -----------------------------------------------------------

@pytest.mark.parametrize("status,answered,opened,invited,flag", [
    (TSStatus.RESOLVED_FIXED, 2, True, True, "ts_fixed"),
    (TSStatus.DEFECT_CONFIRMED, 5, True, True, "ts_defect_confirmed"),
    (TSStatus.SAFETY_STOP, 0, True, True, "ts_safety_stop"),
    (TSStatus.EXPIRED, 0, False, True, "ts_not_attempted"),      # invited, never opened
    (TSStatus.EXPIRED, 0, True, True, "ts_not_attempted"),       # opened, but no step answered
    (TSStatus.EXPIRED, 2, True, True, "ts_partial"),             # stopped midway
    (TSStatus.EXPIRED, 0, False, False, "ts_partial"),           # no invite actually went out: not "not attempted"
    (TSStatus.HANDED_TO_MERCHANT, 0, True, True, "ts_partial"),  # asked for the seller: engaged, not ignoring it
])
def test_record_flags(client, engine, status, answered, opened, invited, flag):
    sid, _ = open_session(client, engine)
    answer(engine, sid, ["DONE_NOT_FIXED"] * answered)
    set_status(engine, sid, status, buyer_last_seen_at=NOW if opened else None, invited_at=NOW if invited else None)
    r = rec(engine, sid)
    assert r.flags == [flag]
    assert f"steps answered {answered} of 5" in r.summary
    assert ("never opened" in r.summary) is (not opened)


def test_no_record_while_running_or_when_the_dispute_resolved(client, engine):
    sid, _ = open_session(client, engine)
    assert rec(engine, sid) is None  # ACTIVE
    set_status(engine, sid, TSStatus.EXPIRED, ended_reason="dispute resolved")
    assert rec(engine, sid) is None


@pytest.mark.parametrize("kind", ["duplicate", "before_dispute", "vision_no", "other_case"])
def test_proof_suspect(client, engine, kind):
    sid, h = open_session(client, engine, created=datetime(2026, 10, 30, tzinfo=timezone.utc))
    steps = view(client, sid, h)["steps"]
    pic = media.jpeg(pattern=4, taken="2026:10:20 10:00:00" if kind == "before_dispute" else None)
    upload(client, sid, h, steps[4]["id"], pic)
    if kind == "duplicate":
        upload(client, sid, h, steps[3]["id"], pic)
    if kind == "vision_no":
        with Session(engine) as s:
            p = s.exec(select(TSProof)).one()
            p.vision_verdict = {"shows_step": "no", "what_is_visible": "a cat", "concerns": "unrelated item"}
            s.add(p); s.commit()
    if kind == "other_case":
        sid2, h2 = open_session(client, engine, pid="PP-TS-2")
        upload(client, sid2, h2, view(client, sid2, h2)["steps"][4]["id"], pic)
        sid = sid2
    set_status(engine, sid, TSStatus.DEFECT_CONFIRMED)
    r = rec(engine, sid)
    assert r.flags == ["ts_defect_confirmed", "ts_proof_suspect"]
    assert r.suspect and all(s.startswith("proof ") for s in r.suspect)
    if kind == "vision_no":
        assert "photo check concerns: unrelated item" in r.summary


def test_clean_proof_is_not_suspect(client, engine):
    sid, h = open_session(client, engine)
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.jpeg(pattern=3))
    set_status(engine, sid, TSStatus.DEFECT_CONFIRMED)
    r = rec(engine, sid)
    assert r.flags == ["ts_defect_confirmed"] and "proofs uploaded: 1 photo(s), 0 video(s)" in r.summary


# ---- rules (deterministic, SNAD only) ----------------------------------------------------------------------------

def ev(i, type, flags=(), source="store"):
    return EvidenceItem(id=i, type=type, source=source, source_ref=f"{source}:{type}/{i}", summary=type, flags=list(flags))


def snad(*extra, reason=SNAD):
    base = [ev(1, "listing"), ev(2, "order"), ev(3, "refund_policy"), ev(4, "tracking"), ev(5, "buyer_message")]
    return CaseFile(dispute_id="PP-1", reason=reason, amount=Decimal("89"), currency="USD", due_date=None, order_id=1,
                    evidence=base + list(extra), allowed_actions=["provide_evidence", "accept_claim"])


def ts(*flags):
    return ev(9, "troubleshooting_record", flags, "buyer_portal")


def test_record_is_optional_supporting_absence_costs_nothing():
    without = evaluate(snad())
    assert without.rule_score == 0.82  # 0.7 + 0.3 * 2/5, exactly as before 9D
    with_rec = evaluate(snad(ts("ts_partial")))
    assert "troubleshooting_record" in with_rec.present and with_rec.rule_score == 0.85  # 0.7 + 0.3 * 3/6


def test_fixed_supports_the_merchant_with_a_note():
    c = evaluate(snad(ts("ts_fixed")))
    assert c.red_flags == [] and c.notes == [TS_FIXED_NOTE]
    b = economic_baseline(c, Decimal("89"), Decimal("25"))
    assert b.action == "FIGHT" and b.why.endswith("buyer confirmed the item works after troubleshooting")


def test_defect_confirmed_is_a_red_flag_unless_the_proof_is_suspect():
    c = evaluate(snad(ts("ts_defect_confirmed")))
    assert c.red_flags == ["defect_confirmed"] and c.rule_score == 0.55  # 0.85 - 0.3
    assert economic_baseline(c, Decimal("89"), Decimal("25")).action == "GATHER_MORE"
    assert economic_baseline(c, Decimal("20"), Decimal("25")).action == "SETTLE"  # cheap + weak: settle
    s = evaluate(snad(ts("ts_defect_confirmed", "ts_proof_suspect")))
    assert s.red_flags == [] and s.rule_score == 0.85  # no automatic penalty for the buyer...
    assert s.merchant_notes and "review the uploads yourself" in s.merchant_notes[0]  # ...the merchant decides


def test_not_attempted_supports_without_penalty():
    c = evaluate(snad(ts("ts_not_attempted")))
    assert c.red_flags == [] and c.rule_score == 0.85
    assert "invited to troubleshoot but didn't try" in economic_baseline(c, Decimal("89"), Decimal("25")).why


def test_safety_stop_is_always_gather_more():
    c = evaluate(snad(ts("ts_safety_stop")))
    assert c.safety_review and c.rule_score >= 0.7
    for amount in ("5", "89", "900"):
        b = economic_baseline(c, Decimal(amount), Decimal("25"))
        assert (b.action, b.why) == ("GATHER_MORE", SAFETY_WHY)
    case = snad(ts("ts_safety_stop"))
    b = economic_baseline(c, Decimal("89"), Decimal("25"))
    fight = Recommendation(action="FIGHT", confidence="high", reasoning="Strong listing evidence.",
                           cited_evidence_ids=[1], missing_items=[], economic_note="x")
    assert any("safety problem" in p for p in validate(fight, case, b, c))  # the LLM can't move it to FIGHT
    ok = Recommendation(action="GATHER_MORE", confidence="low", reasoning="Safety report; review personally.",
                        cited_evidence_ids=[9], missing_items=["personal review of the safety report"], economic_note="x")
    assert validate(ok, case, b, c) == []


def test_flags_ignored_outside_snad():
    c = evaluate(snad(ts("ts_defect_confirmed", "ts_safety_stop"), reason=INR))
    assert c.red_flags == [] or "defect_confirmed" not in c.red_flags
    assert not c.safety_review and c.notes == []


def test_suspect_flag_and_merchant_notes_never_reach_the_llm():
    case = snad(ts("ts_defect_confirmed", "ts_proof_suspect"))
    c = evaluate(case)
    prompt = build_prompt(case, c, economic_baseline(c, Decimal("89"), Decimal("25")), Decimal("25"))
    assert "ts_proof_suspect" not in prompt and "review the uploads yourself" not in prompt
    assert "ts_defect_confirmed" in prompt


# ---- run key + one new analysis ------------------------------------------------------------------------------------

def test_run_key_unchanged_without_a_finished_session():
    pp = PPDispute.model_validate({"dispute_id": "D1", "status": "OPEN", "reason": SNAD})
    m = material_state(pp)
    old = "|".join(f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in m.items())
    assert run_key(pp) == f"run:D1:{hashlib.sha1(old.encode()).hexdigest()[:16]}"  # pre-9D formula
    assert "troubleshooting" not in m
    assert run_key(pp, "DEFECT_CONFIRMED") != run_key(pp) != run_key(pp, "RESOLVED_FIXED")


def test_finished_session_claims_one_run(client, engine, reruns):
    sid, h = open_session(client, engine)
    r = client.post(f"/api/help/{sid}/handoff", headers=h)
    assert r.json()["status"] == "HANDED_TO_MERCHANT"
    assert len(reruns) == 1
    key = reruns[0][1]
    with Session(engine) as s:
        ts_row = s.get(TroubleshootingSession, sid)
        assert ts_row.finished_at is not None
        assert s.exec(select(ProcessedEvent).where(ProcessedEvent.key == key)).one().detail == {
            "troubleshooting": "HANDED_TO_MERCHANT"}
        assert outcome.finished(s, ts_row) is None  # idempotent: the same result never runs twice
    assert len(reruns) == 1
    assert any(a.startswith("Buyer finished troubleshooting (handed to you): re-analysing") for a in actions(engine))


def test_reanalyse_can_be_turned_off(client, engine, reruns, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("TROUBLESHOOTING_REANALYSE", "false")
    get_settings.cache_clear()
    sid, h = open_session(client, engine)
    client.post(f"/api/help/{sid}/handoff", headers=h)
    assert reruns == []
    with Session(engine) as s:
        assert s.get(TroubleshootingSession, sid).finished_at is not None  # the toast still works


def test_expiry_also_finishes(client, engine, reruns):
    sid, _ = open_session(client, engine)
    set_status(engine, sid, TSStatus.ACTIVE, expires_at=NOW - timedelta(minutes=1))
    assert client.get("/api/disputes/PP-TS-1/troubleshooting").json()["session"]["status"] == "EXPIRED"
    assert len(reruns) == 1


@respx.mock
async def test_ingest_of_the_same_paypal_state_after_a_finish_claims_nothing(pp, engine, reruns):
    from app.ingest import ingest_dispute
    from tests.conftest import BASE, token_response
    from tests.test_webhooks import DISPUTE

    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{BASE}/v1/customer/disputes/PP-D-27803").respond(200, json=DISPUTE)
    with Session(engine) as s:
        first = await ingest_dispute(s, "PP-D-27803", client=pp)
        d = s.get(Dispute, first.dispute_row_id)
        row = TroubleshootingSession(dispute_id=d.id, product_name="lamp", token_hash="x" * 64,
                                     expires_at=NOW + timedelta(days=1), status=TSStatus.DEFECT_CONFIRMED,
                                     invited_at=NOW)
        s.add(row); s.commit(); s.refresh(row)
        key = outcome.finished(s, row)
    assert key and key != first.run_key and reruns == [(first.dispute_row_id, key)]
    with Session(engine) as s:
        again = await ingest_dispute(s, "PP-D-27803", client=pp)  # webhook/poll: same PayPal state + same result
    assert again.run_key is None


# ---- investigator ---------------------------------------------------------------------------------------------------

def test_investigator_adds_one_record_and_flags_suspect_proofs_for_the_merchant(client, engine):
    sid, h = open_session(client, engine, created=datetime(2026, 10, 30, tzinfo=timezone.utc))
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.jpeg(taken="2026:10:01 10:00:00"))
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        inv = Investigator(s, d)
        inv._troubleshooting_record()
        assert build_case_file(s, d).evidence == []  # still running: not evidence yet
    set_status(engine, sid, TSStatus.DEFECT_CONFIRMED)
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        for _ in range(2):  # re-runs upsert the same item
            Investigator(s, d)._troubleshooting_record()
        items = build_case_file(s, d).evidence
    assert len(items) == 1
    item = items[0]
    assert (item.type, item.source, item.source_ref) == ("troubleshooting_record", "buyer_portal", f"ts:session/{sid}")
    assert item.flags == ["ts_defect_confirmed", "ts_proof_suspect"]
    acts = actions(engine)
    assert "Found the buyer's troubleshooting result: defect confirmed" in acts
    assert any(a.startswith("For you to check: proof 1 (step 5): taken before the dispute") for a in acts)
    assert "Buyer troubleshooting still in progress (active); not used as evidence yet" in acts


# ---- evidence PDF ----------------------------------------------------------------------------------------------------

def pdf_case():
    return CaseFile(dispute_id="PP-TS-1", reason=SNAD, amount=Decimal("89"), currency="USD", due_date=None,
                    order_id=1, evidence=[])


def test_pdf_gets_a_troubleshooting_page_and_up_to_3_clean_photos(client, engine, tmp_path):
    sid, h = open_session(client, engine, created=datetime(2026, 10, 30, tzinfo=timezone.utc))
    steps = view(client, sid, h)["steps"]
    for i in range(4):
        upload(client, sid, h, steps[i]["id"], media.jpeg(size=(400, 300), pattern=2 + i))
    upload(client, sid, h, steps[4]["id"], media.jpeg(taken="2026:10:01 10:00:00", pattern=9))  # suspect: left out
    upload(client, sid, h, steps[4]["id"], media.mp4(5), "v.mp4", "video/mp4")  # videos can't go in a PDF
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        assert outcome.pdf_section(s, d.id) is None  # running: no record yet
    set_status(engine, sid, TSStatus.DEFECT_CONFIRMED, finished_at=NOW)
    with Session(engine) as s:
        sec = outcome.pdf_section(s, s.exec(select(Dispute)).one().id)
    assert len(sec.images) == 3 and all("Buyer's photo for step" in c for _, c in sec.images)
    assert sec.title == "Buyer troubleshooting record" and len(sec.steps) == 5
    plain = build_evidence_pdf(tmp_path / "a.pdf", pdf_case(), "note")
    full = build_evidence_pdf(tmp_path / "b.pdf", pdf_case(), "note", troubleshooting=sec)
    assert pdf_pages(plain) == 1 and pdf_pages(full) == 2 and pdf_images(full) == 3
    assert full.stat().st_size < 10 * 1024 * 1024 and full.suffix == ".pdf"  # PayPal: pdf, < 10 MB


def test_pdf_drops_photos_rather_than_break_the_10mb_limit(client, engine, tmp_path, monkeypatch):
    from app.agents import resolution

    sid, h = open_session(client, engine)
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.jpeg(size=(400, 300), pattern=3))
    set_status(engine, sid, TSStatus.DEFECT_CONFIRMED)
    with Session(engine) as s:
        sec = outcome.pdf_section(s, s.exec(select(Dispute)).one().id)
    monkeypatch.setattr(resolution, "PDF_MAX_BYTES", 2000)
    out = build_evidence_pdf(tmp_path / "c.pdf", pdf_case(), "note", troubleshooting=sec)
    assert pdf_pages(out) == 2 and pdf_images(out) == 0


def test_fight_resolution_attaches_the_record(client, engine, monkeypatch, tmp_path):
    from app.agents.resolution import resolve
    from app.models import Decision, DecisionStatus
    from app.schemas import SellerNote

    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    sid, h = open_session(client, engine)
    set_status(engine, sid, TSStatus.RESOLVED_FIXED, finished_at=NOW)
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        d.raw = {**d.raw, "links": [{"rel": "provide_evidence", "href": "x", "method": "POST"}]}
        s.add(d); s.commit()
        Investigator(s, d)._troubleshooting_record()
        case = build_case_file(s, d)
        dec = Decision(dispute_id=d.id, recommendation="FIGHT", confidence="high", reasoning="Buyer said it works.",
                       cited_evidence_ids=[case.evidence[0].id], status=DecisionStatus.PROPOSED)
        s.add(dec); s.commit(); s.refresh(dec)

        async def llm(*, system, user, output_model):
            return SellerNote(note="The buyer confirmed on our troubleshooting page that the lamp works.",
                              cited_evidence_ids=[case.evidence[0].id])
        out = asyncio.run(resolve(s, d, case, dec.id, llm=llm))
    req = out["action_request"]
    assert req["endpoint"] == "provide-evidence" and pdf_pages(req["attachments"][0]["path"]) == 2
    assert any("with the buyer's troubleshooting record" in a for a in actions(engine))


# ---- merchant panel API ---------------------------------------------------------------------------------------------

def test_reply_as_seller_pauses_the_assistant(client, engine, monkeypatch):
    sid, h = open_session(client, engine)
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/messages", json={"body": "Hi, I'm the seller. Can you try a "
                                                                                  "different bulb?"})
    assert r.status_code == 200 and r.json()["session"]["merchant_active"] is True
    assert r.json()["messages"][-1]["role"] == "merchant"
    v = view(client, sid, h)
    assert v["seller_active"] and v["messages"][-1]["role"] == "merchant"  # the buyer sees it as "Seller"
    calls = []

    async def llm(**kw):
        calls.append(kw)
        raise AssertionError("the assistant must stay quiet while the seller is chatting")
    monkeypatch.setattr("app.agents.llm.structured_call", llm)
    r = client.post(f"/api/help/{sid}/messages", json={"body": "OK, trying now"}, headers=h)
    assert r.status_code == 202 and r.json() == {"accepted": True, "agent_working": False}  # no typing dots
    v = view(client, sid, h)
    assert v["messages"][-1]["body"] == "OK, trying now" and calls == [] and not v["agent_working"]
    # safety still works while paused
    client.post(f"/api/help/{sid}/messages", json={"body": "now it's smoking"}, headers=h)
    assert view(client, sid, h)["status"] == "SAFETY_STOP"
    # the page has ended: replying there is refused (reply in PayPal)
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/messages", json={"body": "hello?"})
    assert r.status_code == 409 and "PayPal case" in r.json()["detail"]


def test_let_the_assistant_continue(client, engine):
    sid, h = open_session(client, engine)
    client.post("/api/disputes/PP-TS-1/troubleshooting/messages", json={"body": "One moment."})
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/assistant", json={"resume": True})
    assert r.json()["session"]["merchant_active"] is False and not view(client, sid, h)["seller_active"]
    with Session(engine) as s:
        ts_row = s.get(TroubleshootingSession, sid)
        ts_row.merchant_active_at = NOW - T.MERCHANT_PAUSE - timedelta(seconds=1)
        assert not T.merchant_active(ts_row)  # the pause also ends by itself


def test_seller_reply_validation_and_no_session(client, engine):
    from tests.test_troubleshooting import make_case

    make_case(engine, rels=())
    assert client.post("/api/disputes/PP-TS-1/troubleshooting/messages", json={"body": "hi"}).status_code == 404
    assert client.post("/api/disputes/PP-TS-1/troubleshooting/messages", json={"body": ""}).status_code == 422
    assert client.post("/api/disputes/PP-TS-1/troubleshooting/messages", json={"body": "x" * 1501}).status_code == 422


def test_merchant_panel_status_and_queue(client, engine):
    sid, h = open_session(client, engine)
    upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.png())
    client.post(f"/api/help/{sid}/handoff", headers=h)
    st = client.get("/api/disputes/PP-TS-1/troubleshooting").json()
    assert st["session"]["status"] == "HANDED_TO_MERCHANT" and st["session"]["finished_at"]
    assert st["proofs"][0]["checks"]["detected_type"] == "PNG"
    assert [m["role"] for m in st["messages"]].count("agent") >= 1  # the whole chat, read-only
    row = next(r for r in client.get("/api/disputes").json() if r["paypal_dispute_id"] == "PP-TS-1")
    assert row["troubleshooting"] == "HANDED_TO_MERCHANT"


def test_merchant_stream_announces_finished_troubleshooting(client, engine):
    import threading
    import time

    from app.db import get_engine
    from app.main import app

    app.dependency_overrides[get_engine] = lambda: engine  # the stream reads with its own sessions
    sid, h = open_session(client, engine)

    def later():
        time.sleep(0.3)
        set_status(engine, sid, TSStatus.DEFECT_CONFIRMED, finished_at=datetime.now(timezone.utc))
    threading.Thread(target=later).start()
    got = None
    with client.stream("GET", "/api/events/disputes", params={"max_seconds": 3}) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and "troubleshooting" not in line and "session_id" in line:
                got = json.loads(line[5:])
                break
    assert got == {"session_id": sid, "paypal_dispute_id": "PP-TS-1", "status": "DEFECT_CONFIRMED",
                   "text": "Defect confirmed"}


def test_view_source_of_the_record(client, engine):
    sid, h = open_session(client, engine)
    client.post(f"/api/help/{sid}/handoff", headers=h)
    with Session(engine) as s:
        Investigator(s, s.exec(select(Dispute)).one())._troubleshooting_record()
        ev_id = s.exec(select(EvidenceItemRow)).one().id
    r = client.get(f"/api/evidence/{ev_id}/source").json()
    assert r["title"] == f"Buyer troubleshooting session #{sid}" and r["record"]["status"] == "HANDED_TO_MERCHANT"
    assert len(r["record"]["steps"]) == 5
    assert "token_hash" not in json.dumps(r) and "device_hash" not in json.dumps(r)


async def test_safety_case_gets_the_review_item_from_code(engine):
    """Seen live: the LLM chose GATHER_MORE for a safety stop but named nothing missing -> NEEDS_HUMAN twice."""
    from decimal import Decimal as D

    from app.agents.analyst import SAFETY_REVIEW_ITEM, analyze

    with Session(engine) as s:
        d = Dispute(paypal_dispute_id="PP-S", reason=SNAD, status="WAITING_FOR_SELLER_RESPONSE", amount=D("89"),
                    currency="USD", raw={})
        s.add(d); s.commit(); s.refresh(d)
        case = snad(ts("ts_safety_stop"))

        async def llm(*, system, user, output_model):
            assert "safety_review" in user and SAFETY_REVIEW_ITEM in system
            return Recommendation(action="GATHER_MORE", confidence="medium", reasoning="Safety report; a person reviews.",
                                  cited_evidence_ids=[9], missing_items=[], economic_note="Not about money.")
        r = await analyze(s, d, case, llm=llm)
    assert not r.needs_human and r.recommendation.missing_items == [SAFETY_REVIEW_ITEM]
    assert r.baseline.why == SAFETY_WHY

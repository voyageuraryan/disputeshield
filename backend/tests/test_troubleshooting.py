"""Phase 9A: buyer troubleshooting portal foundations (eligibility, private links, invites)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from sqlmodel import Session, select

from app.models import AgentStep, Dispute, Order, TroubleshootingSession, TSMessage, TSStatus
from app.paypal.disputes import send_message
from app.troubleshooting import access
from app.troubleshooting.access import (RateLimiter, compute_expiry, hash_token, is_expired, new_token,
                                        token_matches)
from app.troubleshooting.eligibility import check_eligibility
from tests.conftest import BASE, token_response
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)

SNAD = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
INR = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
LAMP = {"sku": "LAMP-01", "name": "Smart LED desk lamp", "quantity": 1, "category": "electronics",
        "model": "Lumina LD-200", "paypal_category": "PHYSICAL_GOODS"}
MUG = {"sku": "MUG-01", "name": "Ceramic mug", "quantity": 1, "category": "home"}
EBOOK = {"sku": "EBOOK-01", "name": "Ebook: Pottery for Beginners", "quantity": 1, "category": "digital"}
SEND = f"{BASE}/v1/customer/disputes/PP-TS-1/send-message"


@pytest.fixture(autouse=True)
def fresh_limiter():
    access.buyer_limiter.reset()
    yield
    access.buyer_limiter.reset()


def links(*rels):
    return [{"href": f"{BASE}/v1/customer/disputes/PP-TS-1/{r.replace('_', '-')}", "rel": r, "method": "POST"}
            for r in rels]


def make_case(engine, *, pid="PP-TS-1", reason=SNAD, items=(LAMP,), rels=("send_message", "accept_claim"),
              due_in=timedelta(days=10), status="WAITING_FOR_SELLER_RESPONSE", buyer_email="buyer@example.com"):
    with Session(engine) as s:
        o = Order(buyer_email=buyer_email, amount=Decimal("89.00"), currency="USD", items=list(items))
        s.add(o); s.commit(); s.refresh(o)
        d = Dispute(paypal_dispute_id=pid, order_id=o.id, reason=reason, status=status, stage="INQUIRY",
                    amount=Decimal("89.00"), currency="USD",
                    seller_response_due=datetime.now(timezone.utc) + due_in if due_in is not None else None,
                    raw={"dispute_id": pid, "status": status, "links": links(*rels)})
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def steps(engine, agent="troubleshooter"):
    with Session(engine) as s:
        return [a.action for a in s.exec(select(AgentStep).where(AgentStep.agent == agent).order_by(AgentStep.id))]


def link_parts(link):
    u = urlparse(link)
    return int(u.path.rsplit("/", 1)[1]), parse_qs(u.query)["t"][0]


# ---- eligibility --------------------------------------------------------------------

@pytest.mark.parametrize("reason,status,has_order,items,eligible,why", [
    (SNAD, "OPEN", True, [LAMP], True, "Smart LED desk lamp is an electrical/electronic item"),
    (SNAD, "WAITING_FOR_SELLER_RESPONSE", True, [MUG, LAMP], True, "Smart LED desk lamp"),
    # orders saved before categories existed: "category" held PayPal's PHYSICAL_GOODS -> catalog lookup by SKU
    (SNAD, "OPEN", True, [{"sku": "EARBUDS-01", "name": "Wireless earbuds", "category": "PHYSICAL_GOODS"}], True,
     "Wireless earbuds"),
    (SNAD, "OPEN", True, [MUG], False, "no electrical/electronic item"),
    (SNAD, "OPEN", True, [EBOOK], False, "no electrical/electronic item"),
    (SNAD, "OPEN", True, [], False, "no electrical/electronic item"),
    (SNAD, "OPEN", False, None, False, "no store order"),
    (INR, "OPEN", True, [LAMP], False, "not 'not as described'"),
    ("UNAUTHORISED", "OPEN", True, [LAMP], False, "not 'not as described'"),
    (SNAD, "RESOLVED", True, [LAMP], False, "resolved"),
])
def test_eligibility_matrix(reason, status, has_order, items, eligible, why):
    e = check_eligibility(reason, status, has_order, items)
    assert e.eligible is eligible and why in e.why
    if eligible:
        assert e.product_name and e.product_model in ("Lumina LD-200", "SoundPeak E5")


# ---- tokens, expiry, rate limit --------------------------------------------------------

def test_token_issue_verify_and_tamper():
    token, h = new_token()
    assert len(token) >= 43 and len(h) == 64 and token not in h and h == hash_token(token)
    assert token_matches(token, h)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert not token_matches(tampered, h)
    assert not token_matches("", h) and not token_matches(None, h) and not token_matches(token, None)
    assert new_token()[0] != token


def test_expiry_rules():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    due = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    assert compute_expiry(due, now) == due - timedelta(hours=48)
    assert compute_expiry(due.replace(tzinfo=None), now) == due - timedelta(hours=48)  # SQLite gives naive UTC
    assert compute_expiry(None, now) == now + timedelta(days=5)
    assert is_expired(now, now) and not is_expired(now + timedelta(seconds=1), now)


def test_rate_limiter_token_bucket():
    t = [0.0]
    rl = RateLimiter(capacity=3, refill_per_s=1.0, clock=lambda: t[0])
    assert [rl.hit("a") for _ in range(3)] == [0, 0, 0]
    assert rl.hit("a") == pytest.approx(1.0)
    assert rl.hit("b") == 0  # buckets are per session
    t[0] = 1.0
    assert rl.hit("a") == 0


def test_rate_limiter_memory_stays_bounded():
    t = [0.0]
    rl = RateLimiter(capacity=2, refill_per_s=1.0, clock=lambda: t[0], max_keys=3)
    for k in "abc":
        rl.hit(k)
    t[0] = 10.0  # all three have refilled
    rl.hit("d")
    assert set(rl._buckets) == {"d"}


# ---- send-message (documented shape) -----------------------------------------------------

@respx.mock
async def test_send_message_matches_documented_request(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(SEND).respond(200, json={"links": [{"rel": "detail", "method": "GET", "href": "x"}]})
    out = await send_message("PP-TS-1", "Hello", client=pp)
    req = route.calls[0].request
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"message": "Hello"}  # JSON body, no multipart unless files
    assert out["links"][0]["rel"] == "detail"
    for bad in ("", "   ", "x" * 2001):
        with pytest.raises(ValueError):
            await send_message("PP-TS-1", bad, client=pp)
    with pytest.raises(ValueError):
        await send_message("../x", "hi", client=pp)
    assert route.call_count == 1


# ---- invite ------------------------------------------------------------------------------

def test_invite_via_paypal_message_when_allowed(client, engine):
    did = make_case(engine)
    sent = client.mock.post(SEND).respond(200, json={"links": []})
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/invite", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent"] and not body["already_invited"] and body["via"] == "paypal_message"
    assert body["link"] is None  # the link only goes to the buyer
    assert body["session"]["status"] == "INVITED" and body["session"]["product_model"] == "Lumina LD-200"
    message = json.loads(sent.calls[0].request.content)["message"]
    assert "Lumina LD-200" in message and len(message) <= 2000
    link = next(w for w in message.split() if "/help/" in w)
    sid, token = link_parts(link)
    assert link.startswith(f"http://localhost:5173/help/{sid}?t=") and sid == body["session"]["id"]
    with Session(engine) as s:
        row = s.get(TroubleshootingSession, sid)
        assert row.token_hash == hash_token(token) and token not in json.dumps(row.model_dump(mode="json"))
        assert row.dispute_id == did and row.invited_via == "paypal_message"
        # expiry = PayPal's seller_response_due minus 48 h
        d = s.get(Dispute, did)
        assert abs((row.expires_at - (d.seller_response_due - timedelta(hours=48))).total_seconds()) < 1
        all_steps = s.exec(select(AgentStep)).all()
        assert all(token not in json.dumps(a.detail or {}) + a.action for a in all_steps)  # token never logged
    assert steps(engine) == ["Created a troubleshooting session for the Smart LED desk lamp",
                             "Sent the buyer a private troubleshooting link in the PayPal dispute thread"]
    # and the buyer link works
    assert client.get(f"/api/help/{sid}", params={"t": token}).status_code == 200


def test_invite_is_idempotent_and_resend_rotates_the_link(client, engine):
    make_case(engine)
    sent = client.mock.post(SEND).respond(200, json={"links": []})
    first = client.post("/api/disputes/PP-TS-1/troubleshooting/invite").json()
    again = client.post("/api/disputes/PP-TS-1/troubleshooting/invite").json()
    assert again["already_invited"] and not again["sent"] and again["session"]["id"] == first["session"]["id"]
    assert sent.call_count == 1
    old_sid, old_token = link_parts(next(w for w in json.loads(sent.calls[0].request.content)["message"].split()
                                         if "/help/" in w))
    re = client.post("/api/disputes/PP-TS-1/troubleshooting/invite", json={"resend": True}).json()
    assert re["sent"] and sent.call_count == 2 and re["session"]["id"] == old_sid
    new_sid, new_token = link_parts(next(w for w in json.loads(sent.calls[1].request.content)["message"].split()
                                         if "/help/" in w))
    assert new_token != old_token
    assert client.get(f"/api/help/{old_sid}", params={"t": old_token}).status_code == 404  # old link is dead
    assert client.get(f"/api/help/{new_sid}", params={"t": new_token}).status_code == 200
    with Session(engine) as s:
        assert len(s.exec(select(TroubleshootingSession)).all()) == 1
    assert any(a.endswith("(new link; the old one no longer works)") for a in steps(engine))


def test_invite_when_paypal_does_not_allow_messages(client, engine):
    make_case(engine, rels=("provide_evidence",))  # e.g. a chargeback: no send_message link
    sent = client.mock.post(SEND).respond(200, json={"links": []})
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/invite", json={"via": "paypal_message"})
    assert r.status_code == 409
    assert r.json()["detail"]["email_available"] is True and "email" in r.json()["detail"]["reason"]
    assert sent.call_count == 0
    with Session(engine) as s:
        assert s.exec(select(TroubleshootingSession)).all() == []  # nothing created on refusal
    # email-only fallback (dev stub): recorded on the timeline, link handed to the merchant
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/invite", json={"via": "email"})
    assert r.status_code == 200, r.text
    sid, token = link_parts(r.json()["link"])
    assert r.json()["via"] == "email" and sent.call_count == 0
    assert steps(engine)[-1] == "Email invite to buyer@example.com recorded (dev stub: no email is actually sent)"
    assert client.get(f"/api/help/{sid}", params={"t": token}).status_code == 200
    with Session(engine) as s:
        assert [m.body for m in s.exec(select(TSMessage))] == ["Invite sent via email"]


def test_invite_refusals(client, engine):
    make_case(engine, pid="PP-TS-INR", reason=INR)
    r = client.post("/api/disputes/PP-TS-INR/troubleshooting/invite")
    assert r.status_code == 409 and "not as described" in r.json()["detail"]["reason"]
    make_case(engine, pid="PP-TS-LATE", due_in=timedelta(hours=30))  # deadline < 48 h away
    r = client.post("/api/disputes/PP-TS-LATE/troubleshooting/invite")
    assert r.status_code == 409 and "deadline" in r.json()["detail"]["reason"]
    make_case(engine, pid="PP-TS-NOMAIL", rels=(), buyer_email=None)
    r = client.post("/api/disputes/PP-TS-NOMAIL/troubleshooting/invite", json={"via": "email"})
    assert r.status_code == 409 and "no buyer email" in r.json()["detail"]["reason"]
    assert client.post("/api/disputes/PP-NOPE/troubleshooting/invite").status_code == 404
    assert client.post("/api/disputes/PP-TS-INR/troubleshooting/invite", json={"via": "fax"}).status_code == 422


def test_invite_paypal_failure_creates_nothing(client, engine):
    make_case(engine)
    client.mock.post(SEND).respond(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg1"})
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/invite")
    assert r.status_code == 502 and r.json()["detail"]["debug_id"] == "dbg1"
    with Session(engine) as s:
        assert s.exec(select(TroubleshootingSession)).all() == []
    assert steps(engine) == ["Couldn't send the troubleshooting invite through PayPal"]


def test_no_due_date_gives_five_days(client, engine):
    make_case(engine, due_in=None)
    client.mock.post(SEND).respond(200, json={"links": []})
    exp = datetime.fromisoformat(client.post("/api/disputes/PP-TS-1/troubleshooting/invite")
                                 .json()["session"]["expires_at"]).replace(tzinfo=timezone.utc)
    assert timedelta(days=4, hours=23) < exp - datetime.now(timezone.utc) <= timedelta(days=5)


def test_status_endpoint(client, engine):
    make_case(engine)
    st = client.get("/api/disputes/PP-TS-1/troubleshooting").json()
    assert st["eligible"] and st["can_message"] and st["buyer_email_known"] and st["session"] is None
    assert st["product_name"] == "Smart LED desk lamp"


# ---- buyer access ---------------------------------------------------------------------------

def invited(client, engine, **kw):
    make_case(engine, rels=(), **kw)
    r = client.post("/api/disputes/PP-TS-1/troubleshooting/invite", json={"via": "email"})
    assert r.status_code == 200, r.text
    return link_parts(r.json()["link"])


def test_buyer_view_marks_active(client, engine):
    sid, token = invited(client, engine)
    r = client.get(f"/api/help/{sid}", params={"t": token})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ACTIVE" and body["product_model"] == "Lumina LD-200"
    assert body["steps"] == [] and body["messages"] == []  # system messages stay merchant-side
    assert set(body) == {"id", "status", "product_name", "product_model", "expires_at", "steps", "messages"}
    assert client.get(f"/api/help/{sid}", headers={"X-Help-Token": token}).status_code == 200
    assert steps(engine).count("The buyer opened the troubleshooting page") == 1  # only the first open
    with Session(engine) as s:
        assert s.get(TroubleshootingSession, sid).buyer_last_seen_at is not None


def test_buyer_bad_tokens_and_cross_session(client, engine):
    sid, token = invited(client, engine)
    make_case(engine, pid="PP-TS-2", rels=())
    other = link_parts(client.post("/api/disputes/PP-TS-2/troubleshooting/invite", json={"via": "email"})
                       .json()["link"])
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    for params in ({}, {"t": ""}, {"t": tampered}, {"t": other[1]}):
        r = client.get(f"/api/help/{sid}", params=params)
        assert r.status_code == 404 and "isn't valid" in r.json()["detail"]
    assert client.get(f"/api/help/{other[0]}", params={"t": token}).status_code == 404  # A's token on B
    assert client.get("/api/help/999", params={"t": token}).status_code == 404


def test_buyer_link_expired(client, engine):
    sid, token = invited(client, engine)
    with Session(engine) as s:
        row = s.get(TroubleshootingSession, sid)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.add(row); s.commit()
    r = client.get(f"/api/help/{sid}", params={"t": token})
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    with Session(engine) as s:
        assert s.get(TroubleshootingSession, sid).status == TSStatus.EXPIRED
    assert "The buyer's troubleshooting link expired" in steps(engine)


def test_merchant_sees_expiry_without_the_buyer_opening_it(client, engine):
    sid, _ = invited(client, engine)
    with Session(engine) as s:
        row = s.get(TroubleshootingSession, sid)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.add(row); s.commit()
    assert client.get("/api/disputes/PP-TS-1/troubleshooting").json()["session"]["status"] == "EXPIRED"
    assert steps(engine).count("The buyer's troubleshooting link expired") == 1
    client.get("/api/disputes/PP-TS-1/troubleshooting")
    assert steps(engine).count("The buyer's troubleshooting link expired") == 1  # logged once


def test_buyer_link_dead_once_dispute_resolved(client, engine):
    sid, token = invited(client, engine)
    with Session(engine) as s:
        d = s.exec(select(Dispute)).one()
        d.status = "RESOLVED"
        s.add(d); s.commit()
    assert client.get(f"/api/help/{sid}", params={"t": token}).status_code == 410


def test_buyer_route_refuses_merchant_credentials(client, engine):
    sid, token = invited(client, engine)
    r = client.get(f"/api/help/{sid}", params={"t": token}, headers={"Authorization": "Bearer merchant-session"})
    assert r.status_code == 403
    assert client.get(f"/api/help/{sid}", params={"t": token, "token": "merchant-session"}).status_code == 403


def test_buyer_rate_limit(client, engine, monkeypatch):
    sid, token = invited(client, engine)
    monkeypatch.setattr(access, "buyer_limiter", RateLimiter(capacity=2, refill_per_s=0.01))
    assert [client.get(f"/api/help/{sid}", params={"t": token}).status_code for _ in range(3)] == [200, 200, 429]
    r = client.get(f"/api/help/{sid}", params={"t": "wrong"})  # bad guesses are throttled too
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


# ---- settings + auto-invite ---------------------------------------------------------------------

def test_auto_invite_setting_roundtrip(client):
    assert client.get("/api/settings").json()["auto_invite_troubleshooting"] is False
    r = client.put("/api/settings", json={"auto_invite_troubleshooting": True})
    assert r.status_code == 200 and r.json()["auto_invite_troubleshooting"] is True
    # other settings untouched by the toggle; toggle untouched by other updates
    assert client.put("/api/settings", json={"settle_threshold": "30"}).json()["auto_invite_troubleshooting"] is True


@respx.mock
async def test_auto_invite_after_investigation(engine, pp):
    from app.merchant import update_settings
    from app.troubleshooting.service import auto_invite

    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    sent = respx.post(SEND).respond(200, json={"links": []})
    did = make_case(engine)
    with Session(engine) as s:
        await auto_invite(s, s.get(Dispute, did), pp)  # setting off: nothing happens
    assert sent.call_count == 0 and steps(engine) == []
    with Session(engine) as s:
        update_settings(s, auto_invite_troubleshooting=True)
        await auto_invite(s, s.get(Dispute, did), pp)
        await auto_invite(s, s.get(Dispute, did), pp)  # a re-run doesn't message the buyer again
    assert sent.call_count == 1
    with Session(engine) as s:
        a = s.exec(select(AgentStep).where(AgentStep.agent == "troubleshooter").order_by(AgentStep.id.desc())).first()
        assert a.detail["actor"] == "auto"
    # not allowed to message: logged, never raised
    did2 = make_case(engine, pid="PP-TS-2", rels=())
    with Session(engine) as s:
        await auto_invite(s, s.get(Dispute, did2), pp)
    assert steps(engine)[-1].startswith("Auto-invite skipped: PayPal doesn't allow messages")


@respx.mock
async def test_auto_invite_survives_paypal_errors(engine, pp):
    from app.merchant import update_settings
    from app.troubleshooting.service import auto_invite

    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.post(SEND).respond(500, json={"name": "INTERNAL_SERVER_ERROR"})
    did = make_case(engine)
    with Session(engine) as s:
        update_settings(s, auto_invite_troubleshooting=True)
        await auto_invite(s, s.get(Dispute, did), pp)
    assert steps(engine)[-1] == "Auto-invite failed; invite the buyer from the case page"
    with Session(engine) as s:
        assert s.exec(select(TroubleshootingSession)).all() == []


@respx.mock
async def test_orchestrator_invites_after_investigation_when_enabled(engine, pp, tmp_path):
    """Full agent run on the seeded earbuds case (D): investigation -> auto-invite -> analysis continues."""
    from app.agents.runner import run_agents
    from app.merchant import update_settings
    from app.scripts.seed import seed
    from tests.conftest import mock_investigation_lookups

    with Session(engine) as s:
        ids = seed(s)
        update_settings(s, auto_invite_troubleshooting=True)
        d = Dispute(paypal_dispute_id="PP-TS-1", order_id=ids["D"], reason=SNAD, status="WAITING_FOR_SELLER_RESPONSE",
                    stage="INQUIRY", amount=Decimal("59.00"), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        did = d.id
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    due = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    respx.get(f"{BASE}/v1/customer/disputes/PP-TS-1").respond(200, json={
        "dispute_id": "PP-TS-1", "reason": SNAD, "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": "59.00"},
        "seller_response_due_date": due, "update_time": "2026-11-01T10:00:00.000Z",
        "links": links("send_message", "accept_claim", "escalate")})
    mock_investigation_lookups(respx)
    sent = respx.post(SEND).respond(200, json={"links": []})

    result = await run_agents(did, "run:PP-TS-1:1", engine, client=pp, checkpoint_path=str(tmp_path / "c.db"))
    assert sent.call_count == 1 and "SoundPeak E5" in json.loads(sent.calls[0].request.content)["message"]
    assert result["decision_id"]  # the analysis still ran and proposed a decision
    with Session(engine) as s:
        acts = [(a.agent, a.action) for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == did)
                                                    .order_by(AgentStep.id))]
        row = s.exec(select(TroubleshootingSession)).one()
    names = [a for _, a in acts]
    i_elig = names.index("Eligible for buyer troubleshooting: Wireless earbuds is an electrical/electronic item")
    i_sent = names.index("Sent the buyer a private troubleshooting link in the PayPal dispute thread")
    i_analyst = next(i for i, (agent, _) in enumerate(acts) if agent == "analyst")
    assert i_elig < i_sent < i_analyst
    assert row.product_model == "SoundPeak E5" and row.status == TSStatus.INVITED

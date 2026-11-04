"""Phase 13: resolve inquiries with offers. Calculator, validator bounds, rule 5 for make-offer, offer outcomes from
PayPal (webhook -> ingest), and the inquiry-stage not-as-described case that now gets a partial offer."""

import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
import respx
from sqlmodel import Session, select

from app.agents.analyst import validate
from app.ingest import ingest_dispute
from app.models import AgentStep, Decision, DecisionStatus, Dispute, Order, ProcessedEvent
from app.paypal.disputes import NotApprovedError, make_offer, offer_events, offer_types
from app.rules.evidence_rules import SNAD, Baseline
from app.rules.money import (MoneySettings, accept_probability, best, best_offer, fight_net, offer_bounds,
                             offer_problems, options, partial_net, settle_net, win_probability)
from app.schemas import CaseFile, EvidenceItem, Recommendation
from tests.conftest import BASE, token_response
from tests.test_resolution import api, artifacts, engine  # noqa: F401  (fixtures)

DISPUTES = f"{BASE}/v1/customer/disputes"
PID = "PP-D-OFFER"
S = MoneySettings()  # fee 15, 30 min at 25/h -> handling 12.50, max offer 70%


# ---- 3. the calculator (pure) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("score,p", [(0.0, "0.15"), (0.39, "0.15"), (0.40, "0.30"), (0.54, "0.30"), (0.55, "0.45"),
                                     (0.69, "0.45"), (0.70, "0.65"), (0.84, "0.65"), (0.85, "0.80"), (1.0, "0.80")])
def test_win_probability_table(score, p):
    assert win_probability(score) == D(p)


@pytest.mark.parametrize("amount,p_win,fee,minutes,hourly,expected", [
    # -(1 - p) * (amount + fee) - minutes/60 * hourly
    ("89.00", "0.65", "15", "30", "25", "-48.90"),    # 0.35 * 104 = 36.40 + 12.50
    ("89.00", "0.15", "15", "30", "25", "-100.90"),   # 0.85 * 104 = 88.40 + 12.50
    ("200.00", "0.80", "30", "60", "40", "-86.00"),   # 0.20 * 230 = 46 + 40
    ("12.00", "0.30", "8", "0", "25", "-14.00"),      # 0.70 * 20, no handling
])
def test_fight_net(amount, p_win, fee, minutes, hourly, expected):
    s = MoneySettings(dispute_fee=D(fee), handling_minutes=D(minutes), hourly_value=D(hourly))
    assert fight_net(D(p_win), D(amount), s) == D(expected)


def test_settle_and_partial_net():
    assert settle_net(D("89")) == D("-89.00")
    assert accept_probability(D("44.50"), D("89")) == D("0.5000")          # 0.1 + 0.8 * 0.5
    assert accept_probability(D("0"), D("89")) == D("0.1000")
    assert accept_probability(D("89"), D("89")) == D("0.9000")
    # -offer * P(accept) + (1 - P(accept)) * fight: -44.50 * 0.5 + 0.5 * -100.90 = -22.25 - 50.45
    assert partial_net(D("44.50"), D("89"), D("-100.90")) == D("-72.70")


def test_best_offer_respects_bounds_and_max_offer():
    fight = fight_net(D("0.15"), D("200"), S)            # -195.25
    offer = best_offer(D("200"), fight, S)
    lo, hi = offer_bounds(D("200"), S)
    assert (lo, hi) == (D("20.00"), D("140.00")) and lo <= offer <= hi
    grid = [D(200) * p / 100 for p in range(10, 75, 5)]
    assert partial_net(offer, D("200"), fight) == max(partial_net(o, D("200"), fight) for o in grid)
    tight = MoneySettings(max_offer_pct=D("10"))
    assert best_offer(D("200"), fight, tight) == D("20.00")
    assert offer_bounds(D("200"), MoneySettings(max_offer_pct=D("95")))[1] == D("180.00")  # never above 90%


def test_options_availability_and_best():
    opts = {o.action: o for o in options(0.29, D("200"), "USD", can_fight=True, can_accept_claim=True,
                                         offer_types=["REFUND", "REPLACEMENT_WITHOUT_REFUND"], s=S)}
    assert set(opts) == {"FIGHT", "SETTLE", "SETTLE_PARTIAL", "REPLACEMENT"}
    assert opts["FIGHT"].net == D("-195.25") and opts["SETTLE"].net == D("-200.00")
    assert opts["SETTLE_PARTIAL"].net > opts["SETTLE"].net and opts["REPLACEMENT"].net is None
    assert best(list(opts.values())).action == "SETTLE_PARTIAL"
    none = {o.action: o for o in options(0.29, D("200"), "USD", can_fight=False, can_accept_claim=True,
                                         offer_types=[], s=S)}
    assert not none["SETTLE_PARTIAL"].available and not none["FIGHT"].available and "REPLACEMENT" not in none
    assert best(list(none.values())).action == "SETTLE"


# ---- 4. validator bounds -------------------------------------------------------------------------------------

def _case(**kw):
    base = dict(dispute_id=PID, reason=SNAD, amount=D("200"), currency="USD", due_date=None, order_id=1,
                evidence=[EvidenceItem(id=1, type="order", source="store", source_ref="store:order/1", summary="o")],
                allowed_actions=["accept_claim", "make_offer", "escalate", "send_message"],
                offer_types=["REFUND", "REFUND_WITH_RETURN"], stage="INQUIRY")
    return CaseFile(**{**base, **kw})


def _rec(action="SETTLE_PARTIAL", offer="80.00"):
    return Recommendation(action=action, confidence="medium", reasoning="Weak case; an offer costs less.",
                          cited_evidence_ids=[1], missing_items=[], economic_note="Offer beats a refund.",
                          offer_amount=offer)


@pytest.mark.parametrize("offer,problem", [
    ("80.00", None),
    ("20.00", None),                    # exactly 10%
    ("140.00", None),                   # exactly the 70% max-offer setting
    ("19.99", "below 10%"),
    ("140.01", "above the allowed maximum"),
    ("250.00", "above the disputed amount"),
    ("80.001", "more than 2 decimals"),
    ("eighty", "decimal string"),
    (None, "decimal string"),
])
def test_offer_bounds_in_validator(offer, problem):
    probs = validate(_rec(offer=offer), _case(), Baseline("SETTLE_PARTIAL", "rules"), money=S)
    assert (not probs) if problem is None else any(problem in p for p in probs), probs


def test_partial_only_when_paypal_lists_refund_offers_and_offer_only_with_partial():
    no_offer = _case(offer_types=[])
    assert any("SETTLE_PARTIAL is not possible" in p
               for p in validate(_rec(), no_offer, Baseline("SETTLE", "r"), money=S))
    assert any("offer_amount is only for SETTLE_PARTIAL" in p
               for p in validate(_rec("SETTLE", "80.00"), _case(), Baseline("SETTLE", "r"), money=S))
    # SETTLE <-> SETTLE_PARTIAL is the same step (no justification needed); GATHER_MORE -> it needs one
    assert validate(_rec("SETTLE", None), _case(), Baseline("SETTLE_PARTIAL", "r"), money=S) == []
    assert any("must cite a buyer_message or listing" in p
               for p in validate(_rec(), _case(), Baseline("GATHER_MORE", "r"), money=S))
    assert any("more than one step" in p for p in validate(_rec(), _case(), Baseline("FIGHT", "r"), money=S))
    assert validate(_rec("REPLACEMENT", None), _case(offer_types=["REPLACEMENT_WITHOUT_REFUND"]),
                    Baseline("SETTLE", "r"), money=S) == []


# ---- 1. make-offer: spec shape and rule 5 ------------------------------------------------------------------

class _Dec:
    def __init__(self, status):
        self.status = status


@respx.mock
async def test_make_offer_requires_an_approved_decision(pp):
    route = respx.post(f"{DISPUTES}/{PID}/make-offer").respond(200, json={"links": []})
    for status in (DecisionStatus.PROPOSED, DecisionStatus.REJECTED, DecisionStatus.EXECUTED, None):
        with pytest.raises(NotApprovedError):
            await make_offer(_Dec(status) if status else None, PID, note="n", offer_type="REFUND", amount="10.00",
                             currency="USD", client=pp)
    assert route.call_count == 0  # rule 5: nothing reached PayPal


@respx.mock
async def test_make_offer_request_shapes(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.post(f"{DISPUTES}/{PID}/make-offer").respond(200, json={"links": []})
    ok = _Dec(DecisionStatus.APPROVED)
    await make_offer(ok, PID, note="Sorry.", offer_type="REFUND", amount="80.00", currency="USD",
                     invoice_id="INV-1", client=pp)
    assert json.loads(route.calls[-1].request.content) == {
        "note": "Sorry.", "offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "80.00"},
        "invoice_id": "INV-1"}
    await make_offer(ok, PID, note="New one on its way.", offer_type="REPLACEMENT_WITHOUT_REFUND", client=pp)
    assert json.loads(route.calls[-1].request.content) == {"note": "New one on its way.",
                                                           "offer_type": "REPLACEMENT_WITHOUT_REFUND"}
    for bad in (dict(offer_type="PARTIAL_REFUND", amount="1", currency="USD"),   # not a make_offer enum value
                dict(offer_type="REFUND"),                                       # amount required
                dict(offer_type="REPLACEMENT_WITHOUT_REFUND", amount="5", currency="USD"),
                dict(offer_type="REFUND_WITH_RETURN", amount="5", currency="USD")):  # needs a return address
        with pytest.raises(ValueError):
            await make_offer(ok, PID, note="n", client=pp, **bad)
    assert route.call_count == 2


def test_offer_types_come_only_from_paypal():
    raw = {"links": [{"rel": "make_offer"}],
           "allowed_response_options": {"make_offer": {"offer_types": ["REFUND", "SOMETHING_NEW"]}}}
    assert offer_types(raw) == ["REFUND"]                          # unknown values dropped
    assert offer_types({"links": [{"rel": "make_offer"}]}) == []   # not listed -> not assumed
    assert offer_types({"links": [{"rel": "accept_claim"}],
                        "allowed_response_options": {"make_offer": {"offer_types": ["REFUND"]}}}) == ["REFUND"]
    assert offer_types({"links": [{"rel": "accept_claim"}]}) == []  # make_offer not allowed


# ---- 5/6/7. the inquiry-stage not-as-described case, end to end ---------------------------------------------

def inquiry_raw(history=None, status="WAITING_FOR_SELLER_RESPONSE", outcome=None, links=None):
    raw = {"dispute_id": PID, "reason": SNAD, "status": status, "dispute_life_cycle_stage": "INQUIRY",
           "dispute_amount": {"currency_code": "USD", "value": "200.00"},
           "update_time": "2026-11-03T10:00:00.000Z",
           "disputed_transactions": [{"seller_transaction_id": "CAP-OFFER-1"}],
           "links": [{"rel": r, "href": f"{DISPUTES}/{PID}/{r.replace('_', '-')}", "method": "POST"}
                     for r in (links if links is not None else ("accept_claim", "make_offer", "escalate",
                                                                 "send_message"))],
           "allowed_response_options": {"make_offer": {"offer_types": ["REFUND", "REFUND_WITH_RETURN"]}}}
    if history:
        raw["offer"] = {"history": history}
    if outcome:
        raw["dispute_outcome"] = {"outcome_code": outcome}
    return raw


def hist(actor, event, amount="80.00", t="2026-11-03T11:00:00.000Z"):
    return {"actor": actor, "event_type": event, "offer_type": "REFUND", "offer_time": t,
            "offer_amount": {"currency_code": "USD", "value": amount}}


@pytest.fixture
def snad_case(engine):
    """A weak not-as-described inquiry (no listing, no refund policy): rule score < 0.4 -> the rules settle."""
    with Session(engine) as s:
        o = Order(buyer_email="b@example.com", amount=D("200"), currency="USD", paypal_order_id="ORD-OFFER",
                  capture_id="CAP-OFFER-1", invoice_id="INV-77", items=[{"sku": "X", "name": "Speaker", "quantity": 1}])
        s.add(o); s.commit(); s.refresh(o)
        d = Dispute(paypal_dispute_id=PID, order_id=o.id, reason=SNAD, status="OPEN", stage="INQUIRY",
                    amount=D("200.00"), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        return d.id


async def _analyse(api, engine, did, raw):
    from app.agents.runner import run_agents
    api.mock.get(f"{DISPUTES}/{PID}").respond(200, json=raw)
    await run_agents(did, f"run:{PID}:{datetime.now(timezone.utc).timestamp()}", engine, client=api.pp, fresh=True)
    with Session(engine) as s:
        return s.exec(select(Decision).where(Decision.dispute_id == did).order_by(Decision.id.desc())).first()


async def test_inquiry_snad_now_offers_a_partial_refund(api, engine, snad_case):
    dec = await _analyse(api, engine, snad_case, inquiry_raw())
    assert dec.recommendation == "SETTLE_PARTIAL" and dec.status == DecisionStatus.PROPOSED
    assert D("20") <= dec.offer_amount <= D("140")       # 10%..70% (max-offer default) of 200
    req = dec.action_request
    assert req == {"endpoint": "make-offer", "offer_type": "REFUND", "invoice_id": "INV-77",
                   "offer_amount": {"currency_code": "USD", "value": f"{dec.offer_amount:.2f}"},
                   "note": req["note"]}
    assert f"we offer a refund of {dec.offer_amount} USD" in req["note"]
    opts = {o["action"]: o for o in api.get(f"/api/decisions/{dec.id}").json()["options"]}
    assert set(opts) >= {"FIGHT", "SETTLE", "SETTLE_PARTIAL"}
    assert D(opts["SETTLE_PARTIAL"]["net"]) > D(opts["SETTLE"]["net"])
    # without offer types listed by PayPal it stays a full refund (accept-claim), as before Phase 13
    raw = inquiry_raw()
    raw.pop("allowed_response_options")
    dec2 = await _analyse(api, engine, snad_case, raw)
    assert dec2.recommendation == "SETTLE" and dec2.offer_amount is None


async def test_merchant_edits_the_offer_then_it_is_sent(api, engine, snad_case):
    dec = await _analyse(api, engine, snad_case, inquiry_raw())
    send = api.mock.post(f"{DISPUTES}/{PID}/make-offer").respond(200, json={"links": []})
    too_low = api.post(f"/decisions/{dec.id}/approve", json={"offer_amount": "5.00"})
    assert too_low.status_code == 422 and "below 10%" in too_low.json()["detail"]
    too_high = api.post(f"/decisions/{dec.id}/approve", json={"offer_amount": "150.00"})
    assert too_high.status_code == 422 and "above the allowed maximum" in too_high.json()["detail"]
    assert send.call_count == 0

    r = api.post(f"/decisions/{dec.id}/approve", json={"offer_amount": "60.00"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "EXECUTED" and body["approved_action"] == "SETTLE_PARTIAL" and body["offer_amount"] == "60.00"
    sent = json.loads(send.calls[-1].request.content)
    assert sent["offer_type"] == "REFUND" and sent["offer_amount"] == {"currency_code": "USD", "value": "60.00"}
    assert "refund of 60.00 USD" in sent["note"]
    with Session(engine) as s:
        acts = [a.action for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == snad_case)).all()]
    assert any(a.startswith("Approved SETTLE_PARTIAL of 60.00 USD (edited from") for a in acts)
    assert any(a.startswith("Offer sent: PayPal passed a refund offer of 60.00 USD") for a in acts)


async def test_rule5_make_offer_is_refused_without_approval(api, engine, snad_case):
    from app.agents.resolution import execute_decision
    dec = await _analyse(api, engine, snad_case, inquiry_raw())
    send = api.mock.post(f"{DISPUTES}/{PID}/make-offer").respond(200, json={})
    with Session(engine) as s:
        with pytest.raises(NotApprovedError):
            await execute_decision(s, dec.id, client=api.pp)
    assert send.call_count == 0


# ---- 6. offer outcomes arrive as dispute updates (webhook -> ingest) -----------------------------------------

@respx.mock
async def test_offer_outcomes_from_paypal(pp, engine, snad_case):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    route = respx.get(f"{DISPUTES}/{PID}")
    with Session(engine) as s:  # our offer was sent (an EXECUTED SETTLE_PARTIAL decision)
        s.add(Decision(dispute_id=snad_case, recommendation="SETTLE_PARTIAL", confidence="medium", reasoning="r",
                       status=DecisionStatus.EXECUTED, offer_amount=D("80")))
        s.commit()

    route.respond(200, json=inquiry_raw([hist("SELLER", "PROPOSED")], status="UNDER_REVIEW"))
    with Session(engine) as s:
        first = await ingest_dispute(s, PID, client=pp)
    assert first.run_key is None  # PayPal processing our offer: nothing new for the merchant

    # the buyer declines -> the merchant's turn again: a new analysis
    route.respond(200, json=inquiry_raw([hist("SELLER", "PROPOSED"),
                                         hist("BUYER", "DENIED", t="2026-11-04T09:00:00.000Z")]))
    with Session(engine) as s:
        denied = await ingest_dispute(s, PID, client=pp)
        acts = [a.action for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == snad_case)
                                         .order_by(AgentStep.id)).all()]
    assert denied.run_key and denied.run_key != first.run_key
    assert "PayPal shows the buyer your offer: 80.00 USD (refund)" in acts
    assert "The buyer declined your offer: 80.00 USD (refund). The case will be analysed again" in acts

    # the same copy again (webhook redelivery / poll): no duplicate steps, no new run
    with Session(engine) as s:
        again = await ingest_dispute(s, PID, client=pp)
        n = len([a for a in s.exec(select(AgentStep).where(AgentStep.action.like("The buyer declined%"))).all()])
    assert again.run_key is None and n == 1

    # a second offer is accepted: PayPal refunds and resolves the case
    route.respond(200, json=inquiry_raw([hist("SELLER", "PROPOSED"), hist("BUYER", "DENIED", t="2026-11-04T09:00:00.000Z"),
                                         hist("SELLER", "PROPOSED", "100.00", "2026-11-04T10:00:00.000Z"),
                                         hist("BUYER", "ACCEPTED", "100.00", "2026-11-04T12:00:00.000Z")],
                                        status="RESOLVED", outcome="ACCEPTED", links=()))
    with Session(engine) as s:
        await ingest_dispute(s, PID, client=pp)
        d = s.get(Dispute, snad_case)
        acts = [a.action for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == snad_case)).all()]
    assert d.status == "RESOLVED" and d.agent_status == "RESOLVED"
    assert "The buyer accepted your offer: 100.00 USD (refund). PayPal refunds it and closes the case" in acts
    assert any(a.startswith("Dispute resolved at PayPal (ACCEPTED)") for a in acts)


def test_offer_events_are_sorted_and_flattened():
    ev = offer_events({"offer": {"history": [hist("BUYER", "DENIED", t="2026-11-04T00:00:00Z"),
                                             hist("SELLER", "PROPOSED", t="2026-11-03T00:00:00Z")]}})
    assert [(e["actor"], e["event_type"], e["amount"]) for e in ev] == [("SELLER", "PROPOSED", "80.00"),
                                                                     ("BUYER", "DENIED", "80.00")]
    assert offer_events(None) == [] and offer_events({"offer": {}}) == []


# ---- settings ------------------------------------------------------------------------------------------------

def test_money_settings_round_trip_with_fee_sources(api):
    got = api.get("/api/settings").json()
    assert (got["dispute_fee"], got["handling_minutes"], got["hourly_value"], got["max_offer_pct"]) == \
        ("15", 30, "25", 70)
    assert {s["country"] for s in got["fee_sources"]} == {"US", "IN"}
    assert any("paypal.com/in/" in s["url"] for s in got["fee_sources"])
    put = api.put("/api/settings", json={"dispute_fee": "8.00", "handling_minutes": 45, "hourly_value": "30",
                                         "max_offer_pct": 50}).json()
    assert (put["dispute_fee"], put["handling_minutes"], put["hourly_value"], put["max_offer_pct"]) == \
        ("8.00", 45, "30.00", 50)
    assert api.put("/api/settings", json={"max_offer_pct": 95}).status_code == 422
    assert api.put("/api/settings", json={"dispute_fee": "-1"}).status_code == 422


@respx.mock
async def test_denial_seen_first_by_an_agent_refresh_still_reopens_the_case(pp, engine, snad_case):
    """The buyer's denial was recorded by a refresh (investigator) before the webhook ingest: still the merchant's
    turn, because our last decision was a sent offer and the buyer's newest answer is DENIED."""
    from app.ingest import upsert_dispute
    from app.paypal.disputes import Dispute as PPDispute

    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    denied = inquiry_raw([hist("SELLER", "PROPOSED"), hist("BUYER", "DENIED", t="2026-11-04T09:00:00.000Z")])
    respx.get(f"{DISPUTES}/{PID}").respond(200, json=denied)
    with Session(engine) as s:
        s.add(Decision(dispute_id=snad_case, recommendation="SETTLE_PARTIAL", confidence="medium", reasoning="r",
                       status=DecisionStatus.EXECUTED, offer_amount=D("80"),
                       action_request={"endpoint": "make-offer", "offer_type": "REFUND"}))
        pd = PPDispute.model_validate(denied)
        pd._raw = denied
        upsert_dispute(s, pd)  # what an agent run's refresh does
        s.commit()
        res = await ingest_dispute(s, PID, client=pp)
    assert res.run_key is not None


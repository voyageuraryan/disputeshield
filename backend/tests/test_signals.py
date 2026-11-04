"""Phase 15: deterministic buyer signals: CE 3.0-LIKE prior undisputed history, serial disputer, address reuse;
privacy (hashes only), rules, validator, PDF wording, retention."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlmodel import Session, select

from app import signals as S
from app.agents.analyst import validate
from app.agents.investigator import investigate
from app.models import AgentStep, Dispute, EvidenceItemRow, Order
from app.rules.evidence_rules import INR, SNAD, UNAUTHORISED, Baseline, evaluate
from app.schemas import CaseFile, EvidenceItem, Recommendation
from tests.conftest import BASE, token_response
from tests.test_orders_tracking import CAPTURED
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
ADDR = {"address_line_1": "1 Main St", "admin_area_2": "San Jose", "admin_area_1": "CA", "postal_code": "95131",
        "country_code": "US"}
OTHER = {**ADDR, "address_line_1": "99 Elm Rd", "postal_code": "94536"}
FP = hashlib.sha256(b"Mozilla/5.0|1920x1080x24|America/Los_Angeles").hexdigest()


def order(s, days_ago, *, email="b@example.com", ip="203.0.113.7", fp=FP, addr=ADDR, captured=True, payer=None):
    o = Order(buyer_email=email, amount=D("89"), currency="USD", shipping_address=addr,
              capture_id=f"CAP{days_ago}{email[:3]}" if captured else None, created_at=NOW - timedelta(days=days_ago))
    S.capture_checkout(o, ip=ip, device_fp=fp)
    o.payer_hash = S.signal_hash("payer", payer)
    s.add(o); s.commit(); s.refresh(o)
    return o


def dispute(s, o, pid="PP-D-SIG", reason=UNAUTHORISED, raw=None):
    d = Dispute(paypal_dispute_id=pid, order_id=o.id if o else None, reason=reason, status="OPEN", amount=D("89"),
                currency="USD", raw=raw or {"create_time": NOW.isoformat()}, refreshed_at=NOW)
    s.add(d); s.commit(); s.refresh(d)
    return d


# ---- privacy: keyed hashes only ---------------------------------------------------------------------------

def test_hashes_are_keyed_deterministic_and_not_raw(monkeypatch):
    h = S.signal_hash("ip", "203.0.113.7")
    assert h == S.signal_hash("ip", " 203.0.113.7 ") and len(h) == 32 and "203" not in h
    assert h != hashlib.sha256(b"ip:203.0.113.7").hexdigest()[:32]          # keyed, not a plain hash
    assert S.signal_hash("device", "203.0.113.7") != h                       # kind is part of the input
    assert S.signal_hash("ip", "") is None and S.signal_hash("ip", None) is None
    monkeypatch.setenv("SIGNAL_HASH_KEY", "another-key")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        assert S.signal_hash("ip", "203.0.113.7") != h
    finally:
        get_settings.cache_clear()


def test_only_a_browser_sha256_is_accepted_as_device_fingerprint():
    o = Order(amount=D("1"), currency="USD")
    S.capture_checkout(o, ip="198.51.100.1", device_fp="Mozilla/5.0 raw user agent")
    assert o.device_hash is None and o.ip_hash
    S.capture_checkout(o, ip=None, device_fp=FP)
    assert o.device_hash and o.ip_hash is None


def test_client_ip_prefers_the_first_forwarded_hop():
    assert S.client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "10.0.0.2") == "203.0.113.7"
    assert S.client_ip({}, "10.0.0.2") == "10.0.0.2"


def test_checkout_stores_hashes_never_raw_values(client, engine):
    client.mock.post(f"{BASE}/v2/checkout/orders").respond(201, json={"id": "5O190127TN364715T", "status": "CREATED"})
    r = client.post("/api/store/orders", json={"sku": "LAMP-01", "device_fp": FP},
                    headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    captured = json.loads(json.dumps(CAPTURED))
    captured["purchase_units"][0]["payments"]["captures"][0]["custom_id"] = str(oid)
    captured["payer"] = {**captured.get("payer", {}), "payer_id": "A9SZTAF6P42TU"}
    client.mock.post(f"{BASE}/v2/checkout/orders/5O190127TN364715T/capture").respond(201, json=captured)
    assert client.post("/api/store/orders/5O190127TN364715T/capture").status_code == 200
    with Session(engine) as s:
        o = s.get(Order, oid)
        row = json.dumps(o.model_dump(mode="json"))
    assert o.ip_hash == S.signal_hash("ip", "203.0.113.7") and o.device_hash == S.signal_hash("device", FP)
    assert o.payer_hash == S.signal_hash("payer", "A9SZTAF6P42TU")
    for raw in ("203.0.113.7", FP, "A9SZTAF6P42TU"):
        assert raw not in row


# ---- 1. prior undisputed history (Visa CE 3.0-LIKE) ------------------------------------------------------------

def test_ce3_like_match_needs_two_priors_in_window_with_device_or_ip_plus_one(engine):
    with Session(engine) as s:
        order(s, 150)
        order(s, 300, ip="198.51.100.9")                 # device + account + address (IP differs)
        current = order(s, 0)
        h = S.prior_undisputed(s, current, dispute(s, current))
    assert "ce3_like_match" in h.flags and {"device_reused", "address_reused"} <= set(h.flags)
    assert [p["qualifies"] for p in h.priors] == [True, True]
    assert "matched on device + shipping address + account" in h.summary
    assert "not a compliance claim" in h.summary and "PayPal disputes don't follow Visa CE 3.0" in h.summary
    for secret in ("203.0.113.7", FP, S.signal_hash("ip", "203.0.113.7")):
        assert secret not in h.summary


@pytest.mark.parametrize("setup,flags", [
    # only one prior in the 120-365 day window
    (lambda s: [order(s, 150), order(s, 30)], {"partial_match", "device_reused", "ip_reused", "address_reused"}),
    # two priors but outside the window
    (lambda s: [order(s, 400), order(s, 500)], {"partial_match", "device_reused", "ip_reused", "address_reused"}),
    # in the window, but only the address matches (no primary element: no IP / device)
    (lambda s: [order(s, 150, ip=None, fp=None), order(s, 200, ip=None, fp=None)], {"partial_match", "address_reused"}),
])
def test_partial_match(engine, setup, flags):
    with Session(engine) as s:
        setup(s)
        current = order(s, 0)
        h = S.prior_undisputed(s, current, dispute(s, current))
    assert set(h.flags) == flags and "partial match" in h.summary


def test_disputed_unpaid_and_other_buyers_dont_count(engine):
    with Session(engine) as s:
        disputed = order(s, 150)
        dispute(s, disputed, pid="PP-OLD")                 # disputed earlier: excluded
        order(s, 200, captured=False)                      # never paid: excluded
        order(s, 250, email="someone@else.com")            # another buyer, same device: excluded
        current = order(s, 0)
        h = S.prior_undisputed(s, current, dispute(s, current))
    assert h.priors == [] and h.flags == []


def test_account_link_by_paypal_payer_id_when_emails_differ(engine):
    with Session(engine) as s:
        order(s, 150, email="old@example.com", payer="A9SZTAF6P42TU")
        order(s, 200, email="older@example.com", payer="A9SZTAF6P42TU")
        current = order(s, 0, email="new@example.com", payer="A9SZTAF6P42TU")
        h = S.prior_undisputed(s, current, dispute(s, current))
    assert "ce3_like_match" in h.flags


# ---- 2. serial disputer ------------------------------------------------------------------------------------

def test_serial_disputes_by_email_or_payer_within_12_months(engine):
    with Session(engine) as s:
        a, b = order(s, 100), order(s, 200)
        dispute(s, a, pid="PP-1", raw={"create_time": (NOW - timedelta(days=90)).isoformat()})
        dispute(s, None, pid="PP-2", raw={"create_time": (NOW - timedelta(days=200)).isoformat(),   # unmatched dispute,
                                          "disputed_transactions": [{"buyer": {"email": "B@example.com"}}]})  # same email
        dispute(s, b, pid="PP-OLD", raw={"create_time": (NOW - timedelta(days=500)).isoformat()})  # too old
        current = order(s, 0)
        d = dispute(s, current)
        ids = S.serial_disputes(s, current, d)
    assert len(ids) == 2


def test_one_other_dispute_is_not_serial(engine):
    with Session(engine) as s:
        a = order(s, 100)
        dispute(s, a, pid="PP-1", raw={"create_time": (NOW - timedelta(days=90)).isoformat()})
        current = order(s, 0)
        assert len(S.serial_disputes(s, current, dispute(s, current))) == 1


# ---- 3 + investigator: evidence items, flags, no raw values on the timeline ---------------------------------

async def test_investigator_adds_signal_evidence(engine, pp):
    with Session(engine) as s:
        p1, p2 = order(s, 150), order(s, 300)
        x, y = order(s, 60), order(s, 90, addr=OTHER)
        dispute(s, x, pid="PP-X", raw={"create_time": (NOW - timedelta(days=50)).isoformat()})
        dispute(s, y, pid="PP-Y", raw={"create_time": (NOW - timedelta(days=80)).isoformat()})
        current = order(s, 0)
        d = dispute(s, current, raw={"create_time": NOW.isoformat(), "dispute_id": "PP-D-SIG"})
        d.refreshed_at = datetime.now(timezone.utc)  # use the stored copy (no PayPal call)
        s.add(d); s.commit()
        case = await investigate(s, d, pp)
        steps = [a for a in s.exec(select(AgentStep).where(AgentStep.dispute_id == d.id)).all()]
    ev = {e.type: e for e in case.evidence}
    assert "ce3_like_match" in ev["prior_undisputed_history"].flags
    assert "buyer_serial_disputes" in ev["buyer_history"].flags
    assert "2 other dispute(s) by this buyer in the last 12 months" in ev["buyer_history"].summary
    assert "serial" not in ev["buyer_history"].summary.lower()   # neutral wording (it can reach PayPal in the PDF)
    dumped = json.dumps([st.model_dump(mode="json") for st in steps])
    for secret in ("203.0.113.7", FP, S.signal_hash("ip", "203.0.113.7"), S.signal_hash("device", FP)):
        assert secret not in dumped


# ---- rules, notes, validator ----------------------------------------------------------------------------------

def _case(reason, *items):
    ev = [EvidenceItem(id=i + 1, type=t, source="store", source_ref=f"store:{t}/{i}", summary=s_, flags=list(f))
          for i, (t, s_, f) in enumerate(items)]
    return CaseFile(dispute_id="D", reason=reason, amount=D("89"), currency="USD", due_date=None, order_id=1,
                    evidence=ev, allowed_actions=["provide_evidence", "accept_claim"], status="OPEN")


BASE_UNAUTH = [("tracking", "t", ["delivered"]), ("delivery_confirmation", "d", ["address_match"]),
               ("order", "o", []), ("buyer_history", "b", [])]


def test_ce3_like_history_is_unauthorised_supporting_evidence_and_partial_is_not():
    without = evaluate(_case(UNAUTHORISED, *BASE_UNAUTH)).rule_score
    with_ce3 = evaluate(_case(UNAUTHORISED, *BASE_UNAUTH, ("prior_undisputed_history", "p", ["ce3_like_match"])))
    partial = evaluate(_case(UNAUTHORISED, *BASE_UNAUTH, ("prior_undisputed_history", "p", ["partial_match"])))
    assert with_ce3.rule_score > without and "prior_undisputed_history" in with_ce3.present
    assert partial.rule_score == without and "prior_undisputed_history" not in partial.present
    assert any("similar to Visa CE 3.0 evidence; not a compliance claim" in n for n in with_ce3.notes)
    inr = _case(INR, *BASE_UNAUTH, ("prior_undisputed_history", "p", ["ce3_like_match"]))
    assert evaluate(inr).rule_score == evaluate(_case(INR, *BASE_UNAUTH)).rule_score   # UNAUTHORISED only


def test_serial_flag_is_context_never_score():
    hist = ("buyer_history", "Buyer has 5 earlier order(s); 3 other dispute(s) by this buyer in the last 12 months",
            ["buyer_serial_disputes"])
    plain = ("buyer_history", "Buyer has 5 earlier order(s)", [])
    for reason in (SNAD, UNAUTHORISED):
        a = evaluate(_case(reason, ("order", "o", []), ("listing", "l", []), hist))
        b = evaluate(_case(reason, ("order", "o", []), ("listing", "l", []), plain))
        assert a.rule_score == b.rule_score
        assert any("opened 3 other dispute(s) in the last 12 months" in n and "never a reason to fight" in n
                   for n in a.notes)
    assert evaluate(_case(INR, ("tracking", "t", []), hist)).notes == []   # INR: not part of this signal


def test_fight_cannot_rest_on_buyer_signals_alone():
    case = _case(UNAUTHORISED, *BASE_UNAUTH, ("prior_undisputed_history", "p", ["ce3_like_match"]))
    only_signals = Recommendation(action="FIGHT", confidence="high", reasoning="History matches.",
                                  cited_evidence_ids=[4, 5], missing_items=[], economic_note="n")
    assert any("can't rest on buyer history" in p for p in validate(only_signals, case, Baseline("FIGHT", "r")))
    with_proof = only_signals.model_copy(update={"cited_evidence_ids": [1, 2, 5]})
    assert not any("can't rest on buyer history" in p for p in validate(with_proof, case, Baseline("FIGHT", "r")))


# ---- PDF wording, retention -----------------------------------------------------------------------------------

def test_pdf_describes_ce3_like_evidence_without_claiming_compliance(tmp_path):
    from app.agents.resolution import CE3_PDF_NOTE, build_evidence_pdf
    from tests.test_merchant_uploads import pdf_text

    ce3 = _case(UNAUTHORISED, *BASE_UNAUTH, ("prior_undisputed_history", "p", ["ce3_like_match"]))
    text = pdf_text(build_evidence_pdf(tmp_path / "a.pdf", ce3, "note"))
    assert "similar in kind to Visa Compelling Evidence 3.0" in text and "not a claim that Visa CE 3.0 applies" in text
    plain = pdf_text(build_evidence_pdf(tmp_path / "b.pdf", _case(UNAUTHORISED, *BASE_UNAUTH), "note"))
    assert "Visa" not in plain
    assert "no raw IP" in CE3_PDF_NOTE


def test_signal_hashes_are_cleared_after_retention(engine):
    from app.troubleshooting.retention import purge_signal_hashes

    with Session(engine) as s:
        old, recent = order(s, 450), order(s, 100)
        assert purge_signal_hashes(s, now=NOW, days=400) == 1
        assert purge_signal_hashes(s, now=NOW, days=400) == 0
        o, r = s.get(Order, old.id), s.get(Order, recent.id)
    assert (o.ip_hash, o.device_hash, o.payer_hash) == (None, None, None) and r.ip_hash and r.device_hash
    assert o.shipping_address == ADDR  # the order itself stays (needed for shipping/disputes)

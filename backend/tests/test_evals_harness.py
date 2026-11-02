"""Phase 10A: the eval harness itself, offline (fake LLM, no deepeval import). The real-LLM evals live in evals/
and run with scripts/run_evals.sh."""

import asyncio
import itertools
import json
from pathlib import Path

import pytest

from app.agents import llm as llm_module
from app.agents.llm import LLMError
from evals import checks
from evals.cache import DiskCache
from evals.claims import asserted, false_claims
from evals.goldens import build_goldens
from evals.goldens.schema import DIR, load_goldens
from evals.pipeline import Limiter, RateLimited, load_into_db, prompt_hash, run_all, run_golden

GOLDENS = load_goldens()
BY_ID = {g.id: g for g in GOLDENS}


# ---- goldens -------------------------------------------------------------------------------------------------

def test_at_least_36_goldens_covering_the_whole_grid_and_the_adversarial_cases():
    assert len(GOLDENS) >= 36
    cells = {tuple(g.grid[k] for k in ("reason", "strength", "amount", "stage")) for g in GOLDENS if g.grid}
    reasons = ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "UNAUTHORISED")
    assert cells == set(itertools.product(reasons, ("strong", "partial", "weak"), ("below", "above"),
                                          ("inquiry", "chargeback")))
    for gid in ("adv-prompt-injection", "adv-listing-contradicts", "adv-address-mismatch", "adv-cancelled-tracker",
                "adv-prior-disputes", "adv-no-order-match", "adv-paypal-fetch-failed"):
        assert gid in BY_ID
    assert {g.source.split("/")[0] for g in GOLDENS} >= {"grid", "adversarial", "recorded_webhooks",
                                                         "docs", "e2e"}


def test_every_golden_rebuilds_the_same_case_file_from_db_rows():
    for g in GOLDENS:
        load_into_db(g)  # raises if build_case_file(DB) differs from the golden's case file


def test_goldens_are_versioned_and_complete():
    for g in GOLDENS:
        assert g.version == 1 and g.expected_action in g.acceptable_actions
        assert g.case_file.allowed_actions, g.id
        types = {e.type for e in g.case_file.evidence}
        assert set(g.must_cite_types) <= types, g.id  # can't demand citing evidence the case doesn't have
        if "delivery_confirmation" not in types and g.case_file.reason != "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
            assert g.must_not_claim, f"{g.id}: no delivery proof, so delivery claims must be forbidden"


def test_goldens_on_disk_match_the_builder():
    """The JSON files are generated; a rule or template change must regenerate them (a reviewable diff)."""
    recorded = Path(__file__).resolve().parents[1] / "recorded_webhooks"
    if not any(recorded.glob("*.body.json")):
        pytest.skip("recorded_webhooks/ isn't present (git-ignored)")
    built = build_goldens.build_grid() + build_goldens.build_adversarial() + build_goldens.build_recorded_and_demo()
    on_disk = {p.stem: json.loads(p.read_text()) for p in DIR.glob("*.json")}
    assert {g.id: g.model_dump(mode="json") for g in built} == on_disk


def test_recorded_goldens_carry_no_buyer_or_transaction_data():
    for g in GOLDENS:
        if g.source.startswith("recorded_webhooks"):
            assert not {"disputed_transactions", "fund_movements", "refund_details"} & set(g.paypal_raw)


# ---- claims + checks ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,phrase,hit", [
    ("The package was delivered on 15 Oct.", "was delivered", True),
    ("There is no proof the package was delivered.", "was delivered", False),
    ("Tracking shows it has not been confirmed that it was delivered.", "was delivered", False),
    ("The buyer says it never arrived; it arrived per USPS.", "it arrived", True),   # 2nd mention asserts it
    ("The buyer claims the tracking number is fake.", "tracking number is fake", False),  # reported, not asserted
    ("We can't say whether it was delivered.", "was delivered", False),
    ("This buyer is committing fraud.", "fraud", True),
    # seen in the 10A run: a request for proof isn't a claim
    ("PayPal still needs evidence that it was delivered to the buyer.", "was delivered", False),
    ("Upload proof that the item was delivered.", "was delivered", False),
    ("Tracking shows it was delivered on 15 Oct.", "was delivered", True),  # a claim (wrong if nothing shows it)
])
def test_claim_detector(text, phrase, hit):
    assert bool(asserted(text, phrase)) is hit


def out(**kw):
    base = {"action": "FIGHT", "cited_evidence_ids": [1, 2], "cited_types": ["delivery_confirmation", "tracking"],
            "reasoning": "Delivery confirmed to the shipping address.", "note": "Delivered 15 Oct (USPS).",
            "note_cited_ids": [1], "note_cited_types": ["tracking"],
            "request": {"endpoint": "provide-evidence", "attachments": 1, "evidence_type": "PROOF_OF_FULFILLMENT"}}
    return {**base, **kw}


def gold(gid="grid-inr-strong-above-chargeback"):
    return BY_ID[gid].model_dump(mode="json")


def test_checks():
    g = gold()
    assert checks.check_action(out(), g)[0] == 1.0
    assert checks.check_action(out(action="SETTLE"), g)[0] == 0.0
    assert checks.check_citations(out(), g)[0] == 1.0
    assert "don't exist" in checks.check_citations(out(cited_evidence_ids=[99]), g)[1]
    assert "doesn't cite" in checks.check_citations(out(cited_types=["order"], note_cited_types=[]), g)[1]
    assert checks.check_citations(out(action="SETTLE", cited_types=[], note_cited_types=[]), g)[0] == 1.0  # only for expected
    assert checks.check_request(out(), g)[0] == 1.0
    assert "isn't allowed" in checks.check_request(out(request={"endpoint": "escalate", "attachments": 0}), g)[1]
    assert "without the PDF" in checks.check_request(out(request={"endpoint": "provide-evidence", "attachments": 0}), g)[1]
    assert checks.check_request(out(action="GATHER_MORE"), g)[0] == 1.0
    assert checks.check_request(out(resolution_needs_human=True), g)[0] == 0.0
    partial = gold("grid-inr-partial-above-chargeback")
    assert checks.check_claims(out(reasoning="It was delivered.", note=""), partial)[0] == 0.0
    assert checks.check_claims(out(reasoning="Nothing shows it was delivered.", note=""), partial)[0] == 1.0
    assert false_claims("fine", []) == {}


# ---- rate limits, cache, pipeline (fake LLM) -------------------------------------------------------------------

@pytest.mark.parametrize("err,wait", [
    ("groq: HTTP 429 retry-after=1s: {...}", 1.0),
    ("groq: HTTP 429: Please try again in 570ms", 0.57),
    ("groq: HTTP 429: Rate limit reached on tokens per day (TPD)", float("inf")),
    ("groq: HTTP 429", 20.0),
    ("groq: HTTP 500", None),
])
def test_retry_after(err, wait):
    assert Limiter.retry_after(err) == wait


async def test_limiter_retries_429_then_gives_up_on_daily_limits(monkeypatch):
    calls = []

    async def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise LLMError("groq: HTTP 429 retry-after=0s")
        return "ok"
    monkeypatch.setattr(llm_module, "structured_call", flaky)
    lim = Limiter(interval=0, max_wait=5)
    assert await lim.call(system="s", user="u", output_model=None) == "ok" and len(calls) == 2

    async def daily(**kw):
        raise LLMError("groq: HTTP 429: tokens per day (TPD) limit")
    monkeypatch.setattr(llm_module, "structured_call", daily)
    with pytest.raises(RateLimited):
        await lim.call(system="s", user="u", output_model=None)


async def test_run_golden_with_fake_llm_caches_by_prompt_and_model(tmp_path):
    g = BY_ID["grid-inr-strong-above-chargeback"]
    cache = DiskCache("pipeline", root=tmp_path)
    first = await run_golden(g, Limiter(interval=0), cache)
    assert first["cached"] is False and first["action"] == "FIGHT" and first["request"]["endpoint"] == "provide-evidence"
    assert first["note"] and first["attempts"] == 1 and {c["output"] for c in first["llm_calls"]} == {
        "Recommendation", "SellerNote"}
    again = await run_golden(g, Limiter(interval=0), cache)
    assert again["cached"] is True and again["cache_key"] == first["cache_key"]
    engine, did = load_into_db(g)
    assert first["cache_key"].startswith(f"{g.id}-{prompt_hash(g, engine, did)}-")
    other = BY_ID["grid-inr-weak-above-chargeback"]
    e2, d2 = load_into_db(other)
    assert prompt_hash(other, e2, d2) != prompt_hash(g, engine, did)


async def test_provider_failures_are_reported_not_cached(tmp_path, monkeypatch):
    async def down(**kw):
        raise LLMError("groq: HTTP 503 Service Unavailable")
    monkeypatch.setattr(llm_module, "structured_call", down)
    cache = DiskCache("pipeline", root=tmp_path)
    r = await run_golden(BY_ID["grid-inr-strong-above-chargeback"], Limiter(interval=0), cache)
    assert r["error"] == "provider_error" and not list((tmp_path / "pipeline").glob("*.json"))


async def test_run_all_isolates_failures_and_caps_concurrency(tmp_path, monkeypatch):
    live, peak = 0, 0
    real = llm_module.structured_call

    async def counting(**kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        if "PP-EVAL-GRID-INR-WEAK" in kw["user"]:
            raise LLMError("groq: HTTP 429: requests per day (RPD)")
        return await real(**kw)
    monkeypatch.setattr(llm_module, "structured_call", counting)
    picked = [g for g in GOLDENS if g.id.startswith("grid-inr")]
    res = await run_all(picked, cache=DiskCache("pipeline", root=tmp_path), limiter=Limiter(interval=0),
                        progress=lambda *_: None)
    assert len(res) == len(picked) and peak <= 2
    assert {r["error"] for k, r in res.items() if "weak" in k} == {"rate_limited"}
    assert all("action" in r for k, r in res.items() if "weak" not in k)

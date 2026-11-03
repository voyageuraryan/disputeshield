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
    from evals.goldens.schema import decision_files

    on_disk = {p.stem: json.loads(p.read_text()) for p in decision_files()}
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


def test_checks_table_covers_the_10b_metrics():
    assert set(checks.DETERMINISTIC) == {"ActionCorrectness", "CitationValidity", "AllowedActionRespected",
                                         "BaselineDistance", "NoOverclaim", "LengthLimits"}
    assert "ActionCorrectness" not in checks.STRICT  # gated on its own (>= 90%), see evals/gates.py
    # known good / bad per check: tests/test_eval_metrics.py
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


def test_plain_breaks_cycles_and_bounds_size():
    from evals.pipeline import MAX_STR, plain

    a = {"name": "span"}
    a["self"] = a  # live objects in span inputs reference each other
    obj = type("Investigator", (), {})()
    out = plain({"a": a, "obj": obj, "long": "x" * (MAX_STR + 50), "list": [1, (2, 3)]})
    assert out["a"]["self"] == "<…>" and out["obj"].startswith("<") and len(out["long"]) == MAX_STR + 1
    assert out["list"] == [1, [2, 3]]
    json.dumps(out)


async def test_replay_answers_in_order_and_refuses_to_drift():
    from app.schemas import Recommendation, SellerNote
    from evals.pipeline import Replay

    rec = {"action": "FIGHT", "confidence": "high", "reasoning": "r", "cited_evidence_ids": [1], "missing_items": [],
           "economic_note": "e"}
    r = Replay([{"output": "Recommendation", "result": rec}])
    assert (await r(system="s", user="u", output_model=Recommendation)).action == "FIGHT" and r.prompts == ["u"]
    r2 = Replay([{"output": "Recommendation", "result": rec}])
    with pytest.raises(RuntimeError, match="out of step"):
        await r2(system="s", user="u", output_model=SellerNote)


async def test_without_tracing_want_trace_never_replays(tmp_path):
    g = BY_ID["grid-inr-strong-above-chargeback"]
    cache = DiskCache("pipeline", root=tmp_path)
    await run_golden(g, Limiter(interval=0), cache)
    again = await run_golden(g, Limiter(interval=0), cache, want_trace=True)
    assert again["cached"] is True  # served from cache, no replay loop


def test_merge_combines_chunked_runs(tmp_path, monkeypatch):
    from evals import report

    monkeypatch.setattr(report, "REPORTS", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({"rows": [
        {"id": "grid-inr-strong-above-chargeback", "action": "SETTLE"}, {"id": "gone-golden", "action": "FIGHT"},
        {"id": "adv-prompt-injection", "action": "FIGHT"}]}))
    rows = report.merged_rows([{"id": "grid-inr-strong-above-chargeback", "action": "FIGHT"}])
    assert {r["id"]: r["action"] for r in rows} == {"grid-inr-strong-above-chargeback": "FIGHT",
                                                    "adv-prompt-injection": "FIGHT"}


def test_judge_stops_retrying_a_daily_quota(tmp_path, monkeypatch):
    import httpx
    import respx

    from app.agents.llm import GEMINI_URL
    from evals.judge import GeminiJudge, JudgeError

    with respx.mock:
        route = respx.post(GEMINI_URL.format(model="m1")).mock(return_value=httpx.Response(
            429, json={"error": {"message": "You exceeded your current quota, please check your plan"}}))
        j = GeminiJudge(models=["m1"], cache=DiskCache("j", root=tmp_path), interval=0)
        for _ in range(2):
            with pytest.raises(JudgeError, match="daily quota"):
                j.generate("prompt one" + str(_))
        assert route.call_count == 1 and j.exhausted == {"m1"}  # no retries, and skipped afterwards

"""Run one golden through the REAL analyst + resolution with the REAL model under test (no PayPal calls).

golden -> rows in a private in-memory SQLite DB (order, shipments, dispute with the stored PayPal JSON, evidence
items with the golden's ids) -> build_case_file (checked against the golden) -> analyze() -> resolve() for
FIGHT/SETTLE (seller note + PDF + the exact PayPal request; never sent: nothing here executes).

Rate limits (Groq free tier: 30 RPM, 1K requests/day, 8K tokens/min, 200K tokens/day; PROGRESS session 8):
one global limiter spaces model calls (EVALS_MIN_INTERVAL_S, default 15 s ~ 4 calls/min at ~2K tokens each),
at most 2 goldens run at once, HTTP 429 waits for the server's retry-after (up to EVALS_MAX_RETRY_WAIT_S), and a
daily-limit 429 stops that golden as "rate_limited" (not cached, so the next run picks it up).
Outputs are cached by (golden id, prompt hash, model) in evals/.cache/pipeline/.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from decimal import Decimal
from typing import Any

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.agents import llm as llm_module
from app.agents.analyst import SYSTEM_PROMPT, analyze, build_prompt
from app.agents.investigator import build_case_file
from app.agents.llm import LLMError
from app.agents.redact import pii_terms, redact
from app.agents.resolution import NOTE_SYSTEM_PROMPT, resolve
from app.config import get_settings
from app.db import create_db_and_tables
from app.models import (AgentStep, Decision, Dispute, EvidenceItemRow, EvidenceSource, MerchantSettings, Order,
                        Shipment)
from app.rules.evidence_rules import defect_confirmed, economic_baseline, evaluate
from app.schemas import SellerNote
from app.tracing import observe, update_trace
from evals.cache import DiskCache, digest
from evals.goldens.schema import Golden

log = logging.getLogger("evals")
THRESHOLD = Decimal("25")
CONCURRENCY = 2


# llm.py messages that describe the model's own output (counted as model behaviour, not provider failure)
MODEL_FAULTS = ("did not match", "empty response", "output truncated")


class RateLimited(Exception):
    """The model under test refused for the day (or longer than we wait): skip, don't cache."""


def model_under_test() -> str:
    """Part of the cache key: sampling settings change answers just like the model does."""
    s = get_settings()
    return f"{s.llm_providers}:{s.groq_model}:t{s.llm_temperature}:s{s.llm_seed}"


class Limiter:
    """Global spacing between model calls + 429 handling (shared by every golden in the run)."""

    def __init__(self, interval: float | None = None, max_wait: float | None = None, attempts: int = 5):
        self.interval = float(os.getenv("EVALS_MIN_INTERVAL_S", "15")) if interval is None else interval
        self.max_wait = float(os.getenv("EVALS_MAX_RETRY_WAIT_S", "120")) if max_wait is None else max_wait
        self.attempts = attempts
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _slot(self) -> None:
        async with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    @staticmethod
    def retry_after(err: str) -> float | None:
        if "429" not in err:
            return None
        if re.search(r"per day|TPD|RPD", err, re.IGNORECASE):
            return float("inf")
        m = re.search(r"retry-after=([\d.]+)", err) or re.search(r"try again in ([\d.]+)(m?s)", err)
        if not m:
            return 20.0
        value = float(m.group(1))
        return value / 1000 if len(m.groups()) > 1 and m.group(2) == "ms" else value

    async def call(self, **kw: Any) -> Any:
        for attempt in range(self.attempts):
            await self._slot()
            try:
                return await llm_module.structured_call(**kw)
            except LLMError as exc:
                wait = self.retry_after(str(exc))
                if wait is None or attempt == self.attempts - 1:
                    raise
                if wait > self.max_wait:
                    raise RateLimited(str(exc)[:300]) from exc
                log.info("model under test rate-limited; waiting %.1fs", wait + 1)
                await asyncio.sleep(wait + 1)
        raise AssertionError("unreachable")


class Recorder:
    """Per-golden LLM wrapper: goes through the shared limiter and remembers each call (result, prompt, tokens)."""

    def __init__(self, limiter: Limiter):
        self.limiter = limiter
        self.calls: list[dict] = []
        self.prompts: list[str] = []          # what the model under test actually received (PII check)
        self.provider_errors: list[str] = []  # the provider failed (not the model's answer): never cached

    async def __call__(self, *, system: str, user: str, output_model: type) -> Any:
        started = time.monotonic()
        self.prompts.append(user)
        try:
            result = await self.limiter.call(system=system, user=user, output_model=output_model)
        except LLMError as exc:
            if not any(k in str(exc) for k in MODEL_FAULTS):
                self.provider_errors.append(str(exc)[:300])  # HTTP/timeouts; bad output *is* model behaviour
            raise
        self.calls.append({"output": output_model.__name__, "provider": llm_module.last_provider.get(),
                           "seconds": round(time.monotonic() - started, 1), "tokens": llm_module.last_usage.get(),
                           "result": result.model_dump(mode="json")})
        return result


class Replay:
    """Answers with a cached run's recorded results, in order: rebuilds a trace with zero model calls."""

    def __init__(self, calls: list[dict]):
        self.calls, self.i, self.prompts = calls, 0, []

    async def __call__(self, *, system: str, user: str, output_model: type) -> Any:
        self.prompts.append(user)
        call = self.calls[self.i]
        self.i += 1
        if call["output"] != output_model.__name__:
            raise RuntimeError(f"replay out of step: recorded {call['output']}, asked for {output_model.__name__}")
        return output_model.model_validate(call["result"])


def load_into_db(g: Golden):
    """Fresh in-memory DB holding exactly this golden. Returns (engine, dispute id)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(engine)
    case = g.case_file
    with Session(engine) as s:
        s.add(MerchantSettings(id=1, settle_threshold=THRESHOLD))
        if case.order_id is not None and g.order is not None:
            s.add(Order(id=case.order_id, buyer_email=g.order.buyer_email, items=g.order.items, amount=case.amount,
                        currency=case.currency, shipping_address=g.order.shipping_address, invoice_id=g.order.invoice_id))
            for sh in g.shipments:
                s.add(Shipment(order_id=case.order_id, carrier=sh.carrier, tracking_number=sh.tracking_number,
                               status=sh.status))
        s.flush()
        d = Dispute(paypal_dispute_id=case.dispute_id, order_id=case.order_id if g.order else None, reason=case.reason,
                    status=case.status or "WAITING_FOR_SELLER_RESPONSE", stage=case.stage, amount=case.amount,
                    currency=case.currency, raw=g.paypal_raw)
        s.add(d)
        s.flush()
        for e in case.evidence:
            s.add(EvidenceItemRow(id=e.id, dispute_id=d.id, type=e.type, source=EvidenceSource(e.source),
                                  source_ref=e.source_ref, summary=e.summary, flags=e.flags))
        s.commit()
        built = build_case_file(s, d)
        mismatch = []
        if [x.model_dump() for x in built.evidence] != [x.model_dump() for x in case.evidence]:
            mismatch.append("evidence")
        if sorted(built.allowed_actions) != sorted(case.allowed_actions):
            mismatch.append(f"allowed_actions {built.allowed_actions} != {case.allowed_actions}")
        if mismatch:
            raise ValueError(f"{g.id}: the DB rebuilds a different case file ({'; '.join(mismatch)})")
        return engine, d.id


def prompt_hash(g: Golden, engine, dispute_id: int) -> str:
    """Hash of everything the model under test sees for the analysis (system + redacted user prompt + note system)."""
    with Session(engine) as s:
        case = build_case_file(s, s.get(Dispute, dispute_id))
        checklist = evaluate(case)
        base = economic_baseline(checklist, case.amount, THRESHOLD, can_fight=case.can_fight, can_settle=case.can_settle, defect=defect_confirmed(case))
        user = redact(build_prompt(case, checklist, base, THRESHOLD), pii_terms(s, case.order_id))
    return digest(SYSTEM_PROMPT, user, NOTE_SYSTEM_PROMPT)[:16]


# ---- traces (DEEPEVAL_TRACING=1, set by the eval conftest) ------------------------------------------------------

_traces: dict[str, Any] = {}


def install_trace_capture() -> bool:
    """Keep finished traces in memory by name (nothing is posted anywhere). False when tracing is off."""
    from app import tracing

    if not tracing.ENABLED:
        return False
    from deepeval.tracing import trace_manager

    def keep(trace) -> None:  # noqa: ANN001
        name = getattr(trace, "name", None)
        if name:
            _traces[name] = trace
    trace_manager.post_trace = keep
    trace_manager.post_trace_api = lambda *a, **k: None
    return True


MAX_STR = 2000


def plain(obj: Any, _seen: set | None = None, _depth: int = 0) -> Any:
    """A trace as plain JSON: span inputs hold live objects (sessions, the investigator) that reference each other,
    which json.dumps rejects as circular. Cycles and very deep values become a marker; long strings are cut."""
    seen = _seen if _seen is not None else set()
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_STR else obj[:MAX_STR] + "…"
    if _depth > 25 or id(obj) in seen:
        return "<…>"
    if isinstance(obj, dict):
        seen.add(id(obj))
        out = {str(k): plain(v, seen, _depth + 1) for k, v in obj.items()}
        seen.discard(id(obj))
        return out
    if isinstance(obj, (list, tuple, set)):
        seen.add(id(obj))
        out = [plain(v, seen, _depth + 1) for v in obj]
        seen.discard(id(obj))
        return out
    if hasattr(obj, "model_dump"):
        try:
            return plain(obj.model_dump(mode="json"), seen, _depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return plain(repr(obj), seen, _depth + 1)


async def _take_trace(name: str) -> dict | None:
    from app import tracing

    if not tracing.ENABLED:
        return None
    from deepeval.tracing import trace_manager

    for _ in range(50):  # the trace is handed over when its root span closes
        if name in _traces:
            trace = _traces.pop(name)
            roots = trace.root_spans or []
            return plain(trace_manager.create_nested_spans_dict(roots[0])) if roots else None
        await asyncio.sleep(0.02)
    return None


async def shadow_investigation(g: Golden):  # -> FakePayPal (its .requests are the PayPal reads made)
    """The real investigator on the golden's store data + a fake PayPal client (evals/fake_paypal.py), in its own
    DB. Only its tool calls are scored (the analyst decides on the golden's fixed case file)."""
    from app.agents.investigator import investigate
    from app.models import Order as OrderRow
    from evals.fake_paypal import FakePayPal, capture_id, paypal_order_id

    engine, did = load_into_db(g)
    with Session(engine) as s:
        d = s.get(Dispute, did)
        d.transaction_id, d.refreshed_at = capture_id(g), None  # stale copy: the investigator fetches it
        if d.order_id and (oid := paypal_order_id(g)):
            o = s.get(OrderRow, d.order_id)
            o.paypal_order_id = oid
            s.add(o)
        s.add(d)
        s.commit()
        fake = FakePayPal(g)
        await investigate(s, d, client=fake)
    return fake


@observe(type="agent", name="investigation")
async def _traced_investigation(g: Golden) -> None:
    await shadow_investigation(g)
    update_trace(name=f"investigation {g.id}", tags=["evals", "investigation", g.id])


async def tools_called(g: Golden) -> list[str] | None:
    """Tool span names of a shadow investigation (None when tracing is off)."""
    from evals.metrics.agentic import tool_spans

    await _traced_investigation(g)
    trace = await _take_trace(f"investigation {g.id}")
    return tool_spans(trace) if trace is not None else None


# ---- one golden -----------------------------------------------------------------------------------------------------

async def run_golden(g: Golden, limiter: Limiter, cache: DiskCache | None = None, *, want_trace: bool = False) -> dict:
    """Cached outputs are reused. want_trace: the full-run trace is needed (agentic metrics); a cached run is replayed
    through the real code (no model calls) when its results were recorded, else it runs for real."""
    from app import tracing

    want_trace = want_trace and tracing.ENABLED  # without tracing no trace can appear: don't replay for one
    engine, did = load_into_db(g)
    key = f"{g.id}-{prompt_hash(g, engine, did)}-{digest(model_under_test())[:8]}"
    hit = cache.get(key) if cache else None
    if hit and not (want_trace and not hit.get("trace")):
        return {**hit, "cached": True}
    replay = hit and all("result" in c for c in hit.get("llm_calls") or [])
    llm: Any = Replay(hit["llm_calls"]) if replay else Recorder(limiter)
    out = await _traced_run(g, engine, did, key, llm)
    out["trace"] = await _take_trace(f"golden {g.id}")
    if replay:  # decisions come from the original run; the replay only adds the trace
        out = {**hit, "trace": out["trace"], "prompts": hit.get("prompts") or llm.prompts}
    elif out.get("error"):
        return out
    if cache:
        cache.set(key, {k: v for k, v in out.items() if k != "cached"})
    return {**out, "cached": bool(replay)}


@observe(type="agent", name="dispute run")
async def _traced_run(g: Golden, engine, did: int, key: str, llm: Any) -> dict:  # noqa: ANN001
    """One golden = one trace when DEEPEVAL_TRACING=1: shadow investigation, analysis, resolution, LLM calls."""
    from app import tracing

    if tracing.ENABLED:
        await shadow_investigation(g)
    out = await _run(g, engine, did, key, llm)
    from evals.metrics.agentic import TASK

    update_trace(name=f"golden {g.id}", input=f"TASK: {TASK}\nCASE: {g.case_file.model_dump_json()}",
                 output=f"{out.get('action')}: {out.get('reasoning', '')}", tags=["evals", g.id])
    return out


async def _run(g: Golden, engine, did: int, key: str, llm: Any) -> dict:  # noqa: ANN001
    started = time.monotonic()
    with Session(engine) as s:
        d = s.get(Dispute, did)
        case = build_case_file(s, d)
        r = await analyze(s, d, case, llm=llm)
        out: dict[str, Any] = {
            "golden_id": g.id, "model": model_under_test(), "cache_key": key,
            "rule_score": r.checklist.rule_score, "red_flags": r.checklist.red_flags,
            "baseline": r.baseline.action if r.baseline else None, "baseline_why": r.baseline.why if r.baseline else None,
            "needs_human": r.needs_human, "problems": r.problems,
        }
        rec = r.recommendation
        types = {e.id: e.type for e in case.evidence}
        if rec:
            out.update(action=rec.action, confidence=rec.confidence, reasoning=rec.reasoning,
                       cited_evidence_ids=rec.cited_evidence_ids,
                       cited_types=sorted({types.get(i, "?") for i in rec.cited_evidence_ids}),
                       missing_items=rec.missing_items, economic_note=rec.economic_note)
        else:
            out.update(action="NEEDS_HUMAN", reasoning="", cited_evidence_ids=[], cited_types=[], missing_items=[])
        if r.decision_id and rec and rec.action in ("FIGHT", "SETTLE", "SETTLE_PARTIAL", "REPLACEMENT"):
            res = await resolve(s, d, case, r.decision_id, llm=llm)
            dec = s.get(Decision, r.decision_id)
            notes = [c["result"] for c in getattr(llm, "calls", []) if c["output"] == SellerNote.__name__]
            note_ids = notes[-1]["cited_evidence_ids"] if notes else []
            req = dec.action_request or {}
            gave_up = s.exec(select(AgentStep).where(AgentStep.dispute_id == did, AgentStep.agent == "resolution",
                                                     AgentStep.action.like("Needs human review%"))).first()
            out.update(resolution_needs_human=res["needs_human"], note=dec.draft_note or "",
                       resolution_problems=(gave_up.detail or {}).get("problems", []) if gave_up else [],
                       note_cited_ids=note_ids, note_cited_types=sorted({types.get(i, "?") for i in note_ids}),
                       request={"endpoint": req.get("endpoint"), "attachments": len(req.get("attachments") or []),
                                "evidence_type": ((req.get("evidences") or [{}])[0]).get("evidence_type")})
        out["attempts"] = 1 + len(s.exec(select(AgentStep).where(AgentStep.dispute_id == did,
                                                                   AgentStep.action.like("Recommendation rejected%")))
                                  .all())
    if getattr(llm, "provider_errors", None):  # a transient provider failure: report, don't cache
        return {"golden_id": g.id, "error": "provider_error", "detail": " | ".join(llm.provider_errors)[:500]}
    calls = getattr(llm, "calls", [])
    out["llm_calls"] = calls  # results kept: a cached run can be replayed to rebuild its trace
    out["prompts"] = llm.prompts[:1]  # the analyst prompt (PII check); later ones repeat the same case data
    out["providers"] = sorted({c["provider"] for c in calls if c.get("provider")})
    out["tokens"] = {k: sum((c.get("tokens") or {}).get(k) or 0 for c in calls) for k in ("input", "output")}
    out["elapsed_s"] = round(time.monotonic() - started, 1)
    return out


async def run_all(goldens: list[Golden], *, cache: DiskCache | None = None, limiter: Limiter | None = None,
                  concurrency: int = CONCURRENCY, progress=print, want_trace: set[str] | None = None) -> dict[str, dict]:
    """Every golden through the pipeline (concurrency 2, shared limiter), plus its shadow investigation's tool calls."""
    limiter = limiter or Limiter()
    install_trace_capture()
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def one(g: Golden) -> None:
        async with sem:
            try:
                r = await run_golden(g, limiter, cache, want_trace=g.id in (want_trace or set()))
                if not r.get("error"):
                    r["tools_called"] = await tools_called(g)
                results[g.id] = r
            except RateLimited as exc:
                results[g.id] = {"golden_id": g.id, "error": "rate_limited", "detail": str(exc)}
            except Exception as exc:  # noqa: BLE001 - one broken golden must not stop the run
                log.exception("golden %s failed", g.id)
                results[g.id] = {"golden_id": g.id, "error": type(exc).__name__, "detail": str(exc)[:500]}
            r = results[g.id]
            progress(f"[evals] {len(results)}/{len(goldens)} {g.id}: "
                     + (r.get("error") or f"{r.get('action')} ({'cached' if r.get('cached') else r.get('elapsed_s')})"))

    await asyncio.gather(*(one(g) for g in goldens))
    return results

"""Collects per-golden metric results during the run and writes evals/reports/<date>.{json,md} + latest.{json,md}.

Report: gates, per-metric pass rate / average, per-reason breakdown, confusion matrix (expected vs recommended),
the 5 worst cases with judge reasons, model under test + providers + judge, latency p50/p95, token use.
latest.json is what GET /api/admin/evals/latest serves (Trust page, Phase 16).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import quantiles

from evals import gates
from evals.goldens.schema import Golden, load_goldens

REPORTS = Path(__file__).parent / "reports"
ROWS: list[dict] = []
ACTIONS = ("FIGHT", "SETTLE", "GATHER_MORE", "NEEDS_HUMAN")
REASON_SHORT = {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Not received", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
                "Not as described", "UNAUTHORISED": "Unauthorised"}


def selected_goldens() -> list[Golden]:
    goldens = load_goldens()
    only = [x.strip() for x in os.getenv("EVALS_ONLY", "").split(",") if x.strip()]
    return [g for g in goldens if any(g.id == o or g.id.startswith(o) for o in only)] if only else goldens


# The costly judged metrics (PII leakage on the prompt, StepEfficiency, TaskCompletion on the traced run) run on a
# sample by default (free-tier judge quota): each reason strong/weak, the injection case, safety, a real dispute.
DEEP_SAMPLE = ("grid-inr-strong-above-chargeback", "grid-inr-weak-above-chargeback", "grid-snad-strong-above-inquiry",
               "grid-snad-weak-above-chargeback", "grid-unauth-strong-above-chargeback", "adv-prompt-injection",
               "ts-safety", "rec-zwl-chargeback")


def deep_goldens() -> list[Golden]:
    want = os.getenv("EVALS_DEEP", "")
    sel = selected_goldens()
    if want.strip() == "all":
        return sel
    ids = [x.strip() for x in want.split(",") if x.strip()] or list(DEEP_SAMPLE)
    return [g for g in sel if any(g.id == i or g.id.startswith(i) for i in ids)]


def metric_row(m, kind: str) -> dict:
    return {"name": m.__name__, "kind": kind, "score": m.score,
            "success": bool(m.success) if m.score is not None else None,
            "reason": m.reason, "error": getattr(m, "error", None)}


def record(golden: Golden, out: dict, metrics: list[dict]) -> None:
    ROWS.append({
        "id": golden.id, "reason": golden.case_file.reason, "expected": golden.expected_action,
        "acceptable": golden.acceptable_actions, "action": out.get("action"), "error": out.get("error"),
        "error_detail": out.get("detail"), "cached": out.get("cached"), "model": out.get("model"),
        "providers": out.get("providers"), "attempts": out.get("attempts"), "elapsed_s": out.get("elapsed_s"),
        "tokens": out.get("tokens"), "metrics": metrics,
    })


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "-"


def build(rows: list[dict], judge_model: str | None) -> dict:
    ran = [r for r in rows if not r.get("error")]
    stats = gates.summarize(rows)
    by_reason = {}
    for reason, short in REASON_SHORT.items():
        rr = [r for r in ran if r["reason"] == reason]
        by_reason[short] = {"goldens": len(rr), "action_correct": sum(r["action"] in r["acceptable"] for r in rr),
                            "metrics_failed": sum(1 for r in rr for m in r["metrics"] if m["success"] is False)}
    confusion = {e: {a: 0 for a in ACTIONS} for e in ACTIONS[:3]}
    for r in ran:
        confusion[r["expected"]][r["action"] if r["action"] in ACTIONS else "NEEDS_HUMAN"] += 1

    def badness(r: dict) -> tuple:
        scores = [m["score"] for m in r["metrics"] if m["score"] is not None]
        return (sum(m["success"] is False for m in r["metrics"]), -(sum(scores) / len(scores)) if scores else 0)
    worst = [{"id": r["id"], "expected": "/".join(r["acceptable"]), "got": r["action"],
              "failed": [{"metric": m["name"], "score": m["score"], "reason": (m["reason"] or "")[:400]}
                         for m in r["metrics"] if m["success"] is False]}
             for r in sorted(ran, key=badness, reverse=True)[:5] if badness(r)[0]]
    lat = sorted(r["elapsed_s"] for r in ran if r.get("elapsed_s") is not None)
    p50, p95 = (quantiles(lat, n=100)[49], quantiles(lat, n=100)[94]) if len(lat) >= 2 else ((lat or [None])[0],) * 2
    tok = [r["tokens"] for r in ran if r.get("tokens")]
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_under_test": sorted({r["model"] for r in ran if r.get("model")}),
        "providers": sorted({p for r in ran for p in r.get("providers") or []}), "judge": judge_model,
        "goldens": len(rows), "ran": len(ran), "skipped": {r["id"]: r["error"] for r in rows if r.get("error")},
        "gates": gates.check(rows), "metrics": stats, "by_reason": by_reason, "confusion": confusion, "worst": worst,
        "latency_s": {"p50": p50, "p95": p95, "note": "pipeline time per golden incl. rate-limit spacing"},
        "tokens": {"input": sum(t.get("input") or 0 for t in tok), "output": sum(t.get("output") or 0 for t in tok),
                   "goldens_with_usage": len(tok)},
        "rows": rows,
    }


def markdown(rep: dict) -> str:
    g = rep["gates"]
    status = "PASSED" if g["passed"] else ("PARTIAL / FAILED" if g["partial"] else "FAILED")
    md = [f"# Eval report {rep['generated'][:10]}", "",
          f"**Gates: {status}**" + ("" if g["passed"] else "\n\n" + "\n".join(f"- {f}" for f in g["failures"][:20])), "",
          f"Model under test: {', '.join(rep['model_under_test']) or '-'} (providers: {', '.join(rep['providers']) or '-'}); "
          f"judge: {rep['judge'] or 'off'}. Goldens {rep['goldens']}, ran {rep['ran']}"
          + (f", skipped {len(rep['skipped'])}" if rep["skipped"] else "") + ".",
          f"Latency per golden p50 {rep['latency_s']['p50']} s, p95 {rep['latency_s']['p95']} s. Tokens (model under "
          f"test): {rep['tokens']['input']} in / {rep['tokens']['output']} out over {rep['tokens']['goldens_with_usage']} "
          "golden(s) with usage data.", "",
          "| Metric | Kind | Passed | Average | Not measured |", "|---|---|---|---|---|"]
    for name, s in rep["metrics"].items():
        md.append(f"| {name} | {s['kind']} | {_pct(s['passed'], s['measured'])} | {s['average']} | {s['errored']} |")
    md += ["", "| Reason | Goldens | Action correct | Failed metric results |", "|---|---|---|---|"]
    md += [f"| {k} | {v['goldens']} | {_pct(v['action_correct'], v['goldens'])} | {v['metrics_failed']} |"
           for k, v in rep["by_reason"].items()]
    md += ["", "Confusion matrix (rows: expected, columns: recommended)", "",
           "| expected \\ got | " + " | ".join(ACTIONS) + " |", "|---" * (len(ACTIONS) + 1) + "|"]
    md += [f"| {e} | " + " | ".join(str(c[a]) for a in ACTIONS) + " |" for e, c in rep["confusion"].items()]
    md += ["", "## Worst cases", ""]
    for w in rep["worst"] or []:
        md.append(f"- **{w['id']}** (expected {w['expected']}, got {w['got']})")
        md += [f"  - {f['metric']} ({f['score']}): {f['reason']}" for f in w["failed"]]
    if not rep["worst"]:
        md.append("None failed.")
    return "\n".join(md) + "\n"


def merged_rows(rows: list[dict]) -> list[dict]:
    """--merge: this run's rows replace the same goldens' rows in the existing latest.json (chunked runs add up to
    one report; goldens that no longer exist are dropped)."""
    try:
        old = json.loads((REPORTS / "latest.json").read_text()).get("rows") or []
    except (OSError, ValueError):
        old = []
    current = {g.id for g in load_goldens()}
    by_id = {r["id"]: r for r in old if r.get("id") in current}
    by_id.update({r["id"]: r for r in rows})
    return list(by_id.values())


def write_report(judge_model: str | None = None) -> dict | None:
    if not ROWS:
        return None
    REPORTS.mkdir(exist_ok=True)
    rows = merged_rows(ROWS) if os.getenv("EVALS_MERGE") == "1" else ROWS
    rep = build(sorted(rows, key=lambda r: r["id"]), judge_model)
    day = rep["generated"][:10]
    for stem in (day, "latest"):
        (REPORTS / f"{stem}.json").write_text(json.dumps(rep, indent=1, default=str))
        (REPORTS / f"{stem}.md").write_text(markdown(rep))
    return rep

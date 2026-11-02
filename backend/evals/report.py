"""Collects per-golden metric results during the run and writes evals/reports/latest.{json,md}."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evals.goldens.schema import Golden, load_goldens

REPORTS = Path(__file__).parent / "reports"
ROWS: list[dict] = []


def selected_goldens() -> list[Golden]:
    goldens = load_goldens()
    only = [x.strip() for x in os.getenv("EVALS_ONLY", "").split(",") if x.strip()]
    return [g for g in goldens if any(g.id == o or g.id.startswith(o) for o in only)] if only else goldens


def record(golden: Golden, out: dict, metrics: list) -> None:
    ROWS.append({
        "id": golden.id, "expected": golden.expected_action, "acceptable": golden.acceptable_actions,
        "action": out.get("action"), "error": out.get("error"), "cached": out.get("cached"),
        "providers": out.get("providers"), "attempts": out.get("attempts"), "elapsed_s": out.get("elapsed_s"),
        "metrics": [{"name": m.__name__, "score": m.score, "success": bool(m.success) if m.score is not None else None,
                     "reason": m.reason, "error": getattr(m, "error", None)} for m in metrics],
    })


def write_report() -> None:
    if not ROWS:
        return
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = sorted(ROWS, key=lambda r: r["id"])
    totals: dict[str, Counter] = {}
    for r in rows:
        for m in r["metrics"]:
            c = totals.setdefault(m["name"], Counter())
            c["run"] += m["success"] is not None
            c["pass"] += bool(m["success"])
    ran = [r for r in rows if not r["error"]]
    summary = {"generated": stamp, "goldens": len(rows), "ran": len(ran),
               "errors": dict(Counter(r["error"] for r in rows if r["error"])),
               "action_accuracy": f"{sum(r['action'] in r['acceptable'] for r in ran)}/{len(ran)}",
               "metrics": {k: f"{v['pass']}/{v['run']}" for k, v in totals.items()}}
    (REPORTS / "latest.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, default=str))
    lines = [f"# Eval report ({stamp})", "", f"Goldens: {len(rows)}, ran: {len(ran)}"
             + (f", not run: {summary['errors']}" if summary["errors"] else ""),
             f"Action accuracy: {summary['action_accuracy']}", "",
             "| Metric | Passed |", "|---|---|"] + [f"| {k} | {v} |" for k, v in summary["metrics"].items()] + [
             "", "| Golden | Expected | Got | Failed metrics |", "|---|---|---|---|"]
    for r in rows:
        failed = [f"{m['name']}: {(m['reason'] or m['error'] or '')[:140]}" for m in r["metrics"] if m["success"] is False]
        lines.append(f"| {r['id']} | {'/'.join(r['acceptable'])} | {r['error'] or r['action']} | "
                     f"{'<br>'.join(failed).replace('|', '/') or '-'} |")
    (REPORTS / "latest.md").write_text("\n".join(lines) + "\n")

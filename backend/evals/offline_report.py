"""Re-score cached pipeline outputs with the deterministic checks: no model, judge or deepeval calls.

    python -m evals.offline_report [title]      (from backend/; writes evals/reports/offline.md)

Use it after changing a check or a golden: goldens whose prompt changed since their cached run are listed as
"needs a fresh run" (their cache key no longer matches).
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from evals import checks
from evals.cache import ROOT
from evals.goldens.schema import load_goldens
from evals.pipeline import load_into_db, prompt_hash

CHECKS = [("Action", checks.check_action), ("Citations", checks.check_citations),
          ("No false claims", checks.check_claims), ("PayPal request", checks.check_request)]


def main(title: str = "Offline re-score of cached outputs") -> str:
    cached = {}
    for p in (ROOT / "pipeline").glob("*.json"):
        out = json.loads(p.read_text())
        cached.setdefault(out["golden_id"], []).append(out)
    totals, lines = Counter(), []
    for g in load_goldens():
        engine, did = load_into_db(g)
        current = [o for o in cached.get(g.id, []) if o["cache_key"].startswith(f"{g.id}-{prompt_hash(g, engine, did)}-")]
        if not current:
            lines.append(f"| {g.id} | {'/'.join(g.acceptable_actions)} | (needs a fresh run) | - |")
            continue
        out, gold = current[-1], g.model_dump(mode="json")
        fails = []
        for name, fn in CHECKS:
            score, why = fn(out, gold)
            totals[name, "run"] += 1
            totals[name, "pass"] += score == 1.0
            if score < 1:
                fails.append(f"{name}: {why[:150]}")
        lines.append(f"| {g.id} | {'/'.join(g.acceptable_actions)} | {out.get('action')} | "
                     f"{'<br>'.join(fails).replace('|', '/') or '-'} |")
    md = [f"# {title}", "", "Deterministic checks only (no judge). Model under test: " + (
        next(iter(cached.values()))[0]["model"] if cached else "-"), "", "| Metric | Passed |", "|---|---|"]
    md += [f"| {n} | {totals[n, 'pass']}/{totals[n, 'run']} |" for n, _ in CHECKS]
    md += ["", "| Golden | Expected | Got | Failed checks |", "|---|---|---|---|"] + lines
    return "\n".join(md) + "\n"


if __name__ == "__main__":
    from evals.report import REPORTS

    text = main(*sys.argv[1:2])
    (REPORTS / "offline.md").write_text(text)
    print("\n".join(text.splitlines()[4:10]))

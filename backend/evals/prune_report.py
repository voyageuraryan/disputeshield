"""Remove conversation rows that could not run (fixture_missing) from evals/reports/latest.*, without any model call.

    cd backend && .venv/bin/python -m evals.prune_report

Why: a full `--merge` run adds the 13 simulated-buyer conversations, which need the recorded research fixture first
(`python -m evals.record_research`, G92). Until that exists they are not runnable, so they are listed as pending in the
report text instead of counting as "skipped" goldens. Every other row (and every error) is kept as measured.
"""

from __future__ import annotations

import json

from evals import report


def main() -> int:
    path = report.REPORTS / "latest.json"
    d = json.loads(path.read_text())
    pending = sorted(r["id"] for r in d["rows"] if r.get("error") == "fixture_missing")
    rows = [r for r in d["rows"] if r.get("error") != "fixture_missing"]
    judge = (d.get("judge") or "").split("(")[-1].rstrip(")") or None
    rep = report.build(sorted(rows, key=lambda r: r["id"]), judge)
    rep["generated"] = d.get("generated", rep["generated"])
    md = report.markdown(rep)
    if pending:
        md += ("\n\n## Not run yet\n\nSimulated-buyer conversations (need the recorded research fixture, G92): "
               + ", ".join(pending) + "\n")
    path.write_text(json.dumps(rep, indent=1, default=str))
    (report.REPORTS / "latest.md").write_text(md)
    print(f"removed {len(pending)} not-runnable conversation row(s); gates: "
          f"{'PASSED' if rep['gates']['passed'] else 'PARTIAL / FAILED'}")
    for f in rep["gates"]["failures"]:
        print(" -", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

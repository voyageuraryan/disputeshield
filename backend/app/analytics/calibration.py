"""Calibration (Phase 14): rule_score -> P(win), fitted per dispute reason from resolved, contested cases.

Method (simple, documented): bins = the default table's bands (rule_score 0-0.40, 0.40-0.55, 0.55-0.70, 0.70-0.85,
0.85-1). In each bin, P = (wins + PRIOR * default_p) / (cases + PRIOR) (binned mean shrunk toward the default table,
PRIOR = 2 pseudo-cases, so a bin with 1 case can't jump to 0 or 1). Then pool-adjacent-violators makes P
non-decreasing in rule_score (isotonic): a higher score never means a lower chance.

Used by the Phase 13 calculator only when the reason has >= MIN_OUTCOMES real contested outcomes with a rule score;
otherwise the default table. Demo history (is_demo) is never used for real decisions: it can be fitted for the chart.
"""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import Session, select

from app.analytics.kpis import fact
from app.models import DisputeOutcome
from app.rules.money import WIN_TABLE

MIN_OUTCOMES = 10
PRIOR = 2.0
BANDS = sorted(WIN_TABLE)  # ascending (lower bound, default p)


def _band(score: float) -> int:
    idx = 0
    for i, (lo, _) in enumerate(BANDS):
        if score >= float(lo):
            idx = i
    return idx


def pav(values: list[float], weights: list[float]) -> list[float]:
    """Pool adjacent violators: the non-decreasing sequence closest to `values` (weighted least squares)."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-12:
            v1, w1, n1 = blocks[i]
            v2, w2, n2 = blocks[i + 1]
            blocks[i:i + 2] = [[(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2]]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, _, n in blocks:
        out += [v] * n
    return out


def fit(points: list[tuple[float, bool]]) -> list[dict]:
    """[(rule_score, won)] -> per band {lower, upper, n, wins, raw, default, p}."""
    n = [0] * len(BANDS)
    w = [0] * len(BANDS)
    for score, won in points:
        b = _band(score)
        n[b] += 1
        w[b] += int(won)
    defaults = [float(p) for _, p in BANDS]
    smoothed = [(w[i] + PRIOR * defaults[i]) / (n[i] + PRIOR) for i in range(len(BANDS))]
    mono = pav(smoothed, [n[i] + PRIOR for i in range(len(BANDS))])
    uppers = [float(lo) for lo, _ in BANDS[1:]] + [1.0]
    return [{"lower": float(BANDS[i][0]), "upper": uppers[i], "n": n[i], "wins": w[i],
             "raw": round(w[i] / n[i], 4) if n[i] else None, "default": defaults[i], "p": round(mono[i], 4)}
            for i in range(len(BANDS))]


def table(bins: list[dict]) -> tuple[tuple[Decimal, Decimal], ...]:
    """Fitted bins -> the calculator's WIN_TABLE shape (highest bound first)."""
    return tuple((Decimal(str(b["lower"])), Decimal(str(b["p"]))) for b in sorted(bins, key=lambda b: -b["lower"]))


def points(rows: list[dict], reason: str | None = None) -> list[tuple[float, bool]]:
    return [(r["rule_score"], r["won"]) for r in rows
            if r["contested"] and r["rule_score"] is not None and (reason is None or r["reason"] == reason)]


def report(rows: list[dict]) -> list[dict]:
    """Per reason: n, whether it is used (>= MIN_OUTCOMES), the fitted bins (for the chart)."""
    out = []
    for reason in sorted({r["reason"] for r in rows}):
        pts = points(rows, reason)
        out.append({"reason": reason, "n": len(pts), "used": len(pts) >= MIN_OUTCOMES, "min_outcomes": MIN_OUTCOMES,
                    "bins": fit(pts)})
    return out


def win_table_for(session: Session, reason: str) -> tuple[tuple[tuple[Decimal, Decimal], ...] | None, int]:
    """(calibrated table, n) from REAL outcomes of this reason; (None, n) when there are fewer than MIN_OUTCOMES."""
    rows = [fact(o) for o in session.exec(select(DisputeOutcome).where(DisputeOutcome.is_demo == False,  # noqa: E712
                                                                        DisputeOutcome.reason == reason)).all()]
    pts = points(rows)
    return (table(fit(pts)) if len(pts) >= MIN_OUTCOMES else None), len(pts)

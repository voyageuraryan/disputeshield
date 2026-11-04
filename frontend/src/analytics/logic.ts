// Phase 14: pure helpers for the Analytics page (grouping, chips, chart options). Tested in analytics.test.ts.
import type { AgChartOptions } from "ag-charts-community";
import type { AskQuery, CalibrationBin, Fact, QFilter, QueryResult } from "../api";

// Validated with the dataviz validator (light surface): indigo = the app's accent, teal = the second series.
export const SERIES = { primary: "#4f46e5", secondary: "#0d9488", reference: "#94a3b8" };

export const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);

export const COLUMN_LABEL: Record<string, string> = {
  reason: "Reason", stage: "Stage", currency: "Currency", outcome: "Outcome", final_action: "Action sent",
  recommended_action: "Agent recommended", month: "Month", followed: "Followed agent", overridden: "Overridden",
  won: "Won", correct: "Agent right", before_deadline: "Before deadline", amount: "Amount", rule_score: "Rule score",
  hours_to_response: "Hours to response", money_protected: "Money protected", money_refunded: "Money refunded",
  offer_amount: "Offer", created_at: "Opened", resolved_at: "Resolved", response_at: "Responded", ref: "Case",
  contested: "Contested",
};
export const METRIC_LABEL: Record<string, string> = {
  count: "Cases", win_rate: "Win rate", money_protected: "Money protected", money_refunded: "Money refunded",
  accuracy: "Agent accuracy", median_hours_to_response: "Median hours to response",
  before_deadline_pct: "Answered before deadline", total_amount: "Total disputed", avg_amount: "Average amount",
  avg_rule_score: "Average rule score",
};
export const METRICS = Object.keys(METRIC_LABEL);
export const GROUPABLE = ["reason", "stage", "outcome", "final_action", "recommended_action", "month", "followed", "overridden", "won", "correct", "before_deadline"];
const OP_WORD: Record<string, string> = {
  eq: "is", ne: "is not", lt: "<", lte: "≤", gt: ">", gte: "≥", in: "is one of", contains: "contains",
  last_days: "in the last", before: "before", after: "after",
};
const PERCENT_METRICS = new Set(["win_rate", "accuracy", "before_deadline_pct"]);

/** The chip's fixed part: "Amount <", "Opened in the last" (the value is an editable input next to it). */
export const chipPrefix = (f: QFilter) => `${COLUMN_LABEL[f.column] ?? f.column} ${OP_WORD[f.op] ?? f.op}`;

/** "Amount < 25", "Opened in the last 30 days": what a filter chip says. */
export function chipText(f: QFilter): string {
  const col = COLUMN_LABEL[f.column] ?? f.column;
  const v = f.op === "last_days" ? `${f.value} days` : f.value.replaceAll("_", " ").toLowerCase();
  return `${col} ${OP_WORD[f.op] ?? f.op} ${v}`;
}

export const withoutFilter = (q: AskQuery, i: number): AskQuery => ({ ...q, filters: q.filters.filter((_, j) => j !== i) });
export const withFilterValue = (q: AskQuery, i: number, value: string): AskQuery => ({
  ...q,
  filters: q.filters.map((f, j) => (j === i ? { ...f, value } : f)),
});

export function formatMetric(metric: string, v: unknown, currency = "USD"): string {
  if (v == null) return "—";
  const n = Number(v);
  if (PERCENT_METRICS.has(metric)) return pct(n);
  if (metric.startsWith("money") || metric.endsWith("_amount"))
    return n.toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: 0 });
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

export type GroupRow = { key: string; cases: number; won: number; contested: number; win_rate: number | null; protected: number; refunded: number; accuracy: number | null };

/** The fact grid's "Group by" (Community AG Grid has no row grouping): aggregates computed here. */
export function groupFacts(facts: Fact[], by: "reason" | "month" | "outcome"): GroupRow[] {
  const m = new Map<string, Fact[]>();
  for (const f of facts) {
    const k = (by === "reason" ? f.reason_label : by === "month" ? f.month : f.outcome) ?? "—";
    m.set(k, [...(m.get(k) ?? []), f]);
  }
  return [...m.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, fs]) => {
      const c = fs.filter((f) => f.contested);
      const s = fs.filter((f) => f.correct !== null);
      const won = c.filter((f) => f.won).length;
      return {
        key,
        cases: fs.length,
        won,
        contested: c.length,
        win_rate: c.length ? won / c.length : null,
        protected: Math.round(fs.reduce((a, f) => a + f.money_protected, 0) * 100) / 100,
        refunded: Math.round(fs.reduce((a, f) => a + f.money_refunded, 0) * 100) / 100,
        accuracy: s.length ? s.filter((f) => f.correct).length / s.length : null,
      };
    });
}

/** Chart options for an "Ask" result: bar/line/pie over the first group column; null for a table or a single value. */
export function resultChart(r: QueryResult): AgChartOptions | null {
  if (r.kind !== "groups" || r.query.chart === "table" || r.query.group_by.length === 0) return null;
  const x = r.query.group_by[0];
  const y = r.query.metric;
  const data = r.rows.map((row) => ({ ...row, [x]: String(row[x] ?? "—"), [y]: row[y] == null ? null : Number(row[y]) }));
  const title = { text: r.query.title || `${METRIC_LABEL[y] ?? y} by ${(COLUMN_LABEL[x] ?? x).toLowerCase()}` };
  const fmt = (p: { value: number }) => formatMetric(y, p.value);
  if (r.query.chart === "pie")
    return { title, data, series: [{ type: "pie", angleKey: y, legendItemKey: x, calloutLabelKey: x, fills: [SERIES.primary, SERIES.secondary, "#6366f1", "#14b8a6", "#a5b4fc", "#5eead4"], strokes: ["#ffffff"], strokeWidth: 2 }] };
  return {
    title,
    data,
    series: [
      r.query.chart === "line"
        ? { type: "line", xKey: x, yKey: y, yName: METRIC_LABEL[y] ?? y, stroke: SERIES.primary, strokeWidth: 2, marker: { fill: SERIES.primary, size: 8 } }
        : { type: "bar", xKey: x, yKey: y, yName: METRIC_LABEL[y] ?? y, fill: SERIES.primary, cornerRadius: 4, label: { formatter: fmt, color: "#334155", placement: "outside-end" } },
    ],
    axes: {
      x: { type: "category", position: "bottom" },
      y: { type: "number", position: "left", label: { formatter: fmt }, gridLine: { style: [{ stroke: "#e2e8f0" }] } },
    },
  };
}

/** Calibration chart data: fitted P(win) vs the default table per rule-score band (midpoints), observed share as dots. */
export function calibrationData(bins: CalibrationBin[]) {
  return bins.map((b) => ({ score: Math.round(((b.lower + b.upper) / 2) * 100 + 1e-9) / 100, fitted: b.p, starting: b.default, observed: b.raw, n: b.n }));
}

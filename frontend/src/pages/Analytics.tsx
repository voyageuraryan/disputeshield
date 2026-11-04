// Phase 14: Analytics. KPIs, charts (AG Charts Community, MIT), the disputes fact table (AG Grid Community), "Ask your
// disputes" (LLM -> structured query, shown as editable chips; never SQL) and the calibration chart. Community
// editions only: no licence key and no watermark. Grouping is computed client-side (row grouping is Enterprise).
import { useEffect, useMemo, useState } from "react";
import {
  BarSeriesModule,
  CartesianChartModule,
  CategoryAxisModule,
  LegendModule,
  LineSeriesModule,
  ModuleRegistry as ChartsRegistry,
  NumberAxisModule,
  PieSeriesModule,
  PolarChartModule,
  ScatterSeriesModule,
  type AgCartesianChartOptions,
} from "ag-charts-community";
import { AgCharts } from "ag-charts-react";
import {
  CellStyleModule,
  ClientSideRowModelModule,
  ModuleRegistry,
  NumberFilterModule,
  TextFilterModule,
  ValidationModule,
  themeQuartz,
  type ColDef,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { analyticsApi, type AnalyticsSummary, type AskQuery, type Fact, type QueryResult } from "../api";
import {
  calibrationData,
  chipPrefix,
  chipText,
  COLUMN_LABEL,
  formatMetric,
  GROUPABLE,
  groupFacts,
  METRIC_LABEL,
  METRICS,
  pct,
  resultChart,
  SERIES,
  withFilterValue,
  withoutFilter,
  type GroupRow,
} from "../analytics/logic";
import { label } from "../format";

// Only the chart pieces this page uses (bar, line, scatter, pie; category + number axes; legend).
ChartsRegistry.registerModules([
  CartesianChartModule, PolarChartModule, BarSeriesModule, LineSeriesModule, ScatterSeriesModule, PieSeriesModule,
  CategoryAxisModule, NumberAxisModule, LegendModule,
]);
ModuleRegistry.registerModules([
  ClientSideRowModelModule,
  CellStyleModule,
  TextFilterModule,
  NumberFilterModule,
  ...(import.meta.env.DEV ? [ValidationModule] : []),
]);
const theme = themeQuartz.withParams({ accentColor: SERIES.primary, fontSize: 13, headerFontWeight: 600 });
const AXIS = { label: { color: "#475569" }, line: { stroke: "#cbd5e1" } };
const GRID_LINE = { gridLine: { style: [{ stroke: "#e2e8f0" }] } };
const MIN_SCORED = 3;
const EXAMPLES = ["win rate by reason last 30 days", "which SNAD cases did I settle under $25", "money protected by month"];

const Card = ({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) => (
  <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">{title}</h2>
      {right}
    </div>
    {children}
  </section>
);

function Tile({ name, value, sub }: { name: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{name}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

const usd = (n: number, currency = "USD") => n.toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: 0 });

export default function Analytics() {
  const [dataset, setDataset] = useState<"real" | "demo" | undefined>(undefined);
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    analyticsApi.summary(dataset).then(
      (d) => (setData(d), setError(null)),
      (e: Error) => setError(e.message),
    );
  }, [dataset]);

  if (error && !data) return <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">Couldn't load analytics: {error}</p>;
  if (!data) return <p className="text-sm text-slate-500">Loading analytics…</p>;
  const k = data.kpis;
  const [cur, money] = Object.entries(k.money)[0] ?? ["USD", { protected: 0, refunded: 0 }];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Analytics</h1>
        {data.demo && (
          <span data-testid="demo-badge" className="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-semibold text-amber-900" title="Generated demo history (seed --demo-history). Never mixed with your real cases and never used for real decisions.">
            Demo data
          </span>
        )}
        {data.has_real && data.has_demo && (
          <div className="ml-auto inline-flex rounded-md border border-slate-300 text-sm" role="group" aria-label="Dataset">
            {(["real", "demo"] as const).map((d) => (
              <button key={d} onClick={() => setDataset(d)} aria-pressed={data.dataset === d}
                className={`px-3 py-1 ${data.dataset === d ? "bg-indigo-600 text-white" : "text-slate-700 hover:bg-slate-50"}`}>
                {d === "real" ? "Your cases" : "Demo data"}
              </button>
            ))}
          </div>
        )}
      </div>
      {k.count === 0 ? (
        <Card title="No resolved disputes yet">
          <p className="text-sm text-slate-600">
            Analytics fills in as disputes resolve at PayPal. For a demo, load the labelled demo history:{" "}
            <code className="rounded bg-slate-100 px-1">python -m app.scripts.seed --demo-history</code>
          </p>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7" data-testid="kpis">
            <Tile name="Money protected" value={usd(money.protected, cur)} sub="won + partial-offer savings" />
            <Tile name="Money refunded" value={usd(money.refunded, cur)} sub="lost, settled or offers paid" />
            <Tile name="Win rate" value={pct(k.win_rate)} sub={`${k.contested} contested case(s)`} />
            <Tile name="Agent accuracy" value={pct(k.accuracy.rate)} sub={`${k.accuracy.correct} of ${k.accuracy.scored} checkable`} />
            <Tile name="Median time to respond" value={k.median_hours_to_response == null ? "—" : `${k.median_hours_to_response} h`} sub="dispute opened → response sent" />
            <Tile name="Before deadline" value={pct(k.before_deadline.pct)} sub={`${k.before_deadline.on_time} of ${k.before_deadline.answered} responses`} />
            <Tile name="Your overrides" value={String(k.overrides.count)} sub={`${k.overrides.won} won · agent right ${pct(k.overrides.accuracy_when_overridden)}`} />
          </div>
          <div className="grid gap-4 lg:grid-cols-3">
            <Card title="Money over time"><MoneyChart data={data} currency={cur} /></Card>
            <Card title="Win rate by reason"><WinRateChart data={data} /></Card>
            <Card title="Agent accuracy"><AccuracyChart data={data} /></Card>
          </div>
          <Ask dataset={data.dataset} />
          <Facts facts={data.facts} currency={cur} />
          <Calibration data={data} />
        </>
      )}
    </div>
  );
}

function MoneyChart({ data, currency }: { data: AnalyticsSummary; currency: string }) {
  const options = useMemo<AgCartesianChartOptions>(
    () => ({
      data: data.by_month,
      height: 260,
      series: [
        { type: "bar", xKey: "month", yKey: "money_protected", yName: "Protected", fill: SERIES.primary, cornerRadius: 4 },
        { type: "bar", xKey: "month", yKey: "money_refunded", yName: "Refunded", fill: SERIES.secondary, cornerRadius: 4 },
      ],
      axes: {
        x: { type: "category", position: "bottom", ...AXIS },
        y: { type: "number", position: "left", ...AXIS, ...GRID_LINE, label: { color: "#475569", formatter: (p: { value: number }) => usd(p.value, currency) } },
      },
      legend: { position: "bottom" },
    }),
    [data, currency],
  );
  return <AgCharts options={options} />;
}

function WinRateChart({ data }: { data: AnalyticsSummary }) {
  const rows = data.kpis.win_rate_by_reason.filter((r) => r.contested > 0);
  const options = useMemo<AgCartesianChartOptions>(
    () => ({
      data: rows.map((r) => ({ reason: r.label, win_rate: r.win_rate, cases: r.contested })),
      height: 260,
      series: [
        {
          type: "bar", xKey: "reason", yKey: "win_rate", yName: "Win rate", fill: SERIES.primary, cornerRadius: 4,
          label: { formatter: (p: { value: number }) => pct(p.value), color: "#334155", placement: "outside-end" },
          tooltip: { renderer: (p: { datum: { reason: string; win_rate: number; cases: number } }) => ({ heading: p.datum.reason, data: [{ label: "Win rate", value: `${pct(p.datum.win_rate)} of ${p.datum.cases}` }] }) },
        },
      ],
      axes: {
        x: { type: "category", position: "bottom", ...AXIS },
        y: { type: "number", position: "left", min: 0, max: 1, ...AXIS, ...GRID_LINE, label: { color: "#475569", formatter: (p: { value: number }) => pct(p.value) } },
      },
    }),
    [data],
  );
  return rows.length ? <AgCharts options={options} /> : <p className="text-sm text-slate-500">No contested cases yet.</p>;
}

function AccuracyChart({ data }: { data: AnalyticsSummary }) {
  // a month with 1-2 checkable cases swings 0% <-> 100%: show months with at least MIN_SCORED
  const rows = data.by_month.filter((m) => m.accuracy != null && (m.scored ?? 0) >= MIN_SCORED);
  const options = useMemo<AgCartesianChartOptions>(
    () => ({
      data: rows,
      height: 260,
      series: [
        {
          type: "line", xKey: "month", yKey: "accuracy", yName: "Agent accuracy", stroke: SERIES.primary, strokeWidth: 2, marker: { fill: SERIES.primary, size: 8 },
          tooltip: { renderer: (p: { datum: { month: string; accuracy: number; scored?: number } }) => ({ heading: p.datum.month, data: [{ label: "Agent right", value: `${pct(p.datum.accuracy)} of ${p.datum.scored ?? 0} checkable` }] }) },
        },
      ],
      axes: {
        x: { type: "category", position: "bottom", ...AXIS },
        y: { type: "number", position: "left", min: 0, max: 1, ...AXIS, ...GRID_LINE, label: { color: "#475569", formatter: (p: { value: number }) => pct(p.value) } },
      },
    }),
    [data],
  );
  return (
    <>
      {rows.length ? <AgCharts options={options} /> : <p className="text-sm text-slate-500">Not enough checkable outcomes yet.</p>}
      <p className="mt-1 text-xs text-slate-500">
        Checked only where the outcome shows the truth (cases left to PayPal to decide); months with at least {MIN_SCORED} such cases.
      </p>
    </>
  );
}

// ---- Ask your disputes --------------------------------------------------------------------------------------

function Ask({ dataset }: { dataset: string }) {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = async (fn: () => Promise<QueryResult>) => {
    setBusy(true);
    setError(null);
    try {
      setResult(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  // an edited query gets a title built from what it now shows (the LLM's title described the original question)
  const rerun = (q: AskQuery) => go(() => analyticsApi.query(dataset, { ...q, title: "" }));
  const q = result?.query;
  const chart = result ? resultChart(result) : null;
  return (
    <Card title="Ask your disputes">
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim().length >= 3) go(() => analyticsApi.ask(dataset, question.trim()));
        }}
      >
        <input
          aria-label="Ask a question about your disputes"
          value={question}
          maxLength={500}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. win rate by reason last 30 days"
          className="min-w-0 flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <button disabled={busy} className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
      <div className="mt-2 flex flex-wrap gap-2 text-xs">
        {EXAMPLES.map((ex) => (
          <button key={ex} onClick={() => (setQuestion(ex), go(() => analyticsApi.ask(dataset, ex)))} className="rounded-full border border-slate-300 px-2.5 py-0.5 text-slate-600 hover:bg-slate-50">
            {ex}
          </button>
        ))}
      </div>
      {error && <p className="mt-3 rounded-md bg-red-50 p-2 text-xs text-red-700" role="alert">{error}</p>}
      {q && result && (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="query-chips">
            <span className="text-xs text-slate-500">Understood as:</span>
            {q.filters.map((f, i) => (
              <span key={i} className="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2.5 py-0.5 text-indigo-900">
                <span>{chipPrefix(f)}</span>
                <input
                  aria-label={`${COLUMN_LABEL[f.column] ?? f.column} value`}
                  defaultValue={f.value}
                  onBlur={(e) => e.target.value !== f.value && rerun(withFilterValue(q, i, e.target.value))}
                  className="w-24 rounded border border-indigo-200 bg-white px-1 text-xs"
                />
                <button aria-label={`Remove filter ${chipText(f)}`} onClick={() => rerun(withoutFilter(q, i))} className="text-indigo-700 hover:text-indigo-900">×</button>
              </span>
            ))}
            <select aria-label="Group by" value={q.group_by[0] ?? ""} onChange={(e) => rerun({ ...q, group_by: e.target.value ? [e.target.value] : [] })}
              className="rounded-full border border-slate-300 px-2 py-0.5 text-xs">
              <option value="">no grouping</option>
              {GROUPABLE.map((g) => <option key={g} value={g}>by {(COLUMN_LABEL[g] ?? g).toLowerCase()}</option>)}
            </select>
            <select aria-label="Metric" value={q.metric} onChange={(e) => rerun({ ...q, metric: e.target.value })} className="rounded-full border border-slate-300 px-2 py-0.5 text-xs">
              {METRICS.map((m) => <option key={m} value={m}>{METRIC_LABEL[m]}</option>)}
            </select>
            <select aria-label="Chart" value={q.chart} onChange={(e) => rerun({ ...q, chart: e.target.value as AskQuery["chart"] })} className="rounded-full border border-slate-300 px-2 py-0.5 text-xs">
              {["bar", "line", "pie", "table"].map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <p className="text-xs text-slate-500">
            {result.matched} case(s) matched · {METRIC_LABEL[q.metric] ?? q.metric}: <span className="font-medium text-slate-700">{formatMetric(q.metric, result.value)}</span>
          </p>
          {chart && <div className="h-72"><AgCharts options={{ ...chart, height: 288 }} /></div>}
          <ResultGrid result={result} />
        </div>
      )}
    </Card>
  );
}

function ResultGrid({ result }: { result: QueryResult }) {
  const cols = useMemo<ColDef[]>(
    () =>
      result.columns.map((c) => ({
        field: c,
        headerName: COLUMN_LABEL[c] ?? METRIC_LABEL[c] ?? (c === "reason_label" ? "Reason" : c === "cases" ? "Cases" : label(c)),
        flex: 1,
        minWidth: 110,
        valueFormatter: (p: { value: unknown }) => (c in METRIC_LABEL ? formatMetric(c, p.value) : p.value == null ? "—" : String(p.value).replaceAll("_", " ")),
      })),
    [result],
  );
  return (
    <div style={{ height: Math.min(360, 48 + result.rows.length * 42) }}>
      <AgGridReact theme={theme} rowData={result.rows} columnDefs={cols} />
    </div>
  );
}

// ---- the fact table ----------------------------------------------------------------------------------------

function Facts({ facts, currency }: { facts: Fact[]; currency: string }) {
  const [by, setBy] = useState<"" | "reason" | "month" | "outcome">("");
  const money = (p: { value: number | null }) => (p.value == null ? "—" : usd(p.value, currency));
  const yesNo = (p: { value: boolean | null }) => (p.value == null ? "—" : p.value ? "yes" : "no");
  const words = (p: { value: string | null }) => (p.value ? String(p.value).replaceAll("_", " ").toLowerCase() : "—");
  const factCols = useMemo<ColDef<Fact>[]>(
    () => [
      { field: "ref", headerName: "Case", filter: "agTextColumnFilter", minWidth: 120 },
      { field: "reason_label", headerName: "Reason", filter: "agTextColumnFilter" },
      { field: "amount", headerName: "Amount", filter: "agNumberColumnFilter", valueFormatter: money, cellClass: "text-right" },
      { field: "outcome", headerName: "Outcome", filter: "agTextColumnFilter", valueFormatter: words },
      { field: "final_action", headerName: "Action sent", valueFormatter: words },
      { field: "recommended_action", headerName: "Agent recommended", valueFormatter: words },
      { field: "correct", headerName: "Agent right", valueFormatter: yesNo },
      { field: "rule_score", headerName: "Rule score", filter: "agNumberColumnFilter" },
      { field: "money_protected", headerName: "Protected", valueFormatter: money, cellClass: "text-right" },
      { field: "money_refunded", headerName: "Refunded", valueFormatter: money, cellClass: "text-right" },
      { field: "hours_to_response", headerName: "Hours to respond", filter: "agNumberColumnFilter" },
      { field: "before_deadline", headerName: "Before deadline", valueFormatter: yesNo },
      { field: "month", headerName: "Resolved" },
    ],
    [currency],
  );
  const groupCols = useMemo<ColDef<GroupRow>[]>(
    () => [
      { field: "key", headerName: by === "month" ? "Month" : by === "outcome" ? "Outcome" : "Reason", valueFormatter: by === "outcome" ? words : undefined },
      { field: "cases", headerName: "Cases" },
      { field: "win_rate", headerName: "Win rate", valueFormatter: (p) => pct(p.value) },
      { field: "contested", headerName: "Contested" },
      { field: "protected", headerName: "Protected", valueFormatter: money, cellClass: "text-right" },
      { field: "refunded", headerName: "Refunded", valueFormatter: money, cellClass: "text-right" },
      { field: "accuracy", headerName: "Agent accuracy", valueFormatter: (p) => pct(p.value) },
    ],
    [by, currency],
  );
  return (
    <Card
      title={`Disputes (${facts.length})`}
      right={
        <label className="text-xs text-slate-600">
          Group by{" "}
          <select aria-label="Group the table by" value={by} onChange={(e) => setBy(e.target.value as typeof by)} className="rounded border border-slate-300 px-1.5 py-0.5">
            <option value="">nothing (every case)</option>
            <option value="reason">reason</option>
            <option value="month">month</option>
            <option value="outcome">outcome</option>
          </select>
        </label>
      }
    >
      <div style={by ? undefined : { height: 420 }} data-testid="facts-grid">
        {by ? (
          <AgGridReact<GroupRow> theme={theme} domLayout="autoHeight" rowData={groupFacts(facts, by)} columnDefs={groupCols} defaultColDef={{ flex: 1, minWidth: 110 }} />
        ) : (
          <AgGridReact<Fact> theme={theme} rowData={facts} columnDefs={factCols} defaultColDef={{ flex: 1, minWidth: 110, sortable: true }} pagination={false} />
        )}
      </div>
    </Card>
  );
}

// ---- calibration -------------------------------------------------------------------------------------------

function Calibration({ data }: { data: AnalyticsSummary }) {
  const reasons = data.calibration.reasons;
  const [reason, setReason] = useState(reasons[0]?.reason ?? "");
  const r = reasons.find((x) => x.reason === reason) ?? reasons[0];
  const options = useMemo<AgCartesianChartOptions | undefined>(
    () =>
      r && {
        data: calibrationData(r.bins),
        height: 320,
        series: [
          { type: "line", xKey: "score", yKey: "fitted", yName: "Fitted", stroke: SERIES.primary, strokeWidth: 2, marker: { fill: SERIES.primary, size: 8 } },
          { type: "line", xKey: "score", yKey: "starting", yName: "Starting table", stroke: SERIES.reference, strokeWidth: 2, lineDash: [6, 4], marker: { enabled: false } },
          { type: "scatter", xKey: "score", yKey: "observed", yName: "Observed share", fill: SERIES.secondary, stroke: "#ffffff", strokeWidth: 2, size: 9 },
        ],
        axes: {
          x: { type: "number", position: "bottom", min: 0, max: 1, title: { text: "Rule score" }, ...AXIS },
          y: { type: "number", position: "left", min: 0, max: 1, title: { text: "P(win)" }, ...AXIS, ...GRID_LINE, label: { color: "#475569", formatter: (p: { value: number }) => pct(p.value) } },
        },
        legend: { position: "bottom" },
      },
    [r],
  );
  if (!r) return null;
  return (
    <Card
      title="Calibration: rule score → chance to win"
      right={
        <select aria-label="Reason" value={r.reason} onChange={(e) => setReason(e.target.value)} className="rounded border border-slate-300 px-1.5 py-0.5 text-xs">
          {reasons.map((x) => <option key={x.reason} value={x.reason}>{label(x.reason)}</option>)}
        </select>
      }
    >
      <div className="grid gap-4 md:grid-cols-3">
        <div className="md:col-span-2">{options && <AgCharts options={options} />}</div>
        <div className="space-y-2 text-xs text-slate-600" data-testid="calibration-status">
          <p>
            <span className="font-medium text-slate-800">{r.n}</span> contested outcome(s) with a rule score.{" "}
            {r.used ? "Enough to calibrate (10 or more)." : `Calibration starts at ${data.calibration.min_outcomes}; until then the starting table is used.`}
          </p>
          <p>
            {data.calibration.applies_to_decisions
              ? r.used
                ? "The money calculator uses the fitted curve for this reason."
                : "The money calculator uses the starting table for this reason."
              : "Demo data: shown for illustration, never used for real decisions."}
          </p>
          <p>Method: binned share of wins per rule-score band, pulled toward the starting table (2 pseudo-cases), then made non-decreasing (isotonic).</p>
        </div>
      </div>
    </Card>
  );
}

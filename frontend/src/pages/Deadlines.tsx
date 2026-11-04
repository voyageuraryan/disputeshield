// Phase 18: Deadlines view with Bryntum Scheduler (trial package: shows Bryntum's trial watermark; see PROGRESS).
// Rows = open disputes; bars = gathering evidence, buyer troubleshooting, the response deadline, PayPal review.
import "@bryntum/scheduler/scheduler.css";
import "@bryntum/scheduler/stockholm-light.css";
import "@bryntum/scheduler/fontawesome/css/fontawesome.css";
import "@bryntum/scheduler/fontawesome/css/solid.css";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { BryntumScheduler } from "@bryntum/scheduler-react";
import { StringHelper } from "@bryntum/scheduler";
import { deadlinesApi, disputesApi, type DisputeRow, type TroubleshootingWindow } from "../api";
import { COLORS, toSchedule } from "../deadlines/mapping";

export default function Deadlines() {
  const [rows, setRows] = useState<DisputeRow[] | null>(null);
  const [windows, setWindows] = useState<TroubleshootingWindow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const nav = useNavigate();
  useEffect(() => {
    Promise.all([disputesApi.list(), deadlinesApi.windows()]).then(
      ([r, w]) => (setRows(r), setWindows(w)),
      (e: Error) => setErr(e.message),
    );
  }, []);
  const data = useMemo(() => (rows ? toSchedule(rows, windows) : null), [rows, windows]);
  const config = useMemo(
    () => ({
      readOnly: true,
      rowHeight: 44,
      barMargin: 8,
      viewPreset: "dayAndWeek",
      eventStyle: "filled" as const,        // solid bars in the legend colours (tonal ones were too pale)
      eventLayout: "none" as const,         // bars on one line per row: the due milestone sits on the bar, not below it
      useInitialAnimation: false,
      columns: [
        { text: "Dispute", field: "name", width: 170 }, // real ids are long (PP-R-BSX-10195921)
        { text: "Reason", field: "reason", width: 145 },
        { text: "Amount", field: "amount", width: 95, align: "end" as const },
      ],
      eventRenderer: ({ eventRecord }: { eventRecord: { getData: (k: string) => unknown; name: string } }) =>
        eventRecord.getData("kind") === "due" ? "" : StringHelper.encodeHtml(eventRecord.name),
    }),
    [],
  );
  return (
    <div className="flex h-[calc(100vh-7rem)] flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Deadlines</h1>
        <div className="flex flex-wrap gap-3 text-xs text-slate-600" data-testid="legend">
          {(
            [
              ["Gathering evidence / response", COLORS.gather],
              ["Buyer troubleshooting", COLORS.ts],
              ["Response due", COLORS.due],
              ["PayPal reviewing (estimate)", COLORS.review],
            ] as const
          ).map(([t, c]) => (
            <span key={t} className="inline-flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: c }} aria-hidden />
              {t}
            </span>
          ))}
        </div>
        <span className="ml-auto text-xs text-slate-500">Click a bar to open the case. Bryntum Scheduler (trial).</span>
      </div>
      {err && <p className="rounded-md bg-red-50 p-2 text-sm text-red-700">{err}</p>}
      {!data && !err && <p className="text-sm text-slate-500">Loading…</p>}
      {data && data.resources.length === 0 && <p className="text-sm text-slate-500">No open disputes.</p>}
      {data && data.resources.length > 0 && (
        <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-slate-200 bg-white" data-testid="scheduler">
          <BryntumScheduler
            {...config}
            startDate={data.startDate}
            endDate={data.endDate}
            resources={data.resources}
            events={data.bars}
            onEventClick={({ eventRecord }) => nav(`/cases/${encodeURIComponent(String(eventRecord.resourceId))}`)}
          />
        </div>
      )}
    </div>
  );
}

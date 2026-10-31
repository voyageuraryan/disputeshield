import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { disputesApi, type DisputeRow } from "../api";
import { ActionChip, caseStatus, Chip, Due, dueIn, label, money, useNow } from "../format";

ModuleRegistry.registerModules([AllCommunityModule]);

const REFRESH_MS = 10_000;
const theme = themeQuartz.withParams({ accentColor: "#4f46e5", fontSize: 13, headerFontWeight: 600 });

function useIsPhone() {
  const q = "(max-width: 767px)";
  const [phone, setPhone] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setPhone(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return phone;
}

function useDisputes() {
  const [rows, setRows] = useState<DisputeRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      disputesApi
        .list()
        .then((r) => alive && (setRows(r), setError(null)))
        .catch((e) => alive && setError(e.message));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return { rows, error };
}

const matches = (r: DisputeRow, q: string) =>
  !q ||
  [r.paypal_dispute_id, label(r.reason), r.buyer_email, caseStatus(r).text, r.latest_decision?.recommendation]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(q.toLowerCase()));

function PhoneList({ rows, search, now }: { rows: DisputeRow[]; search: string; now: number }) {
  const navigate = useNavigate();
  const shown = rows.filter((r) => matches(r, search));
  if (!shown.length) return <p className="text-sm text-slate-500">No matching disputes.</p>;
  return (
    <ul className="space-y-2">
      {shown.map((r) => {
        const st = caseStatus(r);
        return (
          <li key={r.id}>
            <button
              onClick={() => navigate(`/cases/${r.paypal_dispute_id}`)}
              className="w-full rounded-lg border border-slate-200 bg-white p-3 text-left shadow-sm active:bg-slate-50"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-medium">{label(r.reason)}</span>
                <span className="font-semibold">{money(r.amount, r.currency)}</span>
              </div>
              <div className="mt-0.5 truncate text-xs text-slate-500">{r.buyer_email ?? "buyer unknown"}</div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                {r.latest_decision && <ActionChip action={r.latest_decision.recommendation} />}
                {r.latest_decision && <Chip text={`${r.latest_decision.confidence} confidence`} />}
                <Chip text={st.text} tone={st.tone} />
                {r.seller_response_due && r.status !== "RESOLVED" && (
                  <span className="ml-auto">
                    Due <Due iso={r.seller_response_due} now={now} />
                  </span>
                )}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export default function Queue() {
  const { rows, error } = useDisputes();
  const [search, setSearch] = useState("");
  const now = useNow(30_000);
  const phone = useIsPhone();
  const grid = useRef<AgGridReact<DisputeRow>>(null);
  const navigate = useNavigate();

  useEffect(() => {
    grid.current?.api?.refreshCells({ columns: ["due"], force: true });
  }, [now]);

  const cols = useMemo<ColDef<DisputeRow>[]>(
    () => [
      { headerName: "Dispute", field: "paypal_dispute_id", width: 165, filter: "agTextColumnFilter" },
      { headerName: "Reason", colId: "reason", valueGetter: (p) => label(p.data?.reason), width: 145, filter: "agTextColumnFilter" },
      {
        headerName: "Amount",
        colId: "amount",
        valueGetter: (p) => Number(p.data?.amount ?? 0),
        valueFormatter: (p) => (p.data ? money(p.data.amount, p.data.currency) : ""),
        filter: "agNumberColumnFilter",
        type: "rightAligned",
        width: 110,
      },
      {
        headerName: "Buyer",
        field: "buyer_email",
        minWidth: 160,
        flex: 1,
        filter: "agTextColumnFilter",
        valueFormatter: (p) => p.value ?? "-",
        tooltipField: "buyer_email",
      },
      {
        headerName: "Due in",
        colId: "due",
        valueGetter: (p) => dueIn(p.data?.seller_response_due)?.ms ?? Number.POSITIVE_INFINITY,
        cellRenderer: (p: { data?: DisputeRow }) => <Due iso={p.data?.seller_response_due} />,
        filter: false,
        width: 110,
        sort: "asc",
      },
      {
        headerName: "Recommendation",
        colId: "rec",
        valueGetter: (p) => label(p.data?.latest_decision?.recommendation),
        cellRenderer: (p: { data?: DisputeRow }) =>
          p.data?.latest_decision ? <ActionChip action={p.data.latest_decision.recommendation} /> : <span className="text-slate-400">-</span>,
        filter: "agTextColumnFilter",
        width: 145,
      },
      {
        headerName: "Confidence",
        colId: "confidence",
        valueGetter: (p) => p.data?.latest_decision?.confidence ?? "-",
        filter: "agTextColumnFilter",
        width: 115,
      },
      {
        headerName: "Status",
        colId: "status",
        valueGetter: (p) => (p.data ? caseStatus(p.data).text : ""),
        cellRenderer: (p: { data?: DisputeRow }) => {
          if (!p.data) return null;
          const st = caseStatus(p.data);
          return <Chip text={st.text} tone={st.tone} />;
        },
        filter: "agTextColumnFilter",
        width: 190,
      },
    ],
    [],
  );

  const open = rows?.filter((r) => r.status !== "RESOLVED").length ?? 0;
  const urgent = rows?.filter((r) => r.status !== "RESOLVED" && dueIn(r.seller_response_due, now)?.urgent).length ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Disputes</h1>
          <p className="text-sm text-slate-500">
            {open} open{urgent ? <span className="font-medium text-red-600"> · {urgent} due within 48 h</span> : ""}
          </p>
        </div>
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search disputes, buyers, status…"
          aria-label="Search disputes"
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 sm:w-72"
        />
      </div>
      {error && <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {rows && rows.length === 0 && <p className="text-sm text-slate-500">No disputes yet. New ones appear here automatically.</p>}

      {phone ? (
        rows ? <PhoneList rows={rows} search={search} now={now} /> : !error && <p className="text-sm text-slate-500">Loading…</p>
      ) : (
        // short lists size to their rows; long ones scroll inside a fixed-height grid
        <div style={(rows?.length ?? 0) > 15 ? { height: "calc(100vh - 220px)", minHeight: 360 } : undefined}>
          <AgGridReact<DisputeRow>
            ref={grid}
            theme={theme}
            domLayout={(rows?.length ?? 0) > 15 ? "normal" : "autoHeight"}
            tooltipShowDelay={300}
            rowData={rows ?? undefined}
            columnDefs={cols}
            defaultColDef={{ sortable: true, filter: true, floatingFilter: true, resizable: true }}
            quickFilterText={search}
            loading={rows === null && !error}
            getRowId={(p) => String(p.data.id)}
            onRowClicked={(e) => e.data && navigate(`/cases/${e.data.paypal_dispute_id}`)}
            rowClass="cursor-pointer"
            overlayNoRowsTemplate="No disputes match."
          />
        </div>
      )}
    </div>
  );
}

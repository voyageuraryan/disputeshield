import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AllCommunityModule, ModuleRegistry, type ColDef } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { disputesApi, type DisputeRow } from "../api";
import { ACTION_STYLE, Badge, dueIn, label, money, STATUS_STYLE, when } from "../format";

ModuleRegistry.registerModules([AllCommunityModule]);

const REFRESH_MS = 10_000;

export default function Queue() {
  const [rows, setRows] = useState<DisputeRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    let alive = true;
    const load = () =>
      disputesApi.list().then((r) => alive && (setRows(r), setError(null))).catch((e) => alive && setError(e.message));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const cols = useMemo<ColDef<DisputeRow>[]>(
    () => [
      { headerName: "Dispute", field: "paypal_dispute_id", width: 190 },
      { headerName: "Reason", valueGetter: (p) => label(p.data?.reason), width: 160 },
      { headerName: "Amount", valueGetter: (p) => (p.data ? money(p.data.amount, p.data.currency) : ""), width: 110 },
      { headerName: "PayPal status", valueGetter: (p) => label(p.data?.status), width: 170 },
      {
        headerName: "Respond by",
        valueGetter: (p) => p.data?.seller_response_due ?? null,
        cellRenderer: (p: { data?: DisputeRow }) =>
          p.data?.seller_response_due ? `${when(p.data.seller_response_due)} (${dueIn(p.data.seller_response_due)})` : "-",
        width: 210,
      },
      {
        headerName: "Recommendation",
        valueGetter: (p) => p.data?.latest_decision?.recommendation ?? "",
        cellRenderer: (p: { data?: DisputeRow }) => {
          const d = p.data?.latest_decision;
          return d ? (
            <span className="flex gap-1">
              <Badge text={d.recommendation.replace("_", " ")} style={ACTION_STYLE[d.recommendation]} />
              <Badge text={d.status} style={STATUS_STYLE[d.status]} />
            </span>
          ) : (
            "-"
          );
        },
        flex: 1,
      },
    ],
    [],
  );

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Dispute queue</h1>
        <span className="text-xs text-slate-500">Refreshes every {REFRESH_MS / 1000}s · click a row to open the case</span>
      </div>
      {error && <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {rows && rows.length === 0 && <p className="text-sm text-slate-500">No disputes yet.</p>}
      <div style={{ height: 480 }}>
        <AgGridReact<DisputeRow>
          rowData={rows ?? undefined}
          columnDefs={cols}
          loading={rows === null && !error}
          onRowClicked={(e) => e.data && navigate(`/cases/${e.data.paypal_dispute_id}`)}
          rowClass="cursor-pointer"
        />
      </div>
    </div>
  );
}

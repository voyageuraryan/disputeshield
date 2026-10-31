import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { disputesApi, type Decision, type DisputeDetail } from "../api";
import { ACTION_STYLE, Badge, dueIn, label, money, STATUS_STYLE, when } from "../format";

const NOTE_MAX = 1000;

function DecisionCard({ d, detail, onDone }: { d: Decision; detail: DisputeDetail; onDone: () => void }) {
  const c = detail.case;
  const sendable = d.recommendation === "FIGHT" || d.recommendation === "SETTLE";
  const start = (d.approved_action ?? d.recommendation) === "SETTLE" ? "SETTLE" : "FIGHT";
  const [action, setAction] = useState<"FIGHT" | "SETTLE">(start);
  // retry starts from what was actually sent; a new decision from the agent's draft
  const [note, setNote] = useState(d.final_note ?? d.draft_note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const live = d.status === "PROPOSED";

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const body = () => ({ action, note: note.trim() || undefined, approved_by: "merchant" });
  const allowed = action === "FIGHT" ? c.can_fight : c.can_settle;
  const err = d.paypal_response?.error ? d.paypal_response : null;

  return (
    <div className={`space-y-3 rounded-lg border bg-white p-4 ${live ? "border-indigo-300" : "opacity-90"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">Decision #{d.id}</span>
        <Badge text={d.recommendation.replace("_", " ")} style={ACTION_STYLE[d.recommendation]} />
        <Badge text={`${d.confidence} confidence`} />
        <Badge text={d.status} style={STATUS_STYLE[d.status]} />
        {d.retry_of && <span className="text-xs text-slate-500">retry of #{d.retry_of}</span>}
        {d.approved_action && d.approved_action !== d.recommendation && (
          <span className="text-xs text-slate-500">approved as {d.approved_action}</span>
        )}
      </div>
      <p className="text-sm text-slate-700">{d.reasoning}</p>
      <p className="text-xs text-slate-500">Cites evidence {d.cited_evidence_ids.map((i) => `#${i}`).join(", ") || "-"}</p>

      {d.final_note && !live && (
        <div className="rounded bg-slate-50 p-2 text-xs text-slate-700">
          <span className="font-medium">Note sent: </span>
          {d.final_note}
        </div>
      )}
      {d.action_request && (
        <p className="text-xs text-slate-500">
          PayPal call: <code>{d.action_request.endpoint}</code>
          {d.action_request.endpoint !== "accept-claim" && (
            <>
              {" · "}
              <a className="text-indigo-700 underline" href={disputesApi.pdfUrl(d.id)} target="_blank" rel="noreferrer">
                evidence PDF
              </a>
            </>
          )}
        </p>
      )}
      {err && (
        <p className="rounded bg-red-50 p-2 text-xs text-red-700">
          PayPal refused (HTTP {err.status_code}
          {err.issues?.length ? `, ${err.issues.join(", ")}` : ""}
          {err.debug_id ? `, debug id ${err.debug_id}` : ""}){err.message ? `: ${err.message}` : ""}.{" "}
          {d.can_retry
            ? "Nothing was processed, so it can be retried."
            : detail.decisions.some((x) => x.retry_of === d.id)
              ? `Retried as decision #${detail.decisions.find((x) => x.retry_of === d.id)?.id}.`
              : "PayPal may have processed it: check the case in PayPal before trying again."}
        </p>
      )}
      {d.status === "EXECUTED" && <p className="rounded bg-emerald-50 p-2 text-xs text-emerald-800">PayPal accepted this response.</p>}

      {(live || d.can_retry) && (
        <div className="space-y-2 border-t pt-3">
          {!sendable && live && (
            <p className="text-xs text-slate-600">
              The agent suggests gathering more evidence. You can still respond now by choosing an action and writing the note.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="text-slate-600">Respond with</span>
            {(["FIGHT", "SETTLE"] as const).map((a) => (
              <label key={a} className="flex items-center gap-1">
                <input type="radio" checked={action === a} onChange={() => setAction(a)} />
                {a === "FIGHT" ? "Fight (send evidence)" : "Settle (refund buyer)"}
              </label>
            ))}
          </div>
          {!allowed && <p className="text-xs text-amber-700">PayPal does not allow this action right now ({c.allowed_actions.join(", ") || "none"}).</p>}
          <textarea
            className="h-32 w-full rounded border p-2 text-sm"
            maxLength={NOTE_MAX}
            placeholder={action === "FIGHT" ? "Note to PayPal (facts only)" : "Optional note (a default refund note is used if empty)"}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>
              {note.length}/{NOTE_MAX} characters
            </span>
            <div className="flex gap-2">
              {live && (
                <button disabled={busy} onClick={() => run(() => disputesApi.reject(d.id))}
                  className="rounded border px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-50">
                  Reject
                </button>
              )}
              <button
                disabled={busy || !allowed || (action === "FIGHT" && !note.trim())}
                onClick={() => run(() => (live ? disputesApi.approve(d.id, body()) : disputesApi.retry(d.id, body())))}
                className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-40"
              >
                {busy ? "Sending to PayPal…" : live ? `Approve & send ${action}` : `Retry as ${action}`}
              </button>
            </div>
          </div>
        </div>
      )}
      {error && <p className="rounded bg-red-50 p-2 text-xs text-red-700">{error}</p>}
    </div>
  );
}

export default function CaseDetail() {
  const { disputeId = "" } = useParams();
  const [detail, setDetail] = useState<DisputeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => disputesApi.detail(disputeId).then((d) => (setDetail(d), setError(null))).catch((e) => setError(e.message)),
    [disputeId],
  );
  useEffect(() => {
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, [load]);

  if (error && !detail) return <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>;
  if (!detail) return <p className="text-sm text-slate-500">Loading…</p>;
  const { dispute: dp, case: c } = detail;
  const cited = new Set(detail.decisions[0]?.cited_evidence_ids ?? []);

  return (
    <div className="space-y-5">
      <Link to="/queue" className="text-sm text-indigo-700">← Queue</Link>
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">{dp.paypal_dispute_id}</h1>
        <span className="text-lg">{money(dp.amount, dp.currency)}</span>
        <Badge text={label(dp.reason)} />
        <Badge text={`${label(dp.stage)} · ${label(dp.status)}`} />
        {dp.seller_response_due && (
          <span className="text-sm text-slate-600">
            Respond by {when(dp.seller_response_due)} ({dueIn(dp.seller_response_due)})
          </span>
        )}
      </div>
      <p className="text-xs text-slate-500">
        PayPal allows now: {c.allowed_actions.join(", ") || "nothing (PayPal is handling it)"} · Order{" "}
        {dp.order_id ? `#${dp.order_id}` : "not matched"}
      </p>

      <div className="grid gap-5 lg:grid-cols-5">
        <div className="space-y-3 lg:col-span-3">
          <h2 className="font-medium">Decisions</h2>
          {detail.decisions.length === 0 && <p className="text-sm text-slate-500">The agents are still working…</p>}
          {detail.decisions.map((d) => (
            <DecisionCard key={`${d.id}-${d.status}`} d={d} detail={detail} onDone={load} />
          ))}
        </div>
        <div className="space-y-3 lg:col-span-2">
          <h2 className="font-medium">Evidence ({c.evidence.length})</h2>
          <ul className="space-y-2">
            {c.evidence.map((e) => (
              <li key={e.id} className={`rounded border bg-white p-2 text-xs ${cited.has(e.id) ? "border-emerald-400" : ""}`}>
                <div className="mb-1 flex flex-wrap gap-1">
                  <span className="font-semibold">#{e.id}</span>
                  <Badge text={e.type.replaceAll("_", " ")} />
                  <Badge text={e.source} />
                  {e.flags.map((f) => (
                    <Badge key={f} text={f} style={f.includes("mismatch") || f.includes("cancel") ? "bg-red-100 text-red-700" : undefined} />
                  ))}
                </div>
                <p className="text-slate-700">{e.summary}</p>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="space-y-2">
        <h2 className="font-medium">Timeline <span className="text-xs font-normal text-slate-500">(newest first)</span></h2>
        <ol className="max-h-96 space-y-1 overflow-y-auto rounded border bg-white p-3 text-xs">
          {[...detail.timeline].reverse().map((s) => (
            <li key={s.id} className="flex gap-2">
              <span className="w-24 shrink-0 text-slate-400">{when(s.created_at)}</span>
              <span className="w-24 shrink-0 font-medium text-slate-600">{s.agent}</span>
              <span className="text-slate-800">{s.action}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

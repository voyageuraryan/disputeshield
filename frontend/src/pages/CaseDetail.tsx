import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { disputesApi, type Decision, type DisputeDetail, type Evidence, type EvidenceSource } from "../api";
import { ActionChip, addressLines, caseStatus, Chip, day, Due, label, money, useNow, when } from "../format";
import Timeline from "../Timeline";
import { useDisputeEvents } from "../useDisputeEvents";

const NOTE_MAX = 1000;
type Action = "FIGHT" | "SETTLE";

const Card = ({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) => (
  <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">{title}</h2>
      {right}
    </div>
    {children}
  </section>
);

const Field = ({ k, children }: { k: string; children: React.ReactNode }) => (
  <div className="grid grid-cols-[7.5rem_1fr] gap-2 py-1 text-sm">
    <dt className="text-slate-500">{k}</dt>
    <dd className="min-w-0 break-words text-slate-900">{children}</dd>
  </div>
);

// ---- left: facts + order -----------------------------------------------------------------

function Facts({ detail, now }: { detail: DisputeDetail; now: number }) {
  const { dispute: d, case: c, order: o } = detail;
  return (
    <div className="space-y-4">
      <Card title="Dispute">
        <dl>
          <Field k="Case">{d.paypal_dispute_id}</Field>
          <Field k="Reason">{label(d.reason)}</Field>
          <Field k="Amount">{money(d.amount, d.currency)}</Field>
          <Field k="Stage">{label(d.stage)}</Field>
          <Field k="PayPal status">{label(d.status)}</Field>
          <Field k="Respond by">
            {d.seller_response_due ? (
              <>
                <Due iso={d.seller_response_due} now={now} /> <span className="text-slate-500">({when(d.seller_response_due)})</span>
              </>
            ) : (
              "-"
            )}
          </Field>
          <Field k="You can now">{c.allowed_actions.length ? c.allowed_actions.map(label).join(", ") : "nothing (PayPal is handling it)"}</Field>
          <Field k="Last update">{when(d.updated_at)}</Field>
        </dl>
      </Card>
      <Card title="Order">
        {!o ? (
          <p className="text-sm text-slate-500">No store order matched this dispute.</p>
        ) : (
          <dl>
            <Field k="Order">#{o.id} · {day(o.created_at)}</Field>
            <Field k="Items">
              {(o.items ?? []).map((i, n) => (
                <div key={n}>
                  {i.quantity ?? 1}× {i.name}
                </div>
              ))}
            </Field>
            <Field k="Total">{money(o.amount, o.currency)}</Field>
            <Field k="Buyer">{o.buyer_email ?? "-"}</Field>
            <Field k="Ship to">
              {addressLines(o.shipping_address).map((l) => (
                <div key={l}>{l}</div>
              )) || "-"}
            </Field>
            <Field k="Invoice">{o.invoice_id ?? "-"}</Field>
            <Field k="PayPal order">{o.paypal_order_id ?? "-"}</Field>
            <Field k="Capture">{o.capture_id ?? "-"}</Field>
            {o.shipments.map((s) => (
              <Field key={s.id} k="Shipment">
                <div>
                  {s.carrier ?? "Carrier?"} {s.tracking_number ?? "(untracked)"}
                </div>
                <div className="text-slate-500">
                  {label(s.status)}
                  {s.delivered_at ? ` · delivered ${day(s.delivered_at)}` : ""}
                </div>
                {s.delivered_address && <div className="text-slate-500">to {addressLines(s.delivered_address).join(", ")}</div>}
              </Field>
            ))}
          </dl>
        )}
      </Card>
    </div>
  );
}

// ---- middle: checklist + evidence ------------------------------------------------------------

function SourceDialog({ ev, onClose }: { ev: Evidence; onClose: () => void }) {
  const [src, setSrc] = useState<EvidenceSource | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    disputesApi.source(ev.id).then(setSrc).catch((e) => setError(e.message));
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [ev.id, onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/40 p-0 sm:items-center sm:p-6" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Source of evidence #${ev.id}`}
        onClick={(e) => e.stopPropagation()}
        className="max-h-[85vh] w-full overflow-hidden rounded-t-xl bg-white shadow-xl sm:max-w-2xl sm:rounded-xl"
      >
        <div className="flex items-start justify-between gap-3 border-b p-4">
          <div>
            <h3 className="font-semibold">{src?.title ?? `Evidence #${ev.id}`}</h3>
            <p className="text-xs text-slate-500">
              {src ? `${src.origin} · ${src.source_ref}` : ev.source_ref}
            </p>
          </div>
          <button onClick={onClose} className="rounded px-2 py-1 text-sm text-slate-500 hover:bg-slate-100" aria-label="Close">
            ✕
          </button>
        </div>
        <div className="max-h-[65vh] overflow-auto p-4">
          {error && <p className="text-sm text-red-700">{error}</p>}
          {!src && !error && <p className="text-sm text-slate-500">Loading the record…</p>}
          {src && <pre className="whitespace-pre-wrap break-words text-xs text-slate-800">{JSON.stringify(src.record, null, 2)}</pre>}
        </div>
      </div>
    </div>
  );
}

function EvidencePanel({ detail, cited }: { detail: DisputeDetail; cited: Set<number> }) {
  const { checklist: cl, case: c } = detail;
  const [open, setOpen] = useState<Evidence | null>(null);
  const close = useCallback(() => setOpen(null), []);
  return (
    <div className="space-y-4">
      <Card title="Evidence checklist" right={cl.supported ? <span className="text-sm font-semibold">{Math.round(cl.rule_score * 100)}/100</span> : null}>
        {!cl.supported ? (
          <p className="text-sm text-slate-600">There is no rule for this dispute reason yet, so a person should review it.</p>
        ) : (
          <>
            <div className="mb-3 h-2 overflow-hidden rounded bg-slate-100" aria-hidden>
              <div className="h-full bg-indigo-600" style={{ width: `${cl.rule_score * 100}%` }} />
            </div>
            <ul className="space-y-1.5 text-sm">
              {cl.items.map((i) => (
                <li key={i.type} className="flex items-center gap-2">
                  <span className={i.present ? "text-indigo-600" : i.required ? "text-red-600" : "text-slate-400"} aria-hidden>
                    {i.present ? "✓" : "✗"}
                  </span>
                  <span className={i.present ? "" : "text-slate-500"}>{label(i.type)}</span>
                  <span className="text-xs text-slate-400">{i.required ? "required" : "supporting"}</span>
                  {i.evidence_ids.length > 0 && <span className="ml-auto text-xs text-slate-400">#{i.evidence_ids.join(", #")}</span>}
                  {!i.present && i.required && <span className="ml-auto text-xs font-medium text-red-600">missing</span>}
                </li>
              ))}
            </ul>
            {cl.red_flags.map((f) => (
              <p key={f.code} className="mt-3 rounded-md bg-red-50 p-2 text-xs text-red-700">
                Red flag: {f.description}
              </p>
            ))}
            {cl.baseline && (
              <p className="mt-3 text-xs text-slate-500">
                Rules suggest <span className="font-medium text-slate-700">{label(cl.baseline)}</span>: {cl.baseline_why}.
              </p>
            )}
          </>
        )}
      </Card>
      <Card title={`Evidence (${c.evidence.length})`}>
        {c.evidence.length === 0 && <p className="text-sm text-slate-500">No evidence gathered yet.</p>}
        <ul className="space-y-2">
          {c.evidence.map((e) => (
            <li key={e.id} className={`rounded-md border p-3 text-sm ${cited.has(e.id) ? "border-indigo-300 bg-indigo-50/40" : "border-slate-200"}`}>
              <div className="mb-1 flex flex-wrap items-center gap-1.5">
                <span className="font-semibold">#{e.id}</span>
                <span className="font-medium">{label(e.type)}</span>
                <Chip text={e.source === "paypal" ? "PayPal" : "Store"} />
                {cited.has(e.id) && <Chip text="cited" tone="accent" />}
                {e.flags
                  .filter((f) => f === "address_mismatch" || f === "tracker_cancelled")
                  .map((f) => (
                    <Chip key={f} text={f.replace("_", " ")} tone="danger" />
                  ))}
              </div>
              <p className="text-slate-700">{e.summary}</p>
              <button onClick={() => setOpen(e)} className="mt-1 text-xs font-medium text-indigo-700 hover:underline">
                View source ({e.source === "paypal" ? "PayPal record" : "store record"}) →
              </button>
            </li>
          ))}
        </ul>
      </Card>
      {open && <SourceDialog ev={open} onClose={close} />}
    </div>
  );
}

// ---- right: recommendation ---------------------------------------------------------------------

function current(decisions: Decision[]) {
  return decisions.find((d) => d.status === "PROPOSED") ?? decisions.find((d) => d.can_retry) ?? decisions[0] ?? null;
}

function Recommendation({ d, detail, onDone }: { d: Decision; detail: DisputeDetail; onDone: () => void }) {
  const c = detail.case;
  const actionable = d.status === "PROPOSED" || d.can_retry;
  const initial: Action | null =
    (d.approved_action as Action | null) ?? (d.recommendation === "GATHER_MORE" ? null : (d.recommendation as Action));
  const [action, setAction] = useState<Action | null>(initial);
  const [note, setNote] = useState(d.final_note ?? d.draft_note ?? "");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const changed = action !== null && action !== d.recommendation;
  const allowed = action === "FIGHT" ? c.can_fight : action === "SETTLE" ? c.can_settle : false;
  const needsNote = action === "FIGHT" && !note.trim();
  const err = d.paypal_response?.error ? d.paypal_response : null;
  const retriedBy = detail.decisions.find((x) => x.retry_of === d.id);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setConfirming(false);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const send = () => {
    const body = { action: action ?? undefined, note: note.trim() || undefined, approved_by: "merchant" };
    return run(() => (d.status === "PROPOSED" ? disputesApi.approve(d.id, body) : disputesApi.retry(d.id, body)));
  };
  const toggle = () => {
    setConfirming(false);
    setAction((a) => (a === "FIGHT" ? "SETTLE" : "FIGHT"));
  };

  return (
    <Card
      title="Recommendation"
      right={
        <span className="flex items-center gap-3 text-xs text-slate-400">
          {(d.status === "PROPOSED" || d.status === "FAILED" || d.status === "REJECTED") && detail.dispute.status !== "RESOLVED" && (
            <ReanalyzeButton disputeId={detail.dispute.paypal_dispute_id} onDone={onDone} />
          )}
          Decision #{d.id}
        </span>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <ActionChip action={d.recommendation} />
        <Chip text={`${d.confidence} confidence`} />
        {!actionable && <Chip text={d.status.toLowerCase()} tone={d.status === "FAILED" ? "danger" : "muted"} />}
      </div>
      <p className="mt-3 text-sm leading-relaxed text-slate-800">{d.reasoning}</p>
      {d.economic_note && (
        <p className="mt-3 rounded-md bg-slate-50 p-3 text-sm text-slate-700">
          <span className="font-medium">Is it worth it? </span>
          {d.economic_note}
        </p>
      )}
      {d.missing_items && d.missing_items.length > 0 && (
        <p className="mt-2 text-xs text-slate-500">Still missing: {d.missing_items.map(label).join(", ")}</p>
      )}
      {actionable && d.analysed_threshold && Number(d.analysed_threshold) !== Number(detail.settle_threshold) && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-md bg-indigo-50 p-2 text-xs text-indigo-900">
          <span>
            Analysed with a {money(d.analysed_threshold, detail.dispute.currency)} settle threshold; your setting is now{" "}
            {money(detail.settle_threshold, detail.dispute.currency)}.
          </span>
          <ReanalyzeButton disputeId={detail.dispute.paypal_dispute_id} onDone={onDone} primary />
        </div>
      )}

      {err && (
        <p className="mt-3 rounded-md bg-red-50 p-3 text-xs text-red-700">
          PayPal refused the last attempt (HTTP {err.status_code}
          {err.issues?.length ? `, ${err.issues.join(", ")}` : ""}
          {err.debug_id ? `, debug id ${err.debug_id}` : ""}).{" "}
          {d.can_retry ? "Nothing was processed; you can try again." : retriedBy ? `Retried as decision #${retriedBy.id}.` : "Check the case in PayPal before trying again."}
        </p>
      )}
      {d.status === "EXECUTED" && (
        <p className="mt-3 rounded-md bg-indigo-50 p-3 text-sm text-indigo-800">
          Sent to PayPal ({d.action_request?.endpoint}) on {when(d.executed_at)}. PayPal will now review it.
        </p>
      )}
      {d.status === "REJECTED" && <p className="mt-3 text-sm text-slate-600">You rejected this recommendation.</p>}
      {d.status === "SUPERSEDED" && <p className="mt-3 text-sm text-slate-600">Replaced by a newer analysis or closed because the case was resolved.</p>}

      {actionable && (
        <div className="mt-4 space-y-3 border-t pt-4">
          <div className="text-sm">
            {action ? (
              <>
                You are about to <span className="font-semibold">{action === "FIGHT" ? "fight (send evidence)" : "settle (refund the buyer)"}</span>
                {changed && <span className="text-slate-500"> — changed from {label(d.recommendation)}</span>}.
              </>
            ) : (
              <span className="text-slate-600">The agent suggests gathering more evidence first. Choose an action to respond now.</span>
            )}
          </div>
          {action && !allowed && (
            <p className="text-xs text-red-700">PayPal doesn't allow this right now (allowed: {c.allowed_actions.map(label).join(", ") || "nothing"}).</p>
          )}
          <label className="block">
            <span className="text-xs font-medium text-slate-600">{action === "SETTLE" ? "Note to PayPal (optional)" : "Seller note to PayPal (facts only)"}</span>
            <textarea
              className="mt-1 h-36 w-full rounded-md border border-slate-300 p-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              maxLength={NOTE_MAX}
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
                setConfirming(false);
              }}
            />
            <span className={`text-xs ${note.length > NOTE_MAX * 0.9 ? "text-red-600" : "text-slate-400"}`}>
              {note.length}/{NOTE_MAX}
            </span>
          </label>
          {action === "FIGHT" && d.action_request?.endpoint !== "accept-claim" && d.status === "PROPOSED" && d.action_request && (
            <a href={disputesApi.pdfUrl(d.id)} target="_blank" rel="noreferrer" className="block text-xs font-medium text-indigo-700 hover:underline">
              Preview the evidence PDF →
            </a>
          )}

          {confirming ? (
            <div className="rounded-md border border-indigo-200 bg-indigo-50 p-3 text-sm">
              <p className="text-indigo-900">
                Send this {action === "FIGHT" ? "evidence" : "refund"} to PayPal now? This can't be undone.
              </p>
              <div className="mt-2 flex gap-2">
                <button disabled={busy} onClick={send} className="rounded-md bg-indigo-600 px-3 py-1.5 text-white hover:bg-indigo-700 disabled:opacity-50">
                  {busy ? "Sending…" : "Yes, send it"}
                </button>
                <button disabled={busy} onClick={() => setConfirming(false)} className="rounded-md px-3 py-1.5 text-slate-700 hover:bg-white">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex flex-wrap gap-2">
              <button
                disabled={busy || !action || !allowed || needsNote}
                onClick={() => setConfirming(true)}
                className="flex-1 rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {d.status === "PROPOSED" ? "Approve" : "Try again"}
                {action ? ` · ${label(action)}` : ""}
              </button>
              <button disabled={busy} onClick={toggle} className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
                Change action
              </button>
              {d.status === "PROPOSED" && (
                <button
                  disabled={busy}
                  onClick={() => run(() => disputesApi.reject(d.id))}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50"
                >
                  Reject
                </button>
              )}
            </div>
          )}
          {needsNote && action === "FIGHT" && <p className="text-xs text-slate-500">Write the seller note to approve a fight.</p>}
        </div>
      )}
      {error && <p className="mt-3 rounded-md bg-red-50 p-2 text-xs text-red-700">{error}</p>}
    </Card>
  );
}

function ReanalyzeButton({ disputeId, onDone, primary }: { disputeId: string; onDone: () => void; primary?: boolean }) {
  const [state, setState] = useState<"idle" | "busy" | "queued" | "error">("idle");
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <span className="inline-flex items-center gap-2">
      <button
        disabled={state === "busy" || state === "queued"}
        onClick={async () => {
          setState("busy");
          try {
            await disputesApi.reanalyze(disputeId);
            setState("queued");
            setTimeout(onDone, 1500);
          } catch (e) {
            setState("error");
            setMsg((e as Error).message);
          }
        }}
        className={
          primary
            ? "rounded-md bg-indigo-600 px-2.5 py-1 font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            : "text-xs font-medium text-indigo-700 hover:underline disabled:opacity-50"
        }
      >
        {state === "busy" ? "Starting…" : state === "queued" ? "Re-analysing…" : "Re-analyse"}
      </button>
      {state === "error" && <span className="text-xs text-red-700">{msg}</span>}
    </span>
  );
}

function History({ decisions, currentId }: { decisions: Decision[]; currentId: number | undefined }) {
  const others = decisions.filter((d) => d.id !== currentId);
  if (!others.length) return null;
  return (
    <details className="rounded-lg border border-slate-200 bg-white p-4 text-sm shadow-sm">
      <summary className="cursor-pointer text-sm font-semibold uppercase tracking-wide text-slate-500">Earlier decisions ({others.length})</summary>
      <ul className="mt-3 space-y-2">
        {others.map((d) => (
          <li key={d.id} className="flex flex-wrap items-center gap-2 border-t pt-2">
            <span className="text-slate-500">#{d.id}</span>
            <ActionChip action={d.recommendation} />
            <Chip text={d.status.toLowerCase()} tone={d.status === "FAILED" ? "danger" : "muted"} />
            {d.approved_action && <span className="text-xs text-slate-500">approved as {label(d.approved_action)}</span>}
            {d.paypal_response?.issues?.length ? <span className="text-xs text-red-700">{d.paypal_response.issues.join(", ")}</span> : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

// ---- page -------------------------------------------------------------------------------------

function CaseSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading case">
      <div className="h-7 w-72 animate-pulse rounded bg-slate-200" />
      <div className="grid gap-4 lg:grid-cols-12">
        {["lg:col-span-3", "lg:col-span-5", "lg:col-span-4"].map((c) => (
          <div key={c} className={`${c} h-72 animate-pulse rounded-lg border border-slate-200 bg-white`} />
        ))}
      </div>
    </div>
  );
}

export default function CaseDetail() {
  const { disputeId = "" } = useParams();
  const [detail, setDetail] = useState<DisputeDetail | null>(null);
  const [error, setError] = useState<{ msg: string; notFound: boolean } | null>(null);
  const now = useNow(30_000);

  const load = useCallback(
    () =>
      disputesApi
        .detail(disputeId)
        .then((d) => (setDetail(d), setError(null)))
        .catch((e: Error) => setError({ msg: e.message, notFound: e.message.startsWith("404") })),
    [disputeId],
  );
  // Live steps also tell us when to refresh the decision / facts (debounced: agents log in bursts).
  const refresh = useRef<ReturnType<typeof setTimeout>>();
  const { steps, state } = useDisputeEvents(disputeId, () => {
    clearTimeout(refresh.current);
    refresh.current = setTimeout(load, 700);
  });
  useEffect(() => {
    load();
    const t = setInterval(load, 60_000); // safety net; live events drive normal refreshes
    return () => {
      clearInterval(t);
      clearTimeout(refresh.current);
    };
  }, [load]);

  if (error && !detail) {
    return (
      <div className="mx-auto max-w-md rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm">
        <p className="font-medium">{error.notFound ? "This dispute isn't in DisputeShield" : "Couldn't load this case"}</p>
        <p className="mt-1 text-sm text-slate-500">{error.notFound ? `No dispute ${disputeId}. It may not have been ingested yet.` : error.msg}</p>
        <div className="mt-4 flex justify-center gap-2">
          <Link to="/queue" className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Back to disputes</Link>
          {!error.notFound && (
            <button onClick={load} className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm text-white">Try again</button>
          )}
        </div>
      </div>
    );
  }
  if (!detail) return <CaseSkeleton />;
  const d = current(detail.decisions);
  const cited = new Set(d?.cited_evidence_ids ?? []);
  const st = caseStatus(detail.dispute);

  return (
    <div className="space-y-4">
      {error && (
        <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-800">
          Couldn't refresh ({error.msg}); showing the last loaded data.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Link to="/queue" className="text-sm text-indigo-700 hover:underline">
          ← Disputes
        </Link>
        <h1 className="text-lg font-semibold sm:text-xl">
          {label(detail.dispute.reason)} · {money(detail.dispute.amount, detail.dispute.currency)}
        </h1>
        <Chip text={st.text} tone={st.tone} />
        {detail.dispute.seller_response_due && detail.dispute.status !== "RESOLVED" && (
          <span className="text-sm text-slate-500">
            Due <Due iso={detail.dispute.seller_response_due} now={now} />
          </span>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-12">
        <div className="lg:col-span-3">
          <Facts detail={detail} now={now} />
        </div>
        <div className="lg:col-span-5">
          <EvidencePanel detail={detail} cited={cited} />
        </div>
        {/* On phones the recommendation comes first: it's what the merchant acts on. */}
        <div className="order-first space-y-4 lg:order-none lg:col-span-4">
          {d ? (
            <Recommendation key={`${d.id}-${d.status}`} d={d} detail={detail} onDone={load} />
          ) : (
            <Card title="Recommendation">
              <p className="text-sm text-slate-500">The agents are still working on this case…</p>
            </Card>
          )}
          <History decisions={detail.decisions} currentId={d?.id} />
          <Timeline steps={steps} state={state} />
        </div>
      </div>
    </div>
  );
}

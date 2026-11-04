import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  disputesApi,
  troubleshootingApi,
  type Decision,
  type DisputeDetail,
  type Evidence,
  type EvidenceSource,
  type MerchantUpload,
  type RunInfo,
  type SendAction,
  type MoneyOption,
  type TroubleshootingStatus,
  type TSProofView,
} from "../api";
import { ActionChip, addressLines, caseStatus, Chip, day, Due, fetchedAgo, fightRoute, label, money, sourceLabel, useNow, when, type Tone } from "../format";
import { AddEvidenceForm, CoachPanel, ConfirmedUpload, ConfirmUpload } from "../evidence/AddEvidence";
import { offerNotePreview, offerProblems, sendable, withOffer } from "../offers/logic";
import ShippingPanel from "../ShippingPanel";
import Timeline from "../Timeline";
import { useDisputeEvents } from "../useDisputeEvents";

const NOTE_MAX = 1000;
type Action = SendAction;
const OFFERS: Action[] = ["SETTLE_PARTIAL", "REPLACEMENT"];

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

function Facts({ detail, now, onDone }: { detail: DisputeDetail; now: number; onDone: () => void }) {
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
          </dl>
        )}
      </Card>
      {o && <ShippingCard detail={detail} onDone={onDone} />}
      <TroubleshootingCard detail={detail} />
    </div>
  );
}

const TS_STATUS: Record<string, { text: string; tone: Tone }> = {
  INVITED: { text: "Invited, not opened yet", tone: "accent" },
  ACTIVE: { text: "Buyer opened the page", tone: "accent" },
  AWAITING_PROOF: { text: "Waiting for buyer's proof", tone: "accent" },
  RESOLVED_FIXED: { text: "Fixed by troubleshooting", tone: "solid" },
  DEFECT_CONFIRMED: { text: "Defect confirmed", tone: "danger" },
  SAFETY_STOP: { text: "Stopped for safety", tone: "danger" },
  EXPIRED: { text: "Link expired", tone: "muted" },
  HANDED_TO_MERCHANT: { text: "Handed to you", tone: "accent" },
};

/** Phase 9A: invite the buyer of a faulty electronic item to a private troubleshooting page. */
const STEP_DONE: Record<string, string> = {
  PENDING: "not done yet",
  DONE_FIXED: "fixed it",
  DONE_NOT_FIXED: "done, not fixed",
  SKIPPED: "skipped",
  CANT_DO: "couldn't do it",
};

/** Checks on a buyer upload (flags for the merchant, never decisions) and the advisory photo check. */
export function ProofFlags({ p }: { p: TSProofView }) {
  const c = p.checks ?? {};
  const v = p.vision_verdict;
  if (c.purged_at) {
    return <span className="block text-xs text-slate-500">File deleted: {c.purged_reason ?? "retention policy"}.</span>;
  }
  const warn: string[] = [];
  if (c.taken_after_dispute === "no") warn.push("taken before the dispute was opened");
  if (c.reused_from_other_cases?.length) warn.push(`same file as case ${c.reused_from_other_cases.join(", ")}`);
  if (c.near_duplicates?.length)
    warn.push(`looks like ${c.near_duplicates.map((d) => (d.step ? `step ${d.step}'s upload` : `an upload in case ${d.case}`)).join(", ")}`);
  if (c.duplicate_of_steps?.length) warn.push(`same file as step ${c.duplicate_of_steps.join(", ")}`);
  const info = [
    c.detected_type && `${c.detected_type}${c.converted ? ` (${c.converted})` : ""}`,
    c.duration_s != null && `${c.duration_s} s`,
    c.taken_after_dispute === "yes" ? "taken after the dispute opened" : c.taken_after_dispute === "unknown" ? `capture time unknown${c.capture_time_note ? ` (${c.capture_time_note})` : ""}` : null,
    c.metadata_removed?.length ? `removed: ${c.metadata_removed.join(", ")}` : null,
    p.evidence_ready ? "ready to send as evidence (≤ 10 MB)" : "over 10 MB, not ready as evidence",
  ].filter(Boolean);
  return (
    <span className="block text-xs text-slate-600">
      {warn.length > 0 && <span className="block font-medium text-amber-800">Check: {warn.join("; ")}</span>}
      {info.join(" · ")}
      {v?.shows_step && (
        <span className="block">
          AI look (advisory only): shows the step = <b>{v.shows_step}</b>. {v.what_is_visible}
          {v.concerns ? ` Concerns: ${v.concerns}` : ""}
        </span>
      )}
      {v?.skipped && <span className="block">AI look: {v.skipped}</span>}
      {v?.error && <span className="block">AI look: {v.error}</span>}
    </span>
  );
}

/** What the buyer and the troubleshooting agent did: steps (with proof thumbnails, checks and the advisory photo
 * check), sources, and the whole chat (read-only), plus "Reply as seller" while the buyer's page is open. */
function TroubleshootingDetails({ id, st, onChange }: { id: string; st: TroubleshootingStatus; onChange: (s: TroubleshootingStatus) => void }) {
  const steps = st.steps ?? [];
  const proofs = st.proofs ?? [];
  const msgs = st.messages ?? [];
  const sources = (st.sources ?? []).filter((x) => !x.url.startsWith("internal:"));
  const s = st.session!;
  const live = ["INVITED", "ACTIVE", "AWAITING_PROOF"].includes(s.status);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const chatEnd = useRef<HTMLLIElement>(null);
  useEffect(() => {
    chatEnd.current?.scrollIntoView?.({ block: "nearest" });
  }, [msgs.length]);
  const call = async (fn: () => Promise<TroubleshootingStatus>) => {
    setBusy(true);
    setErr(null);
    try {
      onChange(await fn());
      return true;
    } catch (e) {
      setErr((e as Error).message.replace(/^\d+: /, ""));
      return false;
    } finally {
      setBusy(false);
    }
  };
  const send = async () => {
    const body = text.trim();
    if (body && (await call(() => troubleshootingApi.reply(id, body)))) setText("");
  };
  return (
    <div className="mt-3 space-y-3 text-sm">
      {steps.length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Steps{st.session?.plan_generic ? " (generic, not product-specific)" : ""}
          </p>
          <ol className="mt-1 space-y-1">
            {steps.map((x) => (
              <li key={x.id}>
                {x.ordinal}. {x.title} <span className="text-slate-500">({STEP_DONE[x.status] ?? x.status})</span>
                {x.buyer_note && <span className="block pl-4 text-xs text-slate-600">Buyer's note: {x.buyer_note}</span>}
                {proofs
                  .filter((p) => p.step_id === x.id)
                  .map((p, n) => (
                    <div key={p.id} className="ml-4 mt-1 flex gap-2">
                      <a href={troubleshootingApi.proofUrl(id, p.id)} target="_blank" rel="noopener noreferrer" className="shrink-0 text-indigo-700 hover:underline">
                        {p.mime.startsWith("image") ? (
                          <img src={troubleshootingApi.proofUrl(id, p.id)} alt={`Buyer's photo ${n + 1} for step ${x.ordinal}`} className="h-16 w-16 rounded border border-slate-200 object-cover" loading="lazy" />
                        ) : (
                          <span className="flex h-16 w-16 items-center justify-center rounded border border-slate-200 bg-slate-50 text-xs">▶ video {n + 1}</span>
                        )}
                      </a>
                      <div className="min-w-0">
                        <span className="text-xs font-medium">{p.mime.startsWith("video") ? "video" : "photo"} {n + 1}</span>
                        <ProofFlags p={p} />
                      </div>
                    </div>
                  ))}
              </li>
            ))}
          </ol>
        </div>
      )}
      {sources.length > 0 && (
        <p className="text-xs text-slate-500">
          Based on:{" "}
          {sources.map((x, i) => (
            <span key={x.id}>
              {i > 0 && ", "}
              <a href={x.url} target="_blank" rel="noopener noreferrer nofollow" className="underline">
                {x.title || "source"}
              </a>
            </span>
          ))}
        </p>
      )}
      {msgs.length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Conversation ({msgs.filter((m) => m.role !== "system").length})</p>
          <ol aria-label="Conversation with the buyer" className="mt-1 max-h-80 space-y-1.5 overflow-y-auto rounded-md border border-slate-100 bg-slate-50 p-2">
            {msgs.map((m, i) =>
              m.role === "system" ? (
                <li key={m.id ?? i} className="text-center text-xs italic text-slate-500">{m.body}</li>
              ) : (
                <li key={m.id ?? i} className="whitespace-pre-line">
                  <span className={`font-medium ${m.role === "merchant" ? "text-amber-800" : m.role === "buyer" ? "text-indigo-800" : ""}`}>
                    {m.role === "buyer" ? "Buyer" : m.role === "merchant" ? "You (seller)" : "Assistant"}:
                  </span>{" "}
                  {m.body}
                </li>
              ),
            )}
            <li ref={chatEnd} aria-hidden className="h-px" />
          </ol>
        </div>
      )}
      {live ? (
        <div>
          {s.merchant_active && (
            <p role="status" className="mb-1 text-xs text-amber-800">
              The assistant is paused while you're chatting (for 15 minutes after your last reply).{" "}
              <button disabled={busy} onClick={() => call(() => troubleshootingApi.resumeAssistant(id))} className="font-medium underline">
                Let the assistant continue
              </button>
            </p>
          )}
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              send();
            }}
          >
            <label htmlFor="seller-reply" className="sr-only">
              Reply as seller
            </label>
            <textarea
              id="seller-reply"
              rows={2}
              maxLength={1500}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Reply as seller (the buyer sees it on their page as “Seller”)"
              className="min-w-0 flex-1 rounded-md border border-slate-300 p-2 text-sm"
            />
            <button type="submit" disabled={busy || !text.trim()} className="self-end rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40">
              Reply as seller
            </button>
          </form>
        </div>
      ) : (
        <p className="text-xs text-slate-500">The buyer's page has ended, so replies there aren't possible; reply in the PayPal case.</p>
      )}
      {err && (
        <p role="alert" className="rounded-md bg-amber-50 p-2 text-amber-900">
          {err}
        </p>
      )}
    </div>
  );
}

export function TroubleshootingCard({ detail }: { detail: DisputeDetail }) {
  const id = detail.dispute.paypal_dispute_id;
  const [st, setSt] = useState<TroubleshootingStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string; emailOffer?: boolean } | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const steps = detail.timeline.length;
  const load = useCallback(() => {
    troubleshootingApi
      .status(id)
      .then((s) => (setSt(s), setLoadError(null)))
      .catch((e: Error) => setLoadError(e.message));
  }, [id]);
  useEffect(load, [load, steps]); // the timeline grows when the buyer opens the page or an invite goes out
  // While the buyer's page is open, chat messages don't touch the timeline: refresh the panel every few seconds.
  const running = !!st?.session && ["INVITED", "ACTIVE", "AWAITING_PROOF"].includes(st.session.status);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [running, load]);

  const send = async (via: "paypal_message" | "email", resend = false) => {
    if (resend && !window.confirm("Send the buyer a new link? The old link will stop working.")) return;
    setBusy(true);
    setMsg(null);
    setLink(null);
    try {
      const r = await troubleshootingApi.invite(id, via, resend);
      if (r.already_invited) setMsg({ kind: "ok", text: "The buyer already has a working link. Use “Send a new link” to send another." });
      else setMsg({ kind: "ok", text: via === "email" ? "Email invite recorded." : "Invite sent in the PayPal dispute thread." });
      if (r.link) setLink(r.link);
      load();
    } catch (e) {
      const detail = e instanceof ApiError ? (e.detail as { reason?: string; email_available?: boolean } | null) : null;
      setMsg({ kind: "error", text: detail?.reason ?? (e as Error).message, emailOffer: !!detail?.email_available });
    } finally {
      setBusy(false);
    }
  };

  if (loadError) return <Card title="Buyer troubleshooting"><p className="text-sm text-red-700">Couldn't load: {loadError}</p></Card>;
  if (!st) return null;
  if (!st.eligible && !st.session) {
    const why = st.why.replace("Not eligible for buyer troubleshooting: ", "Not available: ");
    return (
      <Card title="Buyer troubleshooting">
        <p id="ts-why" className="text-sm text-slate-500">{why}.</p>
        <button disabled aria-describedby="ts-why" title={why} className="mt-2 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white opacity-40">
          Invite via PayPal message
        </button>
      </Card>
    );
  }
  const s = st.session;
  const open = detail.dispute.status !== "RESOLVED";
  const btn = "rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-40";
  return (
    <Card title="Buyer troubleshooting" right={s && <Chip text={TS_STATUS[s.status]?.text ?? label(s.status)} tone={TS_STATUS[s.status]?.tone} />}>
      {!s ? (
        <p className="text-sm text-slate-700">
          The buyer says the {st.product_name}
          {st.product_model ? ` (${st.product_model})` : ""} isn't as described. Invite them to a private, guided troubleshooting page:
          many faults are fixed in minutes, and if not, the page collects proof of the fault.
        </p>
      ) : (
        <dl>
          <Field k="Product">
            {s.product_name}
            {s.product_model ? ` · ${s.product_model}` : ""}
          </Field>
          <Field k="Invited">
            {when(s.invited_at)} via {s.invited_via === "email" ? "email" : "PayPal message"}
          </Field>
          <Field k="Link works until">{when(s.expires_at)}</Field>
          <Field k="Buyer last seen">{s.buyer_last_seen_at ? when(s.buyer_last_seen_at) : "not yet"}</Field>
          {s.symptom && <Field k="Problem">{s.symptom}</Field>}
          {s.language && s.language !== "en" && <Field k="Language">{s.language}</Field>}
          {s.ended_reason && <Field k="Ended">{s.ended_reason}</Field>}
        </dl>
      )}
      {s && <TroubleshootingDetails id={id} st={st} onChange={setSt} />}
      {open && (
        <div className="mt-3 flex flex-wrap gap-2">
          {!s ? (
            <>
              <button
                disabled={busy || !st.can_message}
                aria-describedby={st.can_message ? undefined : "ts-no-message"}
                onClick={() => send("paypal_message")}
                className={`${btn} bg-indigo-600 text-white hover:bg-indigo-700`}
                title={st.can_message ? undefined : "PayPal only allows messages while the dispute is an inquiry"}
              >
                {busy ? "Sending…" : "Invite via PayPal message"}
              </button>
              {st.buyer_email_known && (
                <button disabled={busy} onClick={() => send("email")} className={`${btn} border border-slate-300 hover:bg-slate-50`}>
                  Email the link instead
                </button>
              )}
            </>
          ) : (
            ["INVITED", "ACTIVE", "AWAITING_PROOF", "EXPIRED"].includes(s.status) && (
              <button
                disabled={busy}
                onClick={() => send(st.can_message ? "paypal_message" : "email", true)}
                className={`${btn} border border-slate-300 hover:bg-slate-50`}
              >
                Send a new link{st.can_message ? "" : " by email"}
              </button>
            )
          )}
        </div>
      )}
      {!s && !st.can_message && open && (
        <p id="ts-no-message" className="mt-2 text-xs text-slate-500">PayPal doesn't allow messages at this stage (only during an inquiry), so the link can only go by email.</p>
      )}
      {msg && (
        <div role={msg.kind === "error" ? "alert" : "status"} className={`mt-3 rounded-md p-2 text-sm ${msg.kind === "error" ? "bg-amber-50 text-amber-900" : "bg-emerald-50 text-emerald-800"}`}>
          {msg.text}
          {msg.emailOffer && (
            <button disabled={busy} onClick={() => send("email")} className="ml-2 font-medium underline">
              Email it instead
            </button>
          )}
        </div>
      )}
      {link && (
        <div className="mt-2 rounded-md bg-slate-50 p-2 text-xs text-slate-700">
          Email sending is a development stub, so nothing was emailed. Give the buyer this private link (it isn't shown again):
          <input readOnly value={link} onFocus={(e) => e.target.select()} aria-label="Buyer's private link" className="mt-1 w-full rounded border border-slate-300 bg-white p-1 font-mono text-xs" />
        </div>
      )}
    </Card>
  );
}

function ShippingCard({ detail, onDone }: { detail: DisputeDetail; onDone: () => void }) {
  const [saved, setSaved] = useState<string | null>(null);
  const o = detail.order!;
  const resolved = detail.dispute.status === "RESOLVED";
  return (
    <Card title="Shipping & tracking">
      {saved && (
        <div role="status" className="mb-3 rounded-md bg-emerald-50 p-2 text-sm text-emerald-800">
          {saved}{" "}
          {!resolved && (
            <>
              Re-analyse so the agents use it: <ReanalyzeButton disputeId={detail.dispute.paypal_dispute_id} onDone={onDone} />
            </>
          )}
        </div>
      )}
      <ShippingPanel
        order={o}
        digitalOnly={o.digital_only}
        onSaved={(what) => {
          setSaved(what === "tracking" ? "Tracking saved and uploaded to PayPal." : "Delivery recorded.");
          onDone();
        }}
      />
    </Card>
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

/** Phase 15: which kinds of identifiers matched, in words (never the hashed values). */
export function signalChips(flags: string[]): string[] {
  const out: string[] = [];
  if (flags.includes("ce3_like_match")) out.push("CE 3.0-like match");
  else if (flags.includes("partial_match")) out.push("partial match");
  const on = ["device", "ip", "address"].filter((k) => flags.includes(`${k}_reused`)).map((k) => (k === "ip" ? "IP" : k));
  if (on.length) out.push(`matched on ${on.join(" + ")}`);
  if (flags.includes("buyer_serial_disputes")) out.push("serial disputer");
  return out;
}

const sourceName = (s: string) => (s === "paypal" ? "PayPal" : s === "merchant_upload" ? "Your upload" : s === "buyer_portal" ? "Buyer" : "Store");

/** Phase 12: evidence the merchant adds (pending confirmation and confirmed), plus the upload form. */
function YourEvidence({ detail, addType, setAddType, canAdd, onDone }: {
  detail: DisputeDetail;
  addType: string | null;
  setAddType: (t: string | null) => void;
  canAdd: boolean;
  onDone: () => void;
}) {
  const id = detail.dispute.paypal_dispute_id;
  const types = detail.upload_types ?? [];
  const uploads = detail.uploads ?? [];
  const [fresh, setFresh] = useState<MerchantUpload[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const pending = [...uploads, ...fresh.filter((f) => !uploads.some((u) => u.id === f.id))].filter((u) => u.status === "PENDING");
  const confirmed = uploads.filter((u) => u.status === "CONFIRMED");
  if (!addType && pending.length === 0 && confirmed.length === 0 && !notice) return null;
  const done = (run: RunInfo | null) => {
    setFresh([]);
    setNotice(run ? run.message : null);
    onDone();
  };
  return (
    <Card title="Your evidence">
      <div className="space-y-2">
        {notice && <p className="rounded-md bg-indigo-50 p-2 text-xs text-indigo-900" role="status">{notice}</p>}
        {pending.map((u) => (
          <ConfirmUpload key={u.id} disputeId={id} upload={u} types={types} onDone={done} />
        ))}
        {confirmed.map((u) => (
          <ConfirmedUpload key={u.id} disputeId={id} upload={u} onDone={done} />
        ))}
        {addType && canAdd && (
          <AddEvidenceForm
            key={addType}
            disputeId={id}
            types={types}
            initialType={addType}
            onCancel={() => setAddType(null)}
            onUploaded={(u) => {
              setFresh((f) => [...f, u]);
              setAddType(null);
              onDone();
            }}
          />
        )}
      </div>
    </Card>
  );
}

function EvidencePanel({ detail, cited, onDone }: { detail: DisputeDetail; cited: Set<number>; onDone: () => void }) {
  const { checklist: cl, case: c } = detail;
  const [open, setOpen] = useState<Evidence | null>(null);
  const [addType, setAddType] = useState<string | null>(null);
  const close = useCallback(() => setOpen(null), []);
  const uploadable = new Set(detail.upload_types ?? []);
  const canAdd = detail.dispute.status !== "RESOLVED" && !detail.decisions.some((x) => x.status === "APPROVED");
  const coach = cl.coach ?? [];
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
                  {!i.present && canAdd && uploadable.has(i.type) && (
                    <button
                      onClick={() => setAddType(i.type)}
                      className={`${i.required ? "" : "ml-auto "}rounded border border-indigo-200 px-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-50`}
                      aria-label={`Add ${label(i.type)}`}
                    >
                      Add
                    </button>
                  )}
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
      <YourEvidence detail={detail} addType={addType} setAddType={setAddType} canAdd={canAdd} onDone={onDone} />
      {cl.supported && coach.length > 0 && (
        <Card title="What would win this?">
          <CoachPanel items={coach} onAdd={setAddType} canAdd={canAdd} />
        </Card>
      )}
      <Card title={`Evidence (${c.evidence.length})`}>
        {c.evidence.length === 0 && <p className="text-sm text-slate-500">No evidence gathered yet.</p>}
        <ul className="space-y-2">
          {c.evidence.map((e) => (
            <li key={e.id} className={`rounded-md border p-3 text-sm ${cited.has(e.id) ? "border-indigo-300 bg-indigo-50/40" : "border-slate-200"}`}>
              <div className="mb-1 flex flex-wrap items-center gap-1.5">
                <span className="font-semibold">#{e.id}</span>
                <span className="font-medium">{label(e.type)}</span>
                <Chip text={sourceName(e.source)} />
                {cited.has(e.id) && <Chip text="cited" tone="accent" />}
                {e.flags
                  .filter((f) => f === "address_mismatch" || f === "tracker_cancelled")
                  .map((f) => (
                    <Chip key={f} text={f.replace("_", " ")} tone="danger" />
                  ))}
                {signalChips(e.flags).map((s) => (
                  <Chip key={s} text={s} tone="accent" />
                ))}
              </div>
              <p className="text-slate-700">{e.summary}</p>
              <button onClick={() => setOpen(e)} className="mt-1 text-xs font-medium text-indigo-700 hover:underline">
                View source ({e.source === "paypal" ? "PayPal record" : e.source === "merchant_upload" ? "your upload" : "store record"}) →
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

/** Phase 11: how old the PayPal data behind this recommendation is, and which sources were stale. */
export function Freshness({ d }: { d: Pick<Decision, "data_as_of" | "stale_sources"> }) {
  const now = useNow(30_000);
  const ago = fetchedAgo(d.data_as_of, now);
  const stale = d.stale_sources ?? [];
  if (!ago && stale.length === 0) return null;
  return (
    <>
      {ago && <p className="mt-2 text-xs text-slate-500" data-testid="data-freshness">{ago}</p>}
      {stale.length > 0 && (
        <p role="alert" data-testid="stale-banner" className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
          Couldn't refresh {stale.map(sourceLabel).join(", ")} from PayPal on this run, so the last good copy was used.
          Re-analyse once PayPal is reachable before relying on it.
        </p>
      )}
    </>
  );
}

function Recommendation({ d, detail, onDone }: { d: Decision; detail: DisputeDetail; onDone: () => void }) {
  const c = detail.case;
  const actionable = d.status === "PROPOSED" || d.can_retry;
  const initial: Action | null =
    (d.approved_action as Action | null) ?? (d.recommendation === "GATHER_MORE" ? null : (d.recommendation as Action));
  // A drafted note belongs to its action: switching action must never send another action's note (a fight note
  // with a refund). Offer notes are written by the server with the final amount unless the merchant writes one.
  const noteFor = (a: Action | null) =>
    a === null || a === "SETTLE_PARTIAL" ? "" : a === d.approved_action && d.final_note ? d.final_note : a === d.recommendation ? d.draft_note ?? "" : "";
  const [action, setAction] = useState<Action | null>(initial);
  const [note, setNote] = useState(() => noteFor(initial));
  const proposedOffer = d.offer_amount ?? d.options?.find((o) => o.action === "SETTLE_PARTIAL")?.offer_amount ?? "";
  const [offer, setOffer] = useState(proposedOffer ? Number(proposedOffer).toFixed(2) : "");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSend = sendable(c);
  const changed = action !== null && action !== d.recommendation;
  const escalating = action === "FIGHT" && fightRoute(c.allowed_actions) === "escalate";
  const allowed = action !== null && canSend.includes(action);
  const needsNote = action === "FIGHT" && !note.trim();
  const amount = Number(detail.dispute.amount);
  const offerIssues = action === "SETTLE_PARTIAL" ? offerProblems(offer, amount, detail.offer_bounds) : [];
  const cur = detail.dispute.currency;
  const pick = (a: Action) => {
    setConfirming(false);
    setAction(a);
    setNote(noteFor(a));
  };
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
    const body = {
      action: action ?? undefined,
      note: note.trim() || undefined,
      approved_by: "merchant",
      offer_amount: action === "SETTLE_PARTIAL" ? Number(offer).toFixed(2) : undefined,
    };
    return run(() => (d.status === "PROPOSED" ? disputesApi.approve(d.id, body) : disputesApi.retry(d.id, body)));
  };
  const toggle = () => {
    const pool: Action[] = canSend.length ? canSend : ["FIGHT", "SETTLE"];
    pick(pool[(pool.indexOf(action as Action) + 1) % pool.length]);
  };
  const doing =
    action === "SETTLE"
      ? "settle (refund the buyer in full)"
      : action === "SETTLE_PARTIAL"
        ? `offer the buyer a partial refund of ${offer && !offerIssues.length ? money(Number(offer).toFixed(2), cur) : "…"}`
        : action === "REPLACEMENT"
          ? "offer the buyer a replacement item"
          : escalating
            ? "fight by escalating to a PayPal claim"
            : "fight (send evidence)";

  return (
    <>
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
      <Freshness d={d} />
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
        <div className="mt-3 rounded-md bg-indigo-50 p-3 text-sm text-indigo-800">
          <p>
            Sent to PayPal ({d.action_request?.endpoint}) on {when(d.executed_at)}.{" "}
            {d.approved_action === "SETTLE"
              ? "PayPal refunds the buyer and closes the case."
              : d.action_request?.endpoint === "make-offer"
                ? "The buyer has the offer now. If they accept, PayPal refunds it and closes the case; if they decline, DisputeShield analyses the case again."
              : d.action_request?.endpoint === "escalate"
                ? "PayPal will review it as a claim and will probably ask you for evidence; DisputeShield analyses the case again when it does."
                : "PayPal will now review it."}
          </p>
          {d.final_note && (
            <p className="mt-2 whitespace-pre-wrap text-indigo-900">
              <span className="font-medium">Note sent: </span>
              {d.final_note}
            </p>
          )}
        </div>
      )}
      {d.status === "REJECTED" && <p className="mt-3 text-sm text-slate-600">You rejected this recommendation.</p>}
      {d.status === "SUPERSEDED" && <p className="mt-3 text-sm text-slate-600">Replaced by a newer analysis or closed because the case was resolved.</p>}

      {actionable && (
        <div className="mt-4 space-y-3 border-t pt-4">
          <div className="text-sm">
            {action ? (
              <>
                You are about to{" "}
                <span className="font-semibold">{doing}</span>
                {changed && <span className="text-slate-500"> — changed from {label(d.recommendation)}</span>}.
              </>
            ) : (
              <span className="text-slate-600">The agent suggests gathering more evidence first. Choose an action to respond now.</span>
            )}
          </div>
          {action && !allowed && (
            <p className="text-xs text-red-700">PayPal doesn't allow this right now (allowed: {c.allowed_actions.map(label).join(", ") || "nothing"}).</p>
          )}
          {action === "SETTLE_PARTIAL" && (
            <label className="block">
              <span className="text-xs font-medium text-slate-600">Offer to the buyer ({cur})</span>
              <input
                aria-label="Offer amount"
                inputMode="decimal"
                value={offer}
                onChange={(e) => {
                  setOffer(e.target.value);
                  setConfirming(false);
                }}
                className="mt-1 w-32 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
              {detail.offer_bounds && (
                <span className="ml-2 text-xs text-slate-500">
                  between {money(detail.offer_bounds.min, cur)} and {money(detail.offer_bounds.max, cur)}
                </span>
              )}
              {offerIssues.map((p) => (
                <span key={p} className="mt-1 block text-xs text-red-700" role="alert">
                  {p}
                </span>
              ))}
            </label>
          )}
          <label className="block">
            <span className="text-xs font-medium text-slate-600">
              {action === "SETTLE" ? "Note to PayPal (optional)" : action && OFFERS.includes(action) ? "Note to the buyer with the offer (optional)" : "Seller note to PayPal (facts only)"}
            </span>
            <textarea
              placeholder={
                action && OFFERS.includes(action) ? `Leave empty to send: ${offerNotePreview(action, c.dispute_id, offer || "0", detail.dispute.amount, cur)}` : undefined
              }
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
          {escalating && (
            <p className="text-xs text-slate-600">
              At this stage PayPal doesn't accept evidence. Escalating asks PayPal to review the case with your note; PayPal then asks for
              evidence, and the agents prepare it.
            </p>
          )}
          {action === "FIGHT" && !escalating && d.action_request?.endpoint !== "accept-claim" && d.status === "PROPOSED" && d.action_request && (
            <a href={disputesApi.pdfUrl(d.id)} target="_blank" rel="noreferrer" className="block text-xs font-medium text-indigo-700 hover:underline">
              Preview the evidence PDF →
            </a>
          )}

          {confirming ? (
            <div className="rounded-md border border-indigo-200 bg-indigo-50 p-3 text-sm">
              <p className="text-indigo-900">
                {escalating
                  ? "Escalate this case to a PayPal claim now? This can't be undone."
                  : action && OFFERS.includes(action)
                    ? `Send this offer to the buyer now? If they accept, PayPal ${action === "REPLACEMENT" ? "closes the case and you send the item" : `refunds ${money(Number(offer).toFixed(2), cur)} automatically`}. This can't be undone.`
                    : `Send this ${action === "FIGHT" ? "evidence" : "refund"} to PayPal now? This can't be undone.`}
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
                disabled={busy || !action || !allowed || needsNote || offerIssues.length > 0}
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
    {d.options && d.options.length > 0 && (
      <OptionsCard
        options={d.options}
        offer={offer}
        amount={amount}
        currency={cur}
        chosen={action}
        canPick={actionable}
        onPick={(a) => pick(a)}
      />
    )}
    </>
  );
}

/** Phase 13: Fight / Full refund / Partial offer / Replacement side by side, with the expected net outcome. */
export function OptionsCard({ options, offer, amount, currency, chosen, canPick, onPick }: {
  options: MoneyOption[];
  offer: string;
  amount: number;
  currency: string;
  chosen: string | null;
  canPick: boolean;
  onPick: (a: SendAction) => void;
}) {
  const { rows, best } = withOffer(options, offer, amount);
  return (
    <Card title="Options">
      <p className="mb-2 text-xs text-slate-500">
        Expected net outcome for you compared with keeping the sale (estimates: the win and accept chances are starting
        assumptions, not yet learned from your cases).
      </p>
      <ul className="space-y-2" data-testid="options">
        {rows.map((o) => (
          <li
            key={o.action}
            className={`rounded-md border p-2 text-sm ${o.action === chosen ? "border-indigo-400 bg-indigo-50/50" : "border-slate-200"} ${o.available ? "" : "opacity-60"}`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">
                {o.label}
                {o.action === best && <span className="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-800">best</span>}
              </span>
              <span className="tabular-nums">
                {o.net != null ? money(o.net, currency) : "—"}
                {o.chance != null && o.action !== "SETTLE" && <span className="ml-2 text-xs text-slate-500">{Math.round(Number(o.chance) * 100)}% chance</span>}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-slate-600">{o.why}</p>
            {canPick && o.available && o.action !== chosen && (
              <button onClick={() => onPick(o.action)} className="mt-1 text-xs font-medium text-indigo-700 hover:underline">
                Choose {o.label.toLowerCase()}
              </button>
            )}
          </li>
        ))}
      </ul>
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

const WIDE = "(min-width: 1024px)"; // Tailwind lg

function useIsWide() {
  const [wide, setWide] = useState(() => window.matchMedia(WIDE).matches);
  useEffect(() => {
    const mq = window.matchMedia(WIDE);
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

export default function CaseDetail() {
  const { disputeId = "" } = useParams();
  const wide = useIsWide();
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
          <Facts detail={detail} now={now} onDone={load} />
        </div>
        <div className="lg:col-span-5">
          <EvidencePanel detail={detail} cited={cited} onDone={load} />
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
          {wide && <Timeline steps={steps} state={state} />}
        </div>
        {/* On phones the (long) timeline goes last, after the facts and evidence. */}
        {!wide && <Timeline steps={steps} state={state} />}
      </div>
    </div>
  );
}

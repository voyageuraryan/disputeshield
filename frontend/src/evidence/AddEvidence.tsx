// Phase 12: the merchant adds missing evidence (file or fact), checks what was read, confirms; and the
// "What would win this?" coach. Nothing becomes evidence until the merchant presses Confirm.
import { useState } from "react";
import { uploadsApi, type CoachItem, type MerchantUpload, type RunInfo } from "../api";
import { label } from "../format";
import { ACCEPT, checkInput, coachLine, FACT_MAX, fieldsFor, prefill, readNote, toConfirmBody, type FieldValues } from "./logic";

const btn = "rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50";
const ghost = "rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50";
const input = "w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm";

export function CoachPanel({ items, onAdd, canAdd }: { items: CoachItem[]; onAdd: (type: string) => void; canAdd: boolean }) {
  if (items.length === 0) return <p className="text-sm text-slate-500">Nothing the rules ask for is missing.</p>;
  return (
    <ul className="space-y-3" data-testid="coach">
      {items.map((c) => {
        const single = !c.type.includes("+");
        return (
          <li key={c.type} className="rounded-md border border-slate-200 p-3 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{coachLine(c)}</span>
              {single && canAdd && (
                <button onClick={() => onAdd(c.type)} className="text-xs font-medium text-indigo-700 hover:underline">
                  Add {label(c.type)}
                </button>
              )}
            </div>
            <p className="mt-0.5 text-xs text-slate-500">
              {c.required ? "Required" : "Supporting"} · +{Math.round(c.gain * 100)} points, {c.assumption}
            </p>
            {c.howto.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-slate-600">
                {c.howto.map((h) => (
                  <li key={h}>{h}</li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function AddEvidenceForm({ disputeId, types, initialType, onUploaded, onCancel }: {
  disputeId: string;
  types: string[];
  initialType: string;
  onUploaded: (u: MerchantUpload) => void;
  onCancel: () => void;
}) {
  const [type, setType] = useState(initialType);
  const [file, setFile] = useState<File | null>(null);
  const [fact, setFact] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      aria-label="Add evidence"
      className="space-y-2 rounded-md border border-indigo-200 bg-indigo-50/40 p-3 text-sm"
      onSubmit={async (e) => {
        e.preventDefault();
        const problem = checkInput(file, fact);
        if (problem) return setErr(problem);
        setBusy(true);
        setErr(null);
        try {
          onUploaded(await uploadsApi.create(disputeId, type, file, fact));
        } catch (ex) {
          setErr((ex as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <label className="block">
        <span className="text-xs text-slate-600">Evidence type</span>
        <select value={type} onChange={(e) => setType(e.target.value)} className={input}>
          {types.map((t) => (
            <option key={t} value={t}>
              {label(t)}
            </option>
          ))}
        </select>
      </label>
      <label className="block">
        <span className="text-xs text-slate-600">File (PDF, JPG or PNG, under 10 MB)</span>
        <input type="file" accept={ACCEPT} onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="block w-full text-sm" />
      </label>
      <label className="block">
        <span className="text-xs text-slate-600">…or type the fact (e.g. the carrier's delivery confirmation number)</span>
        <textarea value={fact} maxLength={FACT_MAX} onChange={(e) => setFact(e.target.value)} rows={2} className={input} />
      </label>
      <p className="text-xs text-slate-500">Files are read by Google Gemini to fill in the details; you check and confirm every field.</p>
      {err && <p className="text-xs text-red-700" role="alert">{err}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={busy} className={btn}>
          {busy ? "Uploading and reading…" : "Upload"}
        </button>
        <button type="button" onClick={onCancel} className={ghost}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function ConfirmUpload({ disputeId, upload, types, onDone }: {
  disputeId: string;
  upload: MerchantUpload;
  types: string[];
  onDone: (run: RunInfo | null) => void;
}) {
  const [type, setType] = useState(upload.evidence_type);
  const [v, setV] = useState<FieldValues>(() => prefill(upload));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50/50 p-3 text-sm" data-testid={`confirm-${upload.id}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">
          Check the details: {upload.kind === "file" ? upload.original_name ?? "your file" : "your note"}
        </span>
        {upload.kind === "file" && (
          <a href={uploadsApi.fileUrl(disputeId, upload.id)} target="_blank" rel="noreferrer" className="text-xs text-indigo-700 hover:underline">
            Open the file
          </a>
        )}
      </div>
      {upload.fact && <p className="text-xs text-slate-600">You wrote: “{upload.fact}”</p>}
      <p className="text-xs text-slate-500">{readNote(upload)}</p>
      <label className="block">
        <span className="text-xs text-slate-600">Evidence type</span>
        <select value={type} onChange={(e) => setType(e.target.value)} className={input}>
          {types.map((t) => (
            <option key={t} value={t}>
              {label(t)}
            </option>
          ))}
        </select>
      </label>
      <div className="grid gap-2 sm:grid-cols-2">
        {fieldsFor(type).map((f) => (
          <label key={f.key} className={f.key === "delivered_to_address" || f.key === "note" ? "block sm:col-span-2" : "block"}>
            <span className="text-xs text-slate-600">{f.label}</span>
            <input
              type={f.key === "delivery_date" ? "date" : "text"}
              value={v[f.key]}
              placeholder={f.placeholder}
              onChange={(e) => setV({ ...v, [f.key]: e.target.value })}
              className={input}
            />
          </label>
        ))}
      </div>
      {err && <p className="text-xs text-red-700" role="alert">{err}</p>}
      <div className="flex flex-wrap gap-2">
        <button
          disabled={busy}
          className={btn}
          onClick={async () => {
            setBusy(true);
            setErr(null);
            try {
              onDone((await uploadsApi.confirm(disputeId, upload.id, toConfirmBody(v, type))).run);
            } catch (ex) {
              setErr((ex as Error).message);
              setBusy(false);
            }
          }}
        >
          {busy ? "Adding…" : "Confirm and add as evidence"}
        </button>
        <button
          disabled={busy}
          className={ghost}
          onClick={async () => {
            setBusy(true);
            try {
              await uploadsApi.remove(disputeId, upload.id);
              onDone(null);
            } catch (ex) {
              setErr((ex as Error).message);
              setBusy(false);
            }
          }}
        >
          Discard
        </button>
      </div>
    </div>
  );
}

export function ConfirmedUpload({ disputeId, upload, onDone }: { disputeId: string; upload: MerchantUpload; onDone: (run: RunInfo | null) => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex items-start justify-between gap-2 rounded-md border border-slate-200 p-2 text-xs">
      <span className="text-slate-700">
        <span className="font-medium">{label(upload.evidence_type)}</span>: {upload.summary}
      </span>
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            onDone(await uploadsApi.remove(disputeId, upload.id));
          } finally {
            setBusy(false);
          }
        }}
        className="shrink-0 text-red-700 hover:underline disabled:opacity-50"
      >
        Remove
      </button>
    </div>
  );
}

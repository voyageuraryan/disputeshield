import { useEffect, useState } from "react";
import { settingsApi, type MerchantSettings } from "../api";
import { money, when } from "../format";

const MAX = 10000;

export default function Settings() {
  const [saved, setSaved] = useState<MerchantSettings | null>(null);
  const [value, setValue] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [status, setStatus] = useState<{ kind: "idle" | "saving" | "ok" | "error"; text?: string }>({ kind: "idle" });

  const load = () => {
    setLoadError(null);
    settingsApi
      .get()
      .then((s) => {
        setSaved(s);
        setValue(Number(s.settle_threshold).toFixed(2));
      })
      .catch((e) => setLoadError(e.message));
  };
  useEffect(load, []);

  const n = Number(value);
  const invalid =
    value.trim() === "" || !Number.isFinite(n) || n < 0 || n > MAX || !/^\d+(\.\d{1,2})?$/.test(value.trim())
      ? `Enter an amount from 0 to ${MAX.toLocaleString()}, with at most 2 decimals.`
      : null;
  const dirty = saved !== null && Number(saved.settle_threshold) !== n;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (invalid) return;
    setStatus({ kind: "saving" });
    try {
      const s = await settingsApi.save({ settle_threshold: n.toFixed(2) });
      setSaved(s);
      setValue(Number(s.settle_threshold).toFixed(2));
      setStatus({ kind: "ok", text: "Saved. New analyses use this threshold." });
    } catch (err) {
      setStatus({ kind: "error", text: (err as Error).message });
    }
  };

  return (
    <div className="mx-auto max-w-xl space-y-4">
      <h1 className="text-xl font-semibold">Settings</h1>
      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-semibold">Small-dispute settle threshold</h2>
        <p className="mt-1 text-sm text-slate-600">
          When a dispute is for less than this amount and the evidence is weak, the agent recommends settling
          (refunding) instead of gathering more evidence, because fighting would cost more time than it's worth.
        </p>
        <p className="mt-2 rounded-md bg-slate-50 p-2 text-xs text-slate-600">
          Nothing is refunded automatically: you still approve every response before it goes to PayPal. Strong
          cases are still recommended for fighting whatever the amount.
        </p>

        {loadError ? (
          <div className="mt-4 rounded-md bg-red-50 p-3 text-sm text-red-700" role="alert">
            Couldn't load settings: {loadError}{" "}
            <button onClick={load} className="ml-2 font-medium underline">
              Retry
            </button>
          </div>
        ) : !saved ? (
          <div className="mt-4 h-10 w-48 animate-pulse rounded bg-slate-100" aria-busy="true" aria-label="Loading settings" />
        ) : (
          <form onSubmit={save} className="mt-4 space-y-2" noValidate>
            <label htmlFor="threshold" className="block text-sm font-medium text-slate-700">
              Threshold ({saved.currency})
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-500">$</span>
                <input
                  id="threshold"
                  inputMode="decimal"
                  value={value}
                  aria-invalid={!!invalid}
                  aria-describedby="threshold-help"
                  onChange={(e) => {
                    setValue(e.target.value);
                    setStatus({ kind: "idle" });
                  }}
                  className={`w-40 rounded-md border py-2 pl-7 pr-3 text-sm focus:outline-none focus:ring-1 ${
                    invalid ? "border-red-400 focus:ring-red-400" : "border-slate-300 focus:border-indigo-500 focus:ring-indigo-500"
                  }`}
                />
              </div>
              <button
                type="submit"
                disabled={!!invalid || !dirty || status.kind === "saving"}
                className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-40"
              >
                {status.kind === "saving" ? "Saving…" : "Save"}
              </button>
              {dirty && (
                <button type="button" onClick={() => setValue(Number(saved.settle_threshold).toFixed(2))} className="text-sm text-slate-500 hover:underline">
                  Undo
                </button>
              )}
            </div>
            <p id="threshold-help" className={`text-xs ${invalid ? "text-red-600" : "text-slate-500"}`}>
              {invalid ?? `Disputes under ${money(n.toFixed(2), saved.currency)} with a weak case will be recommended for settling. Default $25.00.`}
            </p>
            {status.kind === "ok" && <p className="text-sm text-indigo-700" role="status">{status.text}</p>}
            {status.kind === "error" && <p className="text-sm text-red-700" role="alert">Couldn't save: {status.text}</p>}
            <p className="text-xs text-slate-400">Last changed {when(saved.updated_at)}</p>
          </form>
        )}
      </section>
      {saved && <RefundPolicy initial={saved.refund_policy ?? ""} onSaved={setSaved} />}
      {saved && <AutoInvite value={saved.auto_invite_troubleshooting} onSaved={setSaved} />}
    </div>
  );
}

const POLICY_MAX = 4000;

function RefundPolicy({ initial, onSaved }: { initial: string; onSaved: (s: MerchantSettings) => void }) {
  const [text, setText] = useState(initial);
  const [base, setBase] = useState(initial);
  const [status, setStatus] = useState<{ kind: "idle" | "saving" | "ok" | "error"; text?: string }>({ kind: "idle" });
  const dirty = text.trim() !== base.trim();
  const save = async () => {
    setStatus({ kind: "saving" });
    try {
      const s = await settingsApi.save({ refund_policy: text });
      setBase(s.refund_policy ?? "");
      setText(s.refund_policy ?? "");
      onSaved(s);
      setStatus({ kind: "ok", text: s.refund_policy ? "Saved. New investigations include it as evidence." : "Removed." });
    } catch (e) {
      setStatus({ kind: "error", text: (e as Error).message });
    }
  };
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-semibold">Refund policy</h2>
      <p className="mt-1 text-sm text-slate-600">
        Paste the refund and return policy your buyers see. PayPal expects it as evidence for "not as described"
        disputes, so it's attached to new cases automatically.
      </p>
      <label htmlFor="policy" className="sr-only">Refund policy</label>
      <textarea
        id="policy"
        value={text}
        maxLength={POLICY_MAX}
        onChange={(e) => {
          setText(e.target.value);
          setStatus({ kind: "idle" });
        }}
        placeholder="e.g. Returns accepted within 30 days of delivery for unused items in original packaging. Refunds are issued to the original payment method within 5 business days of receiving the return."
        className="mt-3 h-36 w-full rounded-md border border-slate-300 p-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
      />
      <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-slate-400">
          {text.length}/{POLICY_MAX}
        </span>
        <div className="flex items-center gap-2">
          {dirty && (
            <button onClick={() => setText(base)} className="text-sm text-slate-500 hover:underline">
              Undo
            </button>
          )}
          <button
            onClick={save}
            disabled={!dirty || status.kind === "saving"}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-40"
          >
            {status.kind === "saving" ? "Saving…" : "Save policy"}
          </button>
        </div>
      </div>
      {status.kind === "ok" && <p className="mt-2 text-sm text-indigo-700" role="status">{status.text}</p>}
      {status.kind === "error" && <p className="mt-2 text-sm text-red-700" role="alert">Couldn't save: {status.text}</p>}
    </section>
  );
}

function AutoInvite({ value, onSaved }: { value: boolean; onSaved: (s: MerchantSettings) => void }) {
  const [checked, setChecked] = useState(value); // flips at once; reverts if the save fails
  const [status, setStatus] = useState<{ kind: "idle" | "saving" | "ok" | "error"; text?: string }>({ kind: "idle" });
  const toggle = async (next: boolean) => {
    setChecked(next);
    setStatus({ kind: "saving" });
    try {
      const s = await settingsApi.save({ auto_invite_troubleshooting: next });
      onSaved(s);
      setChecked(s.auto_invite_troubleshooting);
      setStatus({ kind: "ok", text: s.auto_invite_troubleshooting ? "On. Eligible buyers will be invited automatically." : "Off." });
    } catch (e) {
      setChecked(!next);
      setStatus({ kind: "error", text: (e as Error).message });
    }
  };
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-semibold">Buyer troubleshooting</h2>
      <p className="mt-1 text-sm text-slate-600">
        When a buyer says an electrical or electronic item isn't working ("not as described"), DisputeShield can send them a
        private, guided troubleshooting page through the PayPal dispute thread. Many faults are fixed in a few minutes.
      </p>
      <label className="mt-3 flex items-start gap-3 text-sm">
        <input
          type="checkbox"
          checked={checked}
          disabled={status.kind === "saving"}
          onChange={(e) => toggle(e.target.checked)}
          className="mt-0.5 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
        />
        <span>
          <span className="font-medium">Invite eligible buyers automatically</span>
          <span className="block text-xs text-slate-500">
            Off: you invite them from the case page. PayPal only allows messages while a dispute is an inquiry.
          </span>
        </span>
      </label>
      {status.kind === "ok" && <p className="mt-2 text-sm text-indigo-700" role="status">{status.text}</p>}
      {status.kind === "error" && <p className="mt-2 text-sm text-red-700" role="alert">Couldn't save: {status.text}</p>}
    </section>
  );
}

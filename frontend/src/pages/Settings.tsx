import { useEffect, useState } from "react";
import { settingsApi, type AlertRow, type MerchantSettings } from "../api";
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
      {saved && <MoneyInputs saved={saved} onSaved={setSaved} />}
      {saved && <Alerts saved={saved} onSaved={setSaved} />}
      {saved && <McpAccess saved={saved} onChanged={() => settingsApi.get().then(setSaved)} />}
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

/** Phase 13: inputs of the money calculator (Options card). Fee sources are PayPal's public fee pages. */
function MoneyInputs({ saved, onSaved }: { saved: MerchantSettings; onSaved: (s: MerchantSettings) => void }) {
  const init = () => ({
    dispute_fee: Number(saved.dispute_fee ?? 15).toFixed(2),
    handling_minutes: String(saved.handling_minutes ?? 30),
    hourly_value: Number(saved.hourly_value ?? 25).toFixed(2),
    max_offer_pct: String(saved.max_offer_pct ?? 70),
  });
  const [v, setV] = useState(init);
  const [status, setStatus] = useState<{ kind: "idle" | "saving" | "ok" | "error"; text?: string }>({ kind: "idle" });
  const problem = moneyProblem(v);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (problem) return;
    setStatus({ kind: "saving" });
    try {
      const s = await settingsApi.save({
        dispute_fee: Number(v.dispute_fee).toFixed(2),
        handling_minutes: Number(v.handling_minutes),
        hourly_value: Number(v.hourly_value).toFixed(2),
        max_offer_pct: Number(v.max_offer_pct),
      });
      onSaved(s);
      setStatus({ kind: "ok", text: "Saved. New analyses use these numbers." });
    } catch (err) {
      setStatus({ kind: "error", text: (err as Error).message });
    }
  };
  const field = (key: keyof typeof v, labelText: string, help: string) => (
    <label className="block text-sm">
      <span className="font-medium text-slate-700">{labelText}</span>
      <input
        aria-label={labelText}
        inputMode="decimal"
        value={v[key]}
        onChange={(e) => {
          setV({ ...v, [key]: e.target.value });
          setStatus({ kind: "idle" });
        }}
        className="mt-1 w-32 rounded-md border border-slate-300 px-2 py-1.5"
      />
      <span className="mt-0.5 block text-xs text-slate-500">{help}</span>
    </label>
  );
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-semibold">Money: fees, your time and offers</h2>
      <p className="mt-1 text-sm text-slate-600">
        The Options card compares fighting, a full refund and a partial offer using these numbers.
      </p>
      <form onSubmit={save} className="mt-3 grid gap-3 sm:grid-cols-2" noValidate>
        {field("dispute_fee", `PayPal dispute fee (${saved.currency})`, "Charged when a buyer files a claim or chargeback.")}
        {field("handling_minutes", "Your minutes per fought dispute", "Time to gather and send evidence.")}
        {field("hourly_value", `Your hour is worth (${saved.currency})`, "Used to price your time.")}
        {field("max_offer_pct", "Max partial offer (% of the amount)", "10 to 90. Offers are never suggested above this.")}
        <div className="sm:col-span-2">
          {problem && <p className="text-xs text-red-700" role="alert">{problem}</p>}
          <button
            type="submit"
            disabled={!!problem || status.kind === "saving"}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {status.kind === "saving" ? "Saving…" : "Save"}
          </button>
          {status.kind === "ok" && <span className="ml-3 text-sm text-indigo-700" role="status">{status.text}</span>}
          {status.kind === "error" && <span className="ml-3 text-sm text-red-700" role="alert">Couldn't save: {status.text}</span>}
        </div>
      </form>
      <div className="mt-4 rounded-md bg-slate-50 p-3 text-xs text-slate-600" data-testid="fee-sources">
        <p className="font-medium text-slate-700">PayPal's dispute fees (from PayPal's fee pages, checked 3 Nov 2026)</p>
        <ul className="mt-1 space-y-0.5">
          {(saved.fee_sources ?? []).map((s) => (
            <li key={s.country}>
              {s.country}: standard {s.standard}, high volume {s.high_volume} ·{" "}
              <a href={s.url} target="_blank" rel="noreferrer" className="text-indigo-700 hover:underline">
                source
              </a>
            </li>
          ))}
        </ul>
        <p className="mt-1">
          PayPal says it "may charge a Dispute Fee when a buyer files a dispute claim or chargeback"; inquiries aren't
          mentioned. Check your PayPal account for the rate that applies to you.
        </p>
      </div>
    </section>
  );
}

export function moneyProblem(v: { dispute_fee: string; handling_minutes: string; hourly_value: string; max_offer_pct: string }): string | null {
  const num = (s: string) => (/^\d+(\.\d{1,2})?$/.test(s.trim()) ? Number(s) : NaN);
  if (Number.isNaN(num(v.dispute_fee))) return "Enter the dispute fee like 15.00.";
  if (!/^\d+$/.test(v.handling_minutes.trim()) || Number(v.handling_minutes) > 6000) return "Minutes must be a whole number (0 to 6000).";
  if (Number.isNaN(num(v.hourly_value))) return "Enter your hourly value like 25.00.";
  const pct = Number(v.max_offer_pct);
  if (!/^\d+$/.test(v.max_offer_pct.trim()) || pct < 10 || pct > 90) return "Max offer must be between 10 and 90 (%).";
  return null;
}

/** Phase 17: alerts by email / Slack. */
function Alerts({ saved, onSaved }: { saved: MerchantSettings; onSaved: (s: MerchantSettings) => void }) {
  const a = saved.alerts;
  const [email, setEmail] = useState(a?.email_to ?? "");
  const [status, setStatus] = useState<{ kind: "idle" | "saving" | "ok" | "error"; text?: string }>({ kind: "idle" });
  const [recent, setRecent] = useState<AlertRow[]>([]);
  useEffect(() => {
    settingsApi.alerts().then(setRecent, () => setRecent([]));
  }, []);
  if (!a) return null;
  const save = async (patch: Parameters<typeof settingsApi.save>[0], ok = "Saved.") => {
    setStatus({ kind: "saving" });
    try {
      onSaved(await settingsApi.save(patch));
      setStatus({ kind: "ok", text: ok });
    } catch (e) {
      setStatus({ kind: "error", text: (e as Error).message });
    }
  };
  const toggle = (key: "alert_on_proposed" | "alert_on_troubleshooting" | "alert_on_deadlines" | "alert_slack", value: boolean, label: string) => (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={value} disabled={status.kind === "saving"} onChange={(e) => save({ [key]: e.target.checked })}
        className="h-4 w-4 rounded border-slate-300 text-indigo-600" />
      {label}
    </label>
  );
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm" data-testid="alerts-settings">
      <h2 className="font-semibold">Alerts</h2>
      <p className="mt-1 text-sm text-slate-600">Get told where you already are. Each alert is sent once.</p>
      <div className="mt-3 space-y-2">
        {toggle("alert_on_proposed", a.proposed, "A new recommendation is waiting for your approval")}
        {toggle("alert_on_troubleshooting", a.troubleshooting, "A buyer finished the troubleshooting page")}
        {toggle("alert_on_deadlines", a.deadlines, "48 h and 12 h before a response is due, if nothing is approved")}
      </div>
      <form className="mt-4 flex flex-wrap items-end gap-2" onSubmit={(e) => (e.preventDefault(), save({ alert_email_to: email.trim() }, email.trim() ? "Alerts go to this address." : "Email alerts off."))}>
        <label className="text-sm">
          <span className="block font-medium text-slate-700">Email alerts to</span>
          <input type="email" aria-label="Alert email address" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@shop.com" className="mt-1 w-64 rounded-md border border-slate-300 px-2 py-1.5" />
        </label>
        <button className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white">Save</button>
      </form>
      <p className="mt-1 text-xs text-slate-500">
        {a.email_ready ? "Email is configured on the server." : "Email isn't configured on the server yet (SMTP_HOST): alerts are recorded below until it is."}
      </p>
      <div className="mt-3">
        {a.slack_ready ? toggle("alert_slack", a.slack, "Also post to Slack") : <p className="text-xs text-slate-500">Slack: set SLACK_WEBHOOK_URL on the server to post alerts to a channel.</p>}
      </div>
      {status.kind === "ok" && <p className="mt-2 text-sm text-indigo-700" role="status">{status.text}</p>}
      {status.kind === "error" && <p className="mt-2 text-sm text-red-700" role="alert">Couldn't save: {status.text}</p>}
      {recent.length > 0 && (
        <details className="mt-3 text-xs text-slate-600">
          <summary className="cursor-pointer">Recent alerts ({recent.length})</summary>
          <ul className="mt-1 space-y-0.5">
            {recent.map((r) => (
              <li key={r.key}>{r.subject} · {r.status}{r.channels?.length ? ` (${r.channels.join(", ")})` : ""}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/** Phase 17: a token for the MCP server (Claude Desktop etc.). Shown once; only its hash is stored. */
function McpAccess({ saved, onChanged }: { saved: MerchantSettings; onChanged: () => void }) {
  const [token, setToken] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm" data-testid="mcp-settings">
      <h2 className="font-semibold">Use from Claude (MCP)</h2>
      <p className="mt-1 text-sm text-slate-600">
        Let an AI assistant list your disputes, explain recommendations and, only with your exact confirmation, approve them. MCP endpoint:{" "}
        <code className="rounded bg-slate-100 px-1">{saved.mcp_url}</code>. Setup steps are in the README.
      </p>
      <p className="mt-2 text-xs text-slate-500">
        {!saved.mcp_token_created_at
          ? "No token: MCP access is off."
          : saved.mcp_token_expires_at && new Date(saved.mcp_token_expires_at) <= new Date()
            ? `The token expired ${when(saved.mcp_token_expires_at)}. Create a new one to use MCP again.`
            : `A token is active (created ${when(saved.mcp_token_created_at)}` +
              (saved.mcp_token_expires_at ? `, expires ${when(saved.mcp_token_expires_at)}).` : ").")}
      </p>
      {token && (
        <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900" role="status">
          Copy it now, it won't be shown again: <code className="break-all font-mono" data-testid="mcp-token">{token}</code>
        </div>
      )}
      {err && <p className="mt-2 text-xs text-red-700" role="alert">{err}</p>}
      <div className="mt-3 flex gap-2">
        <button
          onClick={async () => {
            setErr(null);
            try {
              setToken((await settingsApi.createMcpToken()).token);
              onChanged();
            } catch (e) {
              setErr((e as Error).message);
            }
          }}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white"
        >
          {saved.mcp_token_created_at ? "Create a new token (replaces the old one)" : "Create token"}
        </button>
        {saved.mcp_token_created_at && (
          <button onClick={async () => (await settingsApi.revokeMcpToken(), setToken(null), onChanged())} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            Revoke
          </button>
        )}
      </div>
    </section>
  );
}


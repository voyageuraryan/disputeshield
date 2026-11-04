// Phase 16: demo-mode Scenarios drawer (only when the server says demo mode is on: DEMO_MODE + ADMIN_TOKEN).
import { useState } from "react";
import { demoApi, type CreatedScenario, type Scenario } from "../api";

const TOKEN_KEY = "ds.adminToken";
const readToken = () => {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
};

export const checklistText = (s: Pick<Scenario, "title" | "checklist">) =>
  [s.title, ...s.checklist.map((c, i) => `${i + 1}. ${c.step}${c.link ? ` (${c.link})` : ""}`)].join("\n");

export default function Scenarios({ onClose }: { onClose: () => void }) {
  const [token, setToken] = useState(readToken);
  const [list, setList] = useState<Scenario[] | null>(null);
  const [made, setMade] = useState<CreatedScenario | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = async (t: string) => {
    setErr(null);
    try {
      setList(await demoApi.scenarios(t));
      try {
        sessionStorage.setItem(TOKEN_KEY, t);
      } catch {
        /* private mode: fine */
      }
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const create = async (key: string) => {
    setBusy(key);
    setErr(null);
    try {
      setMade(await demoApi.create(key, token));
      setCopied(false);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const shown = made ?? null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-900/40" onClick={onClose}>
      <aside role="dialog" aria-label="Demo scenarios" onClick={(e) => e.stopPropagation()} className="h-full w-full max-w-md overflow-y-auto bg-white p-5 shadow-xl">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="font-semibold">Demo scenarios</h2>
            <p className="text-xs text-slate-500">Each creates a real PayPal sandbox order and its store data. The buyer steps stay manual: PayPal has no API to open a buyer dispute.</p>
          </div>
          <button onClick={onClose} aria-label="Close" className="rounded px-2 py-1 text-slate-500 hover:bg-slate-100">✕</button>
        </div>
        {!list && (
          <form className="mt-4 space-y-2" onSubmit={(e) => (e.preventDefault(), load(token))}>
            <label className="block text-sm">
              <span className="font-medium">Admin token</span>
              <input type="password" value={token} onChange={(e) => setToken(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5" />
            </label>
            <button className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white">Show scenarios</button>
          </form>
        )}
        {err && <p className="mt-3 rounded-md bg-red-50 p-2 text-xs text-red-700" role="alert">{err}</p>}
        {list && !shown && (
          <ul className="mt-4 space-y-3">
            {list.map((s) => (
              <li key={s.key} className="rounded-md border border-slate-200 p-3">
                <p className="font-medium">{s.title}</p>
                <p className="text-xs text-slate-600">Expect: <span className="font-medium">{s.expect}</span>. {s.why}</p>
                <button disabled={busy !== null} onClick={() => create(s.key)} className="mt-2 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
                  {busy === s.key ? "Creating the sandbox order…" : "Create this scenario"}
                </button>
              </li>
            ))}
          </ul>
        )}
        {shown && (
          <div className="mt-4 space-y-3" data-testid="scenario-checklist">
            <p className="rounded-md bg-emerald-50 p-2 text-sm text-emerald-900">
              Created store order #{shown.order_id} with PayPal sandbox order {shown.paypal_order_id}. Now the buyer steps:
            </p>
            <ol className="list-decimal space-y-2 pl-5 text-sm">
              {shown.checklist.map((c) => (
                <li key={c.step}>
                  {c.step}
                  {c.link && (
                    <a href={c.link} target="_blank" rel="noreferrer" className="ml-1 text-indigo-700 hover:underline">open</a>
                  )}
                </li>
              ))}
            </ol>
            <div className="flex gap-2">
              <button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(checklistText(shown));
                    setCopied(true);
                  } catch {
                    setCopied(false);
                  }
                }}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm"
              >
                {copied ? "Copied" : "Copy checklist"}
              </button>
              <button onClick={() => setMade(null)} className="rounded-md px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">Back to scenarios</button>
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}

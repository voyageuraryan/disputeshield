import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, helpApi, type BuyerSession, type TSStepView } from "../api";
import { when } from "../format";

const KEY = (id: string) => `ds_help_${id}`;

function readToken(id: string): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get("t");
  try {
    if (fromUrl) {
      sessionStorage.setItem(KEY(id), fromUrl);
      // Keep the private token out of the address bar, history and screenshots; this tab remembers it.
      window.history.replaceState(null, "", window.location.pathname);
      return fromUrl;
    }
    return sessionStorage.getItem(KEY(id));
  } catch {
    return fromUrl; // storage blocked: the URL still works
  }
}

const ENDED_TEXT: Record<string, string> = {
  RESOLVED_FIXED: "Fixed. Thank you for trying the steps!",
  DEFECT_CONFIRMED: "Thank you. The seller now has everything they need and will contact you about what happens next.",
  SAFETY_STOP: "Stopped for your safety. Please don't use the item. The seller will contact you.",
  HANDED_TO_MERCHANT: "The seller is taking it from here and will reply to you directly.",
  EXPIRED: "This troubleshooting session has ended.",
};

const STEP_STATUS: Record<TSStepView["status"], string> = {
  PENDING: "",
  DONE_FIXED: "Fixed it",
  DONE_NOT_FIXED: "Done, still not working",
  SKIPPED: "Skipped",
  CANT_DO: "Couldn't do it",
};

const ACCEPT: Record<string, string> = {
  photo: "image/jpeg,image/png,image/webp,image/gif",
  screenshot: "image/png,image/jpeg,image/webp",
  video: "video/mp4,video/quicktime,video/webm",
};

const btn = "rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-40";

/** The buyer's private troubleshooting page (opened from the link in the PayPal dispute message). */
export default function Help() {
  const { sessionId = "" } = useParams();
  const [token] = useState(() => readToken(sessionId));
  const [data, setData] = useState<BuyerSession | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "chat" | "action">(null);
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!token) {
      setError({ status: 404, text: "This link is incomplete. Please open the full link from the seller's message." });
      return;
    }
    helpApi
      .get(sessionId, token)
      .then(setData)
      .catch((e: Error) => setError({ status: e instanceof ApiError ? e.status : 0, text: e.message }));
  }, [sessionId, token]);

  // Block body: an effect must return nothing or a cleanup function (newer Chromium returns a Promise here).
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [data?.messages.length]);

  // Every action returns the fresh session; a refusal shows inline and keeps the page usable.
  const act = async (kind: "chat" | "action", call: () => Promise<BuyerSession>) => {
    setBusy(kind);
    setNotice(null);
    try {
      setData(await call());
      return true;
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 403 || status === 404 || status === 410) setError({ status, text: (e as Error).message });
      else setNotice((e as Error).message.replace(/^\d+: /, ""));
      return false;
    } finally {
      setBusy(null);
    }
  };

  if (error || !data || !token) {
    return (
      <Shell>
        {error ? (
          <div role="alert" className={`rounded-lg p-4 text-sm ${error.status === 410 ? "bg-slate-100 text-slate-700" : "bg-red-50 text-red-800"}`}>
            {error.text}
          </div>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </Shell>
    );
  }

  const open = !data.ended;
  const sources = new Map(data.sources.map((s) => [s.id, s]));
  const send = async () => {
    const body = text.trim();
    if (!body) return;
    if (await act("chat", () => helpApi.send(sessionId, token, body))) setText("");
  };

  return (
    <Shell>
      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <h1 className="text-lg font-semibold">Let's get your {data.product_name} working</h1>
        {data.product_model && <p className="text-sm text-slate-500">Model {data.product_model}</p>}
        <p className="mt-1 text-xs text-slate-500">This page is private to you and works until {when(data.expires_at)}.</p>
        {!open && (
          <p role="status" className={`mt-3 rounded-md p-3 text-sm ${data.status === "SAFETY_STOP" ? "bg-red-50 text-red-800" : "bg-emerald-50 text-emerald-900"}`}>
            {ENDED_TEXT[data.status] ?? ENDED_TEXT.EXPIRED}
          </p>
        )}
      </section>

      {open && data.awaiting_fixed_confirm && (
        <section role="group" aria-label="Is it working now?" className="rounded-lg border border-emerald-300 bg-emerald-50 p-4">
          <p className="text-sm font-medium text-emerald-900">Is your {data.product_name} working now?</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button disabled={!!busy} onClick={() => act("action", () => helpApi.confirm(sessionId, token, true))} className={`${btn} bg-emerald-600 text-white hover:bg-emerald-700`}>
              Yes, it works now
            </button>
            <button disabled={!!busy} onClick={() => act("action", () => helpApi.confirm(sessionId, token, false))} className={`${btn} border border-slate-300 bg-white hover:bg-slate-50`}>
              No, still not working
            </button>
          </div>
        </section>
      )}

      {data.steps.length > 0 && (
        <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="font-semibold">Steps</h2>
          {data.plan_generic && <p className="mt-1 text-xs text-amber-700">General safety-checked steps, not specific to your model.</p>}
          <ol className="mt-3 space-y-3">
            {data.steps.map((s) => (
              <StepCard
                key={s.id}
                step={s}
                sources={s.source_ids.map((i) => sources.get(i)).filter((x) => !!x && !x.url.startsWith("internal:")) as { id: number; title: string | null; url: string }[]}
                open={open}
                busy={!!busy}
                onStatus={(status, label) => act("chat", () => helpApi.send(sessionId, token, `Step ${s.ordinal}: ${label}`, s.id, status))}
                onUpload={(file) => act("action", () => helpApi.uploadProof(sessionId, token, s.id, file))}
              />
            ))}
          </ol>
        </section>
      )}

      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-semibold">Chat</h2>
        <ul className="mt-2 max-h-[50vh] space-y-2 overflow-y-auto text-sm" aria-live="polite">
          {data.messages.map((m, i) => (
            <li key={i} className={`flex ${m.role === "buyer" ? "justify-end" : "justify-start"}`}>
              <span className={`max-w-[85%] whitespace-pre-line rounded-lg px-3 py-2 ${m.role === "buyer" ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-900"}`}>
                {m.role === "merchant" && <span className="block text-xs font-semibold">Seller</span>}
                {m.body}
              </span>
            </li>
          ))}
          {busy === "chat" && (
            <li className="text-xs text-slate-500" role="status">
              Thinking… looking up your model's support pages can take up to a minute.
            </li>
          )}
          <div ref={endRef} />
        </ul>
        {notice && <p role="alert" className="mt-2 rounded-md bg-amber-50 p-2 text-sm text-amber-900">{notice}</p>}
        {open && (
          <form
            className="mt-3 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              send();
            }}
          >
            <label htmlFor="msg" className="sr-only">Your message</label>
            <textarea
              id="msg"
              rows={2}
              maxLength={1500}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="Describe what's happening…"
              className="min-w-0 flex-1 rounded-md border border-slate-300 p-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
            <button type="submit" disabled={!!busy || !text.trim()} className={`${btn} self-end bg-indigo-600 text-white hover:bg-indigo-700`}>
              Send
            </button>
          </form>
        )}
      </section>

      {open && (
        <div className="text-center">
          <button
            disabled={!!busy}
            onClick={() => window.confirm("Stop troubleshooting and pass this to the seller?") && act("action", () => helpApi.handoff(sessionId, token))}
            className="text-sm font-medium text-indigo-700 underline disabled:opacity-40"
          >
            Talk to the seller
          </button>
        </div>
      )}
    </Shell>
  );
}

function StepCard({
  step: s,
  sources,
  open,
  busy,
  onStatus,
  onUpload,
}: {
  step: TSStepView;
  sources: { id: number; title: string | null; url: string }[];
  open: boolean;
  busy: boolean;
  onStatus: (status: TSStepView["status"], label: string) => void;
  onUpload: (file: File) => void;
}) {
  const pending = s.status === "PENDING";
  return (
    <li className={`rounded-md border p-3 text-sm ${pending ? "border-slate-200" : "border-slate-100 bg-slate-50"}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-medium">
          {s.ordinal}. {s.title}
        </p>
        {!pending && <span className="text-xs text-slate-500">{STEP_STATUS[s.status]}</span>}
      </div>
      {s.safety_level === "caution" && <p className="mt-1 text-xs font-medium text-amber-700">Take care: this step involves power or heat.</p>}
      <p className="mt-1 text-slate-700">{s.instruction}</p>
      {s.why && <p className="mt-1 text-xs text-slate-500">Why: {s.why}</p>}
      {sources.length > 0 && (
        <p className="mt-1 text-xs text-slate-500">
          Source:{" "}
          {sources.map((x, i) => (
            <span key={x.id}>
              {i > 0 && ", "}
              <a href={x.url} target="_blank" rel="noopener noreferrer nofollow" className="underline">
                {x.title || "link"}
              </a>
            </span>
          ))}
        </p>
      )}
      {open && pending && (
        <div className="mt-2 flex flex-wrap gap-2">
          <button disabled={busy} onClick={() => onStatus("DONE_FIXED", "that fixed it")} className={`${btn} border border-emerald-300 text-emerald-800 hover:bg-emerald-50`}>
            That fixed it
          </button>
          <button disabled={busy} onClick={() => onStatus("DONE_NOT_FIXED", "done, still not working")} className={`${btn} border border-slate-300 hover:bg-slate-50`}>
            Done, still not working
          </button>
          <button disabled={busy} onClick={() => onStatus("CANT_DO", "I can't do this step")} className={`${btn} text-slate-600 hover:underline`}>
            I can't do this
          </button>
        </div>
      )}
      {open && s.requires_proof && (
        <label className="mt-2 block text-xs text-slate-700">
          <span className="font-medium">Add a {s.proof_kind === "none" ? "photo" : s.proof_kind}</span>
          {s.proofs > 0 && <span className="ml-2 text-emerald-700">Uploaded ✓ ({s.proofs})</span>}
          <input
            type="file"
            accept={ACCEPT[s.proof_kind] ?? ACCEPT.photo}
            capture="environment"
            disabled={busy}
            aria-label={`Upload a ${s.proof_kind} for step ${s.ordinal}`}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onUpload(f);
              e.target.value = "";
            }}
            className="mt-1 block w-full text-xs"
          />
        </label>
      )}
    </li>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-2xl px-4 py-3 text-sm font-semibold">Product help</div>
      </header>
      <main className="mx-auto max-w-2xl space-y-4 px-4 py-6">
        {children}
        <p className="text-center text-xs text-slate-400">If it smells hot, sparks or smokes, unplug it, stop using it and tell us in the chat.</p>
      </main>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, helpApi, type BuyerSession, type ProofUpload, type StepAnswer, type TSMessageView } from "../api";
import { StepCard, btn } from "../help/StepCard";
import { mergeMessages, outcomeOf, progress, sourceChips, timeLeft, type Outcome } from "../help/logic";

const KEY = (id: string) => `ds_help_${id}`;
const FATAL = [403, 404, 410];

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

export const OUTCOME: Record<Exclude<Outcome, null>, { icon: string; title: string; body: string; tone: string }> = {
  fixed: {
    icon: "✓",
    title: "Great, it's working!",
    body: "Thank you for trying the steps. If you opened a PayPal case only because of this problem, you can close it from PayPal's Resolution Center whenever you're ready. That's entirely up to you; there's no rush.",
    tone: "border-emerald-300 bg-emerald-50 text-emerald-950",
  },
  defect: {
    icon: "📨",
    title: "We've sent your results to the seller",
    body: "You tried every step and showed us the problem. The seller now has your answers and uploads and will contact you about what happens next. Your PayPal case stays open.",
    tone: "border-indigo-300 bg-indigo-50 text-indigo-950",
  },
  safety: {
    icon: "⚠",
    title: "Stopped for your safety",
    body: "Please unplug the item and don't use it again. Keep it away from anything that could catch fire. The seller has been told and will contact you.",
    tone: "border-red-300 bg-red-50 text-red-950",
  },
  handed: {
    icon: "💬",
    title: "The seller is taking it from here",
    body: "We've passed everything you told us to the seller. They'll reply to you directly, usually in your PayPal case.",
    tone: "border-slate-300 bg-white text-slate-900",
  },
  expired: {
    icon: "⏱",
    title: "This help page has closed",
    body: "The time for troubleshooting has ended. Your PayPal case isn't affected, and the seller can still see anything you shared here.",
    tone: "border-slate-300 bg-slate-100 text-slate-900",
  },
};

/** Re-render every `ms` (the countdown). */
function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** Live updates over SSE, reconnecting with backoff. Returns whether a stream is connected (if not, the page polls). */
function useHelpStream(
  sessionId: string,
  token: string | null,
  enabled: boolean,
  lastId: () => number,
  onMessage: (m: TSMessageView) => void,
  onState: (s: Partial<BuyerSession>) => void,
  onFatal: (e: ApiError) => void,
) {
  const [live, setLive] = useState(false);
  const handlers = useRef({ onMessage, onState, onFatal, lastId });
  handlers.current = { onMessage, onState, onFatal, lastId };
  useEffect(() => {
    if (!enabled || !token) return;
    const ctrl = new AbortController();
    let delay = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const connect = async () => {
      if (ctrl.signal.aborted) return;
      try {
        setLive(true);
        await helpApi.stream(
          sessionId,
          token,
          handlers.current.lastId(),
          {
            onMessage: (m) => handlers.current.onMessage(m),
            onState: (s) => {
              delay = 1000;
              handlers.current.onState(s);
            },
          },
          ctrl.signal,
        );
      } catch (e) {
        if (ctrl.signal.aborted) return;
        setLive(false);
        if (e instanceof ApiError && [...FATAL, 409].includes(e.status)) return handlers.current.onFatal(e);
        delay = Math.min(delay * 2, 30000);
      }
      if (!ctrl.signal.aborted) timer = setTimeout(connect, delay);
    };
    connect();
    return () => {
      ctrl.abort();
      if (timer) clearTimeout(timer);
      setLive(false);
    };
  }, [sessionId, token, enabled]);
  return live;
}

/** The buyer's private troubleshooting page (opened from the link in the PayPal dispute message). Mobile first. */
export default function Help() {
  const { sessionId = "" } = useParams();
  const [token] = useState(() => readToken(sessionId));
  const [data, setData] = useState<BuyerSession | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [sheet, setSheet] = useState(false);
  const now = useNow(30000);

  const loaded = useRef(false);
  const load = useCallback(() => {
    if (!token) return;
    helpApi
      .get(sessionId, token)
      .then((d) => {
        loaded.current = true;
        setData((cur) => (cur ? { ...d, messages: mergeMessages(cur.messages, d.messages) } : d));
        setError(null);
      })
      .catch((e: Error) => {
        const status = e instanceof ApiError ? e.status : 0;
        // A background refresh that fails for a moment (5xx, offline) must not replace a working page; only
        // the first load or a refusal of the link itself (403/404/410) does.
        if (!loaded.current || FATAL.includes(status)) setError({ status, text: e.message });
      });
  }, [sessionId, token]);

  useEffect(() => {
    if (!token) {
      setError({ status: 404, text: "This link is incomplete. Please open the full link from the seller's message." });
      return;
    }
    load();
  }, [load, token]);

  /** A refusal that means the page can't be used any more (bad link, other device, closed) replaces the page;
   * anything else is shown inline and the page stays usable. */
  const failed = (e: Error) => {
    const status = e instanceof ApiError ? e.status : 0;
    if (FATAL.includes(status)) setError({ status, text: e.message });
    else setNotice(e.message.replace(/^\d+: /, ""));
  };
  const apply = (d: BuyerSession) => setData((cur) => ({ ...d, messages: mergeMessages(cur?.messages ?? [], d.messages) }));

  const live = useHelpStream(
    sessionId,
    token,
    !!data && !data.ended,
    () => Math.max(0, ...(data?.messages ?? []).map((m) => m.id)),
    (m) => setData((d) => (d ? { ...d, messages: mergeMessages(d.messages, [m]) } : d)),
    (s) => setData((d) => (d ? { ...d, ...s } : d)),
    (e) => setError({ status: e.status, text: e.message }),
  );

  // Without a live stream (blocked by a proxy, offline for a moment), poll while the assistant is working.
  useEffect(() => {
    if (live || !data?.agent_working) return;
    const t = setTimeout(load, 2500);
    return () => clearTimeout(t);
  }, [live, data, load]);

  const act = async (call: () => Promise<BuyerSession>) => {
    setBusy(true);
    setNotice(null);
    try {
      apply(await call());
    } catch (e) {
      failed(e as Error);
    } finally {
      setBusy(false);
    }
  };

  if (error || !data || !token) return <ErrorOrLoading error={error} onRetry={load} />;

  const over = timeLeft(data.expires_at, now).over;
  const open = !data.ended && !over;
  const p = progress(data.steps);
  const sources = new Map(data.sources.map((s) => [s.id, s]));
  const outcome: Outcome = over && !data.ended ? "expired" : outcomeOf(data);
  const working = !!data.agent_working;

  const send = async () => {
    const body = text.trim();
    if (!body || working) return;
    const pending: TSMessageView = { id: -Date.now(), role: "buyer", body, step_id: null, created_at: new Date().toISOString() };
    setData((d) => (d ? { ...d, agent_working: !d.seller_active, messages: [...d.messages, pending] } : d));
    setText("");
    setNotice(null);
    try {
      const r = await helpApi.send(sessionId, token, body);
      // the server says whether an assistant turn started (not while the seller is chatting)
      setData((d) => (d ? { ...d, agent_working: r.agent_working } : d));
      if (!live) load();
    } catch (e) {
      setData((d) => (d ? { ...d, agent_working: false, messages: d.messages.filter((m) => m.id !== pending.id) } : d));
      setText(body);
      failed(e as Error);
    }
  };

  const answer = (stepId: number, status: StepAnswer, note: string) => act(() => helpApi.step(sessionId, token, stepId, status, note));
  const uploaded = (r: ProofUpload) => apply(r);

  const stepList = data.steps.length > 0 && (
    <section aria-label="Troubleshooting steps" className="space-y-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">Steps</h2>
        <span className="text-sm text-slate-600">
          {p.done} of {p.total} done
        </span>
      </div>
      {data.plan_generic && <p className="rounded-lg bg-amber-50 p-2 text-sm text-amber-950">General safety-checked steps, not specific to your model.</p>}
      <ol className="space-y-3">
        {data.steps.map((s) => (
          <StepCard
            key={s.id}
            step={s}
            sources={s.source_ids.map((i) => sources.get(i)).filter((x): x is NonNullable<typeof x> => !!x)}
            open={open}
            busy={busy}
            current={open && p.current?.id === s.id}
            sessionId={sessionId}
            token={token}
            onAnswer={(status, note) => answer(s.id, status, note)}
            onUploaded={uploaded}
            onChanged={open ? apply : undefined}
          />
        ))}
      </ol>
    </section>
  );

  return (
    <div className="min-h-screen bg-slate-50 text-base text-slate-900">
      <Header data={data} now={now} />

      {data.steps.length > 0 && (
        <div className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 px-4 py-2 backdrop-blur lg:hidden">
          <button
            type="button"
            onClick={() => setSheet(true)}
            aria-haspopup="dialog"
            aria-expanded={sheet}
            className={`${btn} w-full justify-between border border-slate-300 bg-white text-slate-900`}
          >
            <span className="min-w-0 truncate">
              Steps {Math.min(p.done + (p.current ? 1 : 0), p.total)}/{p.total}
              {p.current && <span className="ml-2 font-normal text-slate-600">· {p.current.title}</span>}
            </span>
            <span aria-hidden>▴</span>
          </button>
        </div>
      )}

      <main className="mx-auto max-w-6xl px-4 py-4 lg:grid lg:grid-cols-5 lg:gap-6 lg:py-6">
        <section aria-label="Chat" className="flex min-w-0 flex-col lg:col-span-3">
          {outcome && <OutcomeCard outcome={outcome} />}
          {open && data.seller_active && (
            <p role="status" className="mb-3 rounded-xl border border-amber-300 bg-amber-50 p-3 text-amber-950">
              💬 The seller is chatting with you here now. The assistant is paused while they reply.
            </p>
          )}
          {open && data.awaiting_fixed_confirm && (
            <div role="group" aria-label="Is it working now?" className="mb-3 rounded-xl border border-emerald-300 bg-emerald-50 p-4">
              <p className="font-medium text-emerald-950">Is your {data.product_name} working now?</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                <button type="button" disabled={busy} onClick={() => act(() => helpApi.confirm(sessionId, token, true))} className={`${btn} bg-emerald-700 text-white hover:bg-emerald-800`}>
                  Yes, it works now
                </button>
                <button type="button" disabled={busy} onClick={() => act(() => helpApi.confirm(sessionId, token, false))} className={`${btn} border border-slate-400 bg-white hover:bg-slate-50`}>
                  No, still not working
                </button>
              </div>
            </div>
          )}

          <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
            <h2 className="border-b border-slate-100 px-4 py-3 font-semibold">Let's get your {data.product_name} working</h2>
            <ol aria-live="polite" aria-relevant="additions" aria-label="Messages" className="space-y-3 px-3 py-4 sm:px-4 lg:max-h-[60vh] lg:overflow-y-auto">
              {data.messages.map((m) => (
                <Bubble key={m.id} m={m} chips={sourceChips(m, data.steps, data.sources)} />
              ))}
              {working && open && (
                <li role="status" className="flex items-center gap-2 text-sm text-slate-600">
                  <span className="flex gap-1" aria-hidden>
                    <span className="h-2 w-2 animate-bounce rounded-full bg-slate-500 [animation-delay:-0.3s]" />
                    <span className="h-2 w-2 animate-bounce rounded-full bg-slate-500 [animation-delay:-0.15s]" />
                    <span className="h-2 w-2 animate-bounce rounded-full bg-slate-500" />
                  </span>
                  The assistant is typing…{data.steps.length === 0 && " Looking up your model can take up to a minute."}
                </li>
              )}
              <ScrollAnchor dep={data.messages.length + (working ? 1 : 0)} />
            </ol>
            {notice && (
              <p role="alert" className="mx-4 mb-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-950">
                {notice}
              </p>
            )}
            {open && (
              <form
                className="sticky bottom-0 flex gap-2 rounded-b-xl border-t border-slate-100 bg-white p-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  send();
                }}
              >
                <label htmlFor="msg" className="sr-only">
                  Your message
                </label>
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
                  className="min-w-0 flex-1 resize-none rounded-lg border border-slate-400 p-2 text-base focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600"
                />
                <button type="submit" disabled={working || !text.trim()} className={`${btn} self-end bg-indigo-700 text-white hover:bg-indigo-800`}>
                  Send
                </button>
              </form>
            )}
          </div>

          {open && (
            <div className="mt-3 text-center">
              <button
                type="button"
                disabled={busy}
                onClick={() => window.confirm("Stop troubleshooting and pass this to the seller?") && act(() => helpApi.handoff(sessionId, token))}
                className="min-h-[44px] px-2 text-base font-medium text-indigo-800 underline disabled:opacity-50"
              >
                Talk to the seller
              </button>
            </div>
          )}
          <p className="mt-4 text-center text-sm text-slate-600">If it smells hot, sparks or smokes: unplug it, stop using it and tell us in the chat.</p>
        </section>

        {stepList && (
          <aside className="hidden lg:col-span-2 lg:block">
            <div className="sticky top-4 max-h-[calc(100vh-2rem)] overflow-y-auto pb-4">{stepList}</div>
          </aside>
        )}
      </main>

      {sheet && stepList && <BottomSheet onClose={() => setSheet(false)}>{stepList}</BottomSheet>}
    </div>
  );
}

function Header({ data, now }: { data: BuyerSession; now: number }) {
  const left = timeLeft(data.expires_at, now);
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto max-w-6xl px-4 py-3">
        <div className="flex items-center justify-between gap-2 text-sm">
          <span className="font-semibold text-slate-800">{data.store_name || "Product help"}</span>
          <span className="text-slate-600">🔒 Private help page</span>
        </div>
        <h1 className="mt-2 text-xl font-semibold leading-tight">Help with your order{data.order_ref ? ` #${data.order_ref}` : ""}</h1>
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <span aria-hidden className="flex h-14 w-14 shrink-0 items-center justify-center rounded-xl bg-slate-100 text-3xl">
              {data.product_image || "📦"}
            </span>
            <div className="min-w-0 flex-1">
              <p className="font-medium">{data.product_name}</p>
              {data.product_model && <p className="text-sm text-slate-600">Model {data.product_model}</p>}
              {data.paypal_case_id && <p className="break-words text-sm text-slate-600">PayPal case {data.paypal_case_id}</p>}
            </div>
          </div>
          {!data.ended && (
            <span className={`self-start rounded-full px-3 py-1 text-sm font-medium sm:self-center ${left.urgent ? "bg-amber-100 text-amber-950" : "bg-slate-100 text-slate-800"}`}>
              <span className="sr-only">Time left before this page closes: </span>⏱ {left.text}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}

function Bubble({ m, chips }: { m: TSMessageView; chips: ReturnType<typeof sourceChips> }) {
  if (m.role === "system") return <li className="text-center text-sm italic text-slate-600">{m.body}</li>;
  const buyer = m.role === "buyer";
  const seller = m.role === "merchant";
  return (
    <li className={`flex ${buyer ? "justify-end" : "justify-start"}`}>
      <div className={`flex max-w-[88%] flex-col ${buyer ? "items-end" : "items-start"}`}>
        <span className="mb-0.5 text-xs font-medium text-slate-600">{buyer ? "You" : seller ? "Seller" : "Assistant"}</span>
        <span
          className={`whitespace-pre-line break-words rounded-2xl px-3 py-2 ${
            buyer ? "rounded-br-sm bg-indigo-700 text-white" : seller ? "rounded-bl-sm border border-amber-300 bg-amber-50 text-slate-900" : "rounded-bl-sm bg-slate-100 text-slate-900"
          } ${m.id < 0 ? "opacity-70" : ""}`}
        >
          {m.body}
        </span>
        {chips.length > 0 && (
          <ul className="mt-1 flex flex-wrap gap-1.5" aria-label="Sources">
            {chips.map((c) => (
              <li key={c.id}>
                <a
                  href={c.url}
                  target="_blank"
                  rel="noopener noreferrer nofollow"
                  className="inline-flex max-w-[16rem] items-center gap-1 rounded-full border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-800 hover:bg-slate-50"
                >
                  <span className="truncate">{c.title || c.domain}</span>
                  {c.title && <span className="text-slate-600">· {c.domain}</span>}
                  <span aria-hidden>↗</span>
                  <span className="sr-only">(opens in a new tab)</span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  );
}

function ScrollAnchor({ dep }: { dep: number }) {
  const ref = useRef<HTMLLIElement>(null);
  // Block body: an effect must return nothing or a cleanup function (newer Chromium returns a Promise here).
  useEffect(() => {
    ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [dep]);
  return <li ref={ref} aria-hidden className="h-px" />;
}

function OutcomeCard({ outcome }: { outcome: Exclude<Outcome, null> }) {
  const o = OUTCOME[outcome];
  const ref = useRef<HTMLDivElement>(null);
  // Bring the result into view (the buyer may be scrolled down in the chat or the steps) and move focus to it.
  useEffect(() => {
    ref.current?.focus?.({ preventScroll: true });
    ref.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }, [outcome]);
  return (
    <div ref={ref} tabIndex={-1} role="status" className={`mb-3 scroll-mt-20 rounded-xl border p-4 focus:outline-none ${o.tone}`}>
      <p className="flex items-center gap-2 text-lg font-semibold">
        <span aria-hidden>{o.icon}</span>
        {o.title}
      </p>
      <p className="mt-1">{o.body}</p>
    </div>
  );
}

function BottomSheet({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const back = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", key);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", key);
      document.body.style.overflow = overflow;
      back?.focus?.();
    };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 lg:hidden">
      <div className="absolute inset-0 bg-slate-900/50" onClick={onClose} aria-hidden />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Troubleshooting steps"
        tabIndex={-1}
        className="absolute inset-x-0 bottom-0 max-h-[85vh] overflow-y-auto rounded-t-2xl bg-slate-50 px-4 pb-6 pt-2 shadow-xl focus:outline-none"
      >
        <div className="sticky top-0 z-10 -mx-4 mb-2 flex justify-end bg-slate-50 px-4 py-2">
          <button type="button" onClick={onClose} className={`${btn} border border-slate-300 bg-white text-slate-900`}>
            Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function ErrorOrLoading({ error, onRetry }: { error: { status: number; text: string } | null; onRetry: () => void }) {
  const expired = error?.status === 410;
  return (
    <div className="min-h-screen bg-slate-50 text-base text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-2xl px-4 py-3 text-sm font-semibold">🔒 Private help page</div>
      </header>
      <main className="mx-auto max-w-2xl px-4 py-6">
        {!error ? (
          <p role="status" className="text-slate-600">
            Loading…
          </p>
        ) : (
          <div role="alert" className={`rounded-xl border p-4 ${expired ? "border-slate-300 bg-slate-100" : "border-red-200 bg-red-50 text-red-950"}`}>
            {expired && <p className="text-lg font-semibold">⏱ This help page has closed</p>}
            <p className={expired ? "mt-1" : ""}>{error.text}</p>
            {error.status === 0 && (
              <button type="button" onClick={onRetry} className={`${btn} mt-3 bg-indigo-700 text-white`}>
                Try again
              </button>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

import { useEffect, useRef } from "react";
import type { Step } from "./api";
import type { LiveState } from "./useDisputeEvents";

// One small line icon per agent (24x24 stroke paths).
const ICONS: Record<string, { label: string; path: string }> = {
  orchestrator: { label: "Orchestrator", path: "M5 12h14M13 6l6 6-6 6" },
  investigator: { label: "Investigator", path: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4" },
  analyst: { label: "Analyst", path: "M5 20V10M12 20V4M19 20v-7" },
  resolution: { label: "Resolution", path: "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6" },
  merchant: { label: "You", path: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0" },
  executor: { label: "PayPal call", path: "M3 11l18-8-8 18-2-8z" },
};
const FALLBACK = { label: "Agent", path: "M12 12h.01" };

const Icon = ({ agent }: { agent: string }) => {
  const i = ICONS[agent] ?? FALLBACK;
  return (
    <span title={i.label} className="relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white ring-1 ring-slate-300">
      <svg viewBox="0 0 24 24" className="h-4 w-4 text-slate-600" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d={i.path} />
      </svg>
    </span>
  );
};

// Older runs logged short codes; show them in words.
const CODES: Record<string, string> = {
  run_started: "Agent run started",
  run_pending: "Run paused (agents not built yet at the time)",
  run_skipped: "Run skipped: the dispute is already resolved",
  run_failed: "Agent run failed",
};
const say = (s: Step) => {
  if (s.action === "run_failed" && s.detail && typeof s.detail.error === "string") return `Agent run failed: ${s.detail.error}`;
  return CODES[s.action] ?? s.action;
};

const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayOf = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

const STATE_TEXT: Record<LiveState, { text: string; dot: string }> = {
  connecting: { text: "Connecting…", dot: "bg-slate-300" },
  live: { text: "Live", dot: "bg-indigo-600 animate-pulse" },
  reconnecting: { text: "Reconnecting…", dot: "bg-amber-400" },
  polling: { text: "Live stream unavailable · refreshing every 5 s", dot: "bg-slate-400" },
};

export default function Timeline({ steps, state }: { steps: Step[] | null; state: LiveState }) {
  const box = useRef<HTMLOListElement>(null);
  const pinned = useRef(true); // follow new steps unless the user scrolled up to read

  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [steps?.length]);

  const st = STATE_TEXT[state];
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" aria-label="Agent timeline">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Agent timeline</h2>
        <span className="flex items-center gap-1.5 text-xs text-slate-500" aria-live="polite">
          <span className={`h-2 w-2 rounded-full ${st.dot}`} aria-hidden />
          {st.text}
        </span>
      </div>

      {steps === null ? (
        <ul className="space-y-3" aria-busy="true" aria-label="Loading timeline">
          {[0, 1, 2].map((i) => (
            <li key={i} className="flex animate-pulse items-center gap-3">
              <span className="h-7 w-7 rounded-full bg-slate-100" />
              <span className="h-3 flex-1 rounded bg-slate-100" />
            </li>
          ))}
        </ul>
      ) : steps.length === 0 ? (
        <p className="text-sm text-slate-500">No agent activity yet. Steps appear here live as the agents work on this case.</p>
      ) : (
        <ol
          ref={box}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
          className="relative max-h-[28rem] space-y-3 overflow-y-auto pr-1"
        >
          <span className="absolute bottom-2 left-[13px] top-2 w-px bg-slate-200" aria-hidden />
          {steps.map((s, i) => {
            const newDay = i === 0 || dayOf(steps[i - 1].created_at) !== dayOf(s.created_at);
            return (
              <li key={s.id} className="relative flex gap-3">
                <Icon agent={s.agent} />
                <div className="min-w-0 flex-1 pt-0.5">
                  <p className={`text-sm leading-snug ${s.action === "run_failed" ? "text-red-700" : "text-slate-800"}`}>{say(s)}</p>
                  <p className="text-xs text-slate-400">
                    <time dateTime={s.created_at}>{newDay ? `${dayOf(s.created_at)}, ` : ""}{time(s.created_at)}</time>
                    {" · "}
                    {(ICONS[s.agent] ?? FALLBACK).label}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

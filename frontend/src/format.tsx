import { useEffect, useState } from "react";
import type { DisputeRow } from "./api";

export const LABELS: Record<string, string> = {
  MERCHANDISE_OR_SERVICE_NOT_RECEIVED: "Item not received",
  MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: "Not as described",
  UNAUTHORISED: "Unauthorized",
  WAITING_FOR_SELLER_RESPONSE: "Needs your response",
  WAITING_FOR_BUYER_RESPONSE: "Waiting for buyer",
  UNDER_REVIEW: "PayPal reviewing",
  RESOLVED: "Resolved",
  OPEN: "Open",
  INQUIRY: "Inquiry",
  CHARGEBACK: "Chargeback",
  PRE_ARBITRATION: "Pre-arbitration",
  ARBITRATION: "Arbitration",
  GATHER_MORE: "Gather more",
  FIGHT: "Fight",
  SETTLE: "Settle",
  SETTLE_PARTIAL: "Partial refund offer",
  REPLACEMENT: "Replacement offer",
  provide_evidence: "send evidence",
  provide_supporting_info: "send supporting info",
  accept_claim: "refund the buyer",
  make_offer: "make an offer",
  appeal: "appeal",
  escalate: "escalate to a PayPal claim",
  send_message: "message the buyer",
  acknowledge_return_item: "acknowledge a return",
  delivery_confirmation: "Delivery confirmation",
  tracking: "Tracking",
  invoice: "PayPal payment record",
  order: "Store order",
  listing: "Product listing",
  buyer_message: "Buyer message",
  buyer_history: "Buyer history",
  refund_policy: "Refund policy",
};

export const label = (v: string | null | undefined) => (v ? LABELS[v] ?? v.replaceAll("_", " ").toLowerCase() : "-");

export function money(amount: string, currency: string) {
  const n = Number(amount);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { style: "currency", currency }) : `${amount} ${currency}`;
}

export function when(iso: string | null | undefined) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function day(iso: string | null | undefined) {
  if (!iso) return "-";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/** Countdown to a deadline; urgent under 48 h (shown in red). */
export function dueIn(iso: string | null | undefined, now = Date.now()): { text: string; urgent: boolean; ms: number } | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return { text: "overdue", urgent: true, ms };
  const mins = Math.floor(ms / 60_000);
  const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
  const text = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
  return { text, urgent: ms < 48 * 3_600_000, ms };
}

/** Parse a backend timestamp; SQLite drops the offset, so a bare one is UTC. */
export function utcMs(iso: string): number {
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
}

/** "Based on data fetched 7 min ago" (Phase 11 freshness line). */
export function fetchedAgo(iso: string | null | undefined, now = Date.now()): string | null {
  if (!iso) return null;
  const mins = Math.max(0, Math.floor((now - utcMs(iso)) / 60_000));
  if (Number.isNaN(mins)) return null;
  const ago = mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : mins < 1440 ? `${Math.floor(mins / 60)} h ${mins % 60} min ago` : `${Math.floor(mins / 1440)} d ago`;
  return `Based on data fetched ${ago}`;
}

const SOURCE_WORDS: Record<string, string> = {
  "paypal:dispute": "the PayPal dispute",
  "paypal:capture": "the PayPal transaction",
  "paypal:trackers": "PayPal tracking",
};
export const sourceLabel = (s: string) => SOURCE_WORDS[s] ?? s;

/** Re-render every `ms` so countdowns tick. */
export function useNow(ms = 30_000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** What the merchant needs to know about a case, in one phrase. */
export function caseStatus(r: Pick<DisputeRow, "status" | "agent_status" | "latest_decision">): { text: string; tone: Tone } {
  const d = r.latest_decision;
  if (r.status === "RESOLVED" || r.agent_status === "RESOLVED") return { text: "Resolved", tone: "muted" };
  if (d?.status === "EXECUTED") return { text: "Response sent", tone: "muted" };
  if (d?.status === "FAILED" || r.agent_status === "FAILED") return { text: "Response failed", tone: "danger" };
  if (r.agent_status === "NEEDS_HUMAN") return { text: "Needs your review", tone: "accent" };
  if (d?.status === "PROPOSED") return { text: "Awaiting your approval", tone: "accent" };
  if (d?.status === "REJECTED") return { text: "You rejected it", tone: "muted" };
  if (!d) return { text: "Agents working…", tone: "muted" };
  return { text: label(r.status), tone: "muted" };
}

/** Phase 9D: the buyer's troubleshooting session has ended (queue badge + toast). */
export const TS_FINISHED_TEXT: Record<string, string> = {
  RESOLVED_FIXED: "fixed",
  DEFECT_CONFIRMED: "defect confirmed",
  SAFETY_STOP: "stopped for safety",
  EXPIRED: "ended without a result",
  HANDED_TO_MERCHANT: "handed to you",
};
export function tsBadge(r: Pick<DisputeRow, "troubleshooting" | "status">): { text: string; title: string; tone: Tone } | null {
  const outcome = r.troubleshooting ? TS_FINISHED_TEXT[r.troubleshooting] : undefined;
  if (!outcome || r.status === "RESOLVED") return null;
  return {
    text: "Buyer finished troubleshooting",
    title: `Buyer troubleshooting: ${outcome}`,
    tone: r.troubleshooting === "SAFETY_STOP" || r.troubleshooting === "DEFECT_CONFIRMED" ? "danger" : "accent",
  };
}

export type Tone = "accent" | "muted" | "danger" | "solid";

const TONES: Record<Tone, string> = {
  accent: "bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-200",
  solid: "bg-indigo-600 text-white",
  muted: "bg-slate-100 text-slate-600",
  danger: "bg-red-50 text-red-700 ring-1 ring-inset ring-red-200",
};

export const Chip = ({ text, tone = "muted", title }: { text: string; tone?: Tone; title?: string }) => (
  <span title={title} className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>
    {text}
  </span>
);

/** Recommendation chip: the recommended action is the one accent element. */
export const ActionChip = ({ action }: { action: string }) => (
  <Chip text={label(action)} tone={action === "GATHER_MORE" ? "accent" : "solid"} />
);

export const Due = ({ iso, now }: { iso: string | null | undefined; now?: number }) => {
  const d = dueIn(iso, now);
  if (!d) return <span className="text-slate-400">-</span>;
  return (
    <span className={d.urgent ? "font-semibold text-red-600" : "text-slate-700"} title={iso ? new Date(iso).toLocaleString() : ""}>
      {d.text}
    </span>
  );
};

export function addressLines(a: Record<string, string> | null | undefined) {
  if (!a) return [];
  return [a.address_line_1, a.address_line_2, [a.admin_area_2, a.admin_area_1, a.postal_code].filter(Boolean).join(", "), a.country_code].filter(
    Boolean,
  ) as string[];
}

/** How a FIGHT reaches PayPal at the case's current stage (mirrors build_action_request on the server). */
export function fightRoute(allowed: string[]): "evidence" | "supporting" | "escalate" | null {
  if (allowed.includes("provide_evidence")) return "evidence";
  if (allowed.includes("provide_supporting_info")) return "supporting";
  if (allowed.includes("escalate")) return "escalate";
  return null;
}

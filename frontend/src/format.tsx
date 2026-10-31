export const LABELS: Record<string, string> = {
  MERCHANDISE_OR_SERVICE_NOT_RECEIVED: "Item not received",
  MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: "Not as described",
  UNAUTHORISED: "Unauthorized",
  WAITING_FOR_SELLER_RESPONSE: "Needs your response",
  WAITING_FOR_BUYER_RESPONSE: "Waiting for buyer",
  UNDER_REVIEW: "PayPal reviewing",
  RESOLVED: "Resolved",
  OPEN: "Open",
};

export const label = (v: string | null | undefined) => (v ? LABELS[v] ?? v.replaceAll("_", " ").toLowerCase() : "-");

export function money(amount: string, currency: string) {
  const n = Number(amount);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { style: "currency", currency }) : `${amount} ${currency}`;
}

export function when(iso: string | null) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function dueIn(iso: string | null) {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "overdue";
  const h = Math.floor(ms / 3_600_000);
  return h >= 48 ? `${Math.floor(h / 24)} days left` : `${h} h left`;
}

export const ACTION_STYLE: Record<string, string> = {
  FIGHT: "bg-emerald-100 text-emerald-800",
  SETTLE: "bg-amber-100 text-amber-800",
  GATHER_MORE: "bg-sky-100 text-sky-800",
};

export const STATUS_STYLE: Record<string, string> = {
  PROPOSED: "bg-indigo-100 text-indigo-800",
  APPROVED: "bg-indigo-100 text-indigo-800",
  EXECUTED: "bg-emerald-100 text-emerald-800",
  FAILED: "bg-red-100 text-red-800",
  REJECTED: "bg-slate-200 text-slate-700",
  SUPERSEDED: "bg-slate-100 text-slate-500",
};

export const Badge = ({ text, style }: { text: string; style?: string }) => (
  <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${style ?? "bg-slate-100 text-slate-700"}`}>{text}</span>
);

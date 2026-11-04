// Phase 18: GET /api/disputes + troubleshooting windows -> Bryntum Scheduler rows (resources) and bars (events).
// Pure and tested (deadlines.test.ts). Bars per open dispute:
//   gather    dispute opened -> response sent (or the deadline, while nothing is sent yet)      indigo
//   ts        buyer troubleshooting window: invited -> finished (or the link's expiry)          teal
//   due       seller response deadline: a zero-length milestone                               red
//   review    PayPal reviewing: response sent -> now + REVIEW_DAYS (PayPal gives no end date)  grey
import type { DisputeRow, TroubleshootingWindow } from "../api";
import { label, money, utcMs } from "../format";

export const COLORS = { gather: "#4f46e5", ts: "#0d9488", due: "#dc2626", review: "#94a3b8" };
export const REVIEW_DAYS = 7;
const DAY = 86_400_000;

export type Resource = { id: string; name: string; reason: string; amount: string };
export type Bar = { id: string; resourceId: string; name: string; startDate: Date; endDate: Date; eventColor: string; kind: "gather" | "ts" | "due" | "review"; draggable: false; resizable: false };

const at = (iso: string | null | undefined) => (iso ? new Date(utcMs(iso)) : null);

export function toSchedule(rows: DisputeRow[], windows: TroubleshootingWindow[], now = Date.now()) {
  const open = rows.filter((r) => r.status !== "RESOLVED");
  const resources: Resource[] = open.map((r) => ({
    id: r.paypal_dispute_id,
    name: r.paypal_dispute_id,
    reason: label(r.reason),
    amount: money(r.amount, r.currency),
  }));
  const bars: Bar[] = [];
  const bar = (r: DisputeRow, kind: Bar["kind"], name: string, s: Date, e: Date) =>
    bars.push({ id: `${r.paypal_dispute_id}:${kind}`, resourceId: r.paypal_dispute_id, name, startDate: s, endDate: e, eventColor: COLORS[kind], kind, draggable: false, resizable: false });
  for (const r of open) {
    const opened = at(r.opened_at);
    const due = at(r.seller_response_due);
    const sent = at(r.responded_at);
    const start = opened ?? (due ? new Date(Math.min(due.getTime(), now) - DAY) : new Date(now - DAY));
    const gatherEnd = sent ?? due ?? new Date(Math.max(now, start.getTime()) + DAY);
    if (gatherEnd > start) bar(r, "gather", sent ? "Evidence gathered, response sent" : "Gathering evidence", start, gatherEnd);
    const w = windows.find((x) => x.dispute_id === r.paypal_dispute_id);
    const ws = at(w?.start), we = at(w?.end);
    if (w && ws && we && we > ws) bar(r, "ts", `Buyer troubleshooting (${w.status.replaceAll("_", " ").toLowerCase()})`, ws, we);
    if (due) bar(r, "due", "Seller response due", due, due);
    if (sent) bar(r, "review", "PayPal reviewing", sent, new Date(Math.max(now, sent.getTime()) + REVIEW_DAYS * DAY));
  }
  const times = bars.flatMap((b) => [b.startDate.getTime(), b.endDate.getTime()]);
  const startDate = new Date(Math.min(now - 3 * DAY, ...times) - DAY);
  const endDate = new Date(Math.max(now + 14 * DAY, ...times) + 2 * DAY);
  return { resources, bars, startDate, endDate };
}

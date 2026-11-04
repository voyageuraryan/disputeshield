// Phase 16: the 5-step guided tour for judges (queue -> live timeline -> evidence -> options -> approve). Pure parts.
import type { DisputeRow } from "../api";

export type TourStep = { key: string; where: "queue" | "case"; selector: string; fallback?: string; title: string; body: string };

export const TOUR: TourStep[] = [
  { key: "queue", where: "queue", selector: "[data-tour=queue]", title: "1 · Every dispute, as it happens",
    body: "PayPal sends a webhook the moment a buyer opens a dispute; it lands here in seconds with the deadline counting down. Click a row to open the case." },
  { key: "timeline", where: "case", selector: "[data-tour=timeline]", title: "2 · The agents work live",
    body: "Investigator, analyst and resolution steps stream onto this timeline: every PayPal call, every rule, every AI answer, so nothing is a black box." },
  { key: "evidence", where: "case", selector: "[data-tour=evidence]", title: "3 · Evidence, checked by rules",
    body: "Fixed rules decide which evidence this dispute reason needs. The AI can only cite items that exist here, and you can add what's missing." },
  { key: "options", where: "case", selector: "[data-tour=options]", fallback: "[data-tour=recommendation]", title: "4 · Options, in money",
    body: "Fight, full refund or a partial offer, each with the expected net outcome, so the recommendation is a business decision, not a guess." },
  { key: "approve", where: "case", selector: "[data-tour=approve]", fallback: "[data-tour=recommendation]", title: "5 · Nothing moves without you",
    body: "Nothing is sent to PayPal until you approve, and the code checks that approval again before any money-moving call." },
];

/** The case the tour opens: one waiting for approval, else the newest open one. */
export function pickTourCase(rows: Pick<DisputeRow, "paypal_dispute_id" | "status" | "latest_decision">[]): string | null {
  const waiting = rows.find((r) => r.status !== "RESOLVED" && r.latest_decision?.status === "PROPOSED");
  const open = rows.find((r) => r.status !== "RESOLVED");
  return (waiting ?? open ?? rows[0])?.paypal_dispute_id ?? null;
}

export const stepPath = (step: TourStep, caseId: string | null) => (step.where === "queue" ? "/queue" : caseId ? `/cases/${encodeURIComponent(caseId)}` : "/queue");

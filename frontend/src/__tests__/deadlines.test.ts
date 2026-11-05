import { describe, expect, it } from "vitest";
import type { DisputeRow, TroubleshootingWindow } from "../api";
import { COLORS, REVIEW_DAYS, toSchedule } from "../deadlines/mapping";

const NOW = Date.parse("2026-11-04T12:00:00Z");
const DAY = 86_400_000;
const row = (over: Partial<DisputeRow>): DisputeRow => ({
  id: 1, paypal_dispute_id: "PP-1", reason: "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", stage: "CHARGEBACK", status: "WAITING_FOR_SELLER_RESPONSE",
  amount: "89.00", currency: "USD", seller_response_due: "2026-11-08T12:00:00Z", agent_status: null, order_id: 1, buyer_email: null,
  updated_at: null, latest_decision: null, opened_at: "2026-11-01T12:00:00Z", responded_at: null, ...over,
});

describe("Deadlines mapping", () => {
  it("one row per OPEN dispute, with reason and amount", () => {
    const { resources } = toSchedule([row({}), row({ paypal_dispute_id: "PP-R", status: "RESOLVED" })], [], NOW);
    expect(resources).toEqual([{ id: "PP-1", name: "PP-1", reason: "Item not received", amount: "$89.00" }]);
  });
  it("not answered yet: gathering until the deadline + a red due milestone", () => {
    const { bars } = toSchedule([row({})], [], NOW);
    expect(bars.map((b) => b.kind)).toEqual(["gather", "due"]);
    const [gather, due] = bars;
    expect([gather.startDate.toISOString(), gather.endDate.toISOString(), gather.name]).toEqual(["2026-11-01T12:00:00.000Z", "2026-11-08T12:00:00.000Z", "Gathering evidence"]);
    expect(due.startDate.getTime()).toBe(due.endDate.getTime()); // zero length = milestone
    expect(due.eventColor).toBe(COLORS.due);
    expect(bars.every((b) => b.resourceId === "PP-1" && !b.draggable && !b.resizable)).toBe(true);
  });
  it("answered: gathering ends at the response, then PayPal review (estimated)", () => {
    const { bars } = toSchedule([row({ responded_at: "2026-11-03T09:00:00" })], [], NOW); // bare SQLite time = UTC
    const gather = bars.find((b) => b.kind === "gather")!;
    const review = bars.find((b) => b.kind === "review")!;
    expect(gather.endDate.toISOString()).toBe("2026-11-03T09:00:00.000Z");
    expect(gather.name).toBe("Evidence gathered, response sent");
    expect(review.startDate.toISOString()).toBe("2026-11-03T09:00:00.000Z");
    expect(review.endDate.getTime()).toBe(NOW + REVIEW_DAYS * DAY);
  });
  it("PayPal reviewing / waiting on the buyer with nothing asked of the seller: no 'Gathering evidence' bar", () => {
    const review = toSchedule([row({ status: "UNDER_REVIEW", seller_response_due: null })], [], NOW).bars;
    expect(review.map((b) => [b.kind, b.name])).toEqual([["review", "PayPal reviewing"]]);
    const buyer = toSchedule([row({ status: "WAITING_FOR_BUYER_RESPONSE", seller_response_due: null })], [], NOW).bars;
    expect(buyer.map((b) => b.name)).toEqual(["Waiting for the buyer"]);
  });
  it("adds the buyer troubleshooting window", () => {
    const w: TroubleshootingWindow[] = [{ dispute_id: "PP-1", status: "AWAITING_PROOF", start: "2026-11-02T10:00:00Z", end: "2026-11-06T12:00:00Z", finished: false }];
    const ts = toSchedule([row({})], w, NOW).bars.find((b) => b.kind === "ts")!;
    expect([ts.name, ts.eventColor, ts.startDate.toISOString()]).toEqual(["Buyer troubleshooting (awaiting proof)", COLORS.ts, "2026-11-02T10:00:00.000Z"]);
    expect(toSchedule([row({})], [{ ...w[0], end: null }], NOW).bars.some((b) => b.kind === "ts")).toBe(false);
  });
  it("unknown open time or deadline still gives a sensible bar; unique ids", () => {
    const { bars, startDate, endDate } = toSchedule([row({ opened_at: null, seller_response_due: null }), row({ paypal_dispute_id: "PP-2" })], [], NOW);
    expect(bars.filter((b) => b.resourceId === "PP-1").map((b) => b.kind)).toEqual(["gather"]);
    expect(new Set(bars.map((b) => b.id)).size).toBe(bars.length);
    expect(startDate.getTime()).toBeLessThan(NOW - 3 * DAY);
    expect(endDate.getTime()).toBeGreaterThan(NOW + 14 * DAY);
  });
});

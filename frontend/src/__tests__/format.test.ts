import { describe, expect, it } from "vitest";
import { caseStatus, dueIn, fightRoute, label, money } from "../format";

const H = 3_600_000;
const NOW = Date.parse("2026-11-01T12:00:00Z");
const at = (ms: number) => new Date(NOW + ms).toISOString();

describe("dueIn (countdown, red under 48h)", () => {
  it("is null without a deadline", () => expect(dueIn(null, NOW)).toBeNull());
  it("shows days+hours and is not urgent at 3 days", () => expect(dueIn(at(73 * H), NOW)).toMatchObject({ text: "3d 1h", urgent: false }));
  it("is urgent just under 48h", () => expect(dueIn(at(47.9 * H), NOW)?.urgent).toBe(true));
  it("is not urgent at exactly 48h", () => expect(dueIn(at(48 * H), NOW)?.urgent).toBe(false));
  it("shows hours+minutes under a day", () => expect(dueIn(at(5 * H + 7 * 60_000), NOW)?.text).toBe("5h 7m"));
  it("shows minutes under an hour", () => expect(dueIn(at(9 * 60_000), NOW)?.text).toBe("9m"));
  it("is overdue and urgent after the deadline", () => expect(dueIn(at(-1), NOW)).toMatchObject({ text: "overdue", urgent: true }));
});

describe("caseStatus (plain-language status)", () => {
  const row = (status: string, agent_status: string | null, d?: { status: string }) => ({
    status,
    agent_status,
    latest_decision: d ? { id: 1, recommendation: "FIGHT", confidence: "high", ...d } : null,
  });
  it("resolved wins", () => expect(caseStatus(row("RESOLVED", "PROPOSED", { status: "PROPOSED" })).text).toBe("Resolved"));
  it("awaiting approval", () => expect(caseStatus(row("WAITING_FOR_SELLER_RESPONSE", "PROPOSED", { status: "PROPOSED" }))).toEqual({ text: "Awaiting your approval", tone: "accent" }));
  it("failed is danger", () => expect(caseStatus(row("OPEN", "FAILED", { status: "FAILED" })).tone).toBe("danger"));
  it("sent", () => expect(caseStatus(row("UNDER_REVIEW", "EXECUTED", { status: "EXECUTED" })).text).toBe("Response sent"));
  it("needs review", () => expect(caseStatus(row("OPEN", "NEEDS_HUMAN", { status: "SUPERSEDED" })).text).toBe("Needs your review"));
  it("no decision yet", () => expect(caseStatus(row("OPEN", null)).text).toBe("Agents working…"));
});

describe("labels and money", () => {
  it("maps PayPal codes", () => {
    expect(label("MERCHANDISE_OR_SERVICE_NOT_RECEIVED")).toBe("Item not received");
    expect(label("UNAUTHORISED")).toBe("Unauthorized");
    expect(label("accept_claim")).toBe("refund the buyer");
  });
  it("falls back to readable text", () => expect(label("SOME_NEW_CODE")).toBe("some new code"));
  it("formats currency", () => expect(money("61.5", "USD")).toMatch(/61\.50/));
});

describe("fightRoute", () => {
  it("prefers evidence, then supporting info, then escalation", () => {
    expect(fightRoute(["accept_claim", "provide_evidence", "escalate"])).toBe("evidence");
    expect(fightRoute(["provide_supporting_info", "accept_claim"])).toBe("supporting");
    expect(fightRoute(["accept_claim", "make_offer", "escalate", "send_message"])).toBe("escalate");
    expect(fightRoute(["accept_claim"])).toBeNull();
  });
});

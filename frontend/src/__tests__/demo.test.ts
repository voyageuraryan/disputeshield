import { describe, expect, it } from "vitest";
import { checklistText } from "../demo/Scenarios";
import { pickTourCase, stepPath, TOUR } from "../demo/tour";
import { codeRef, pctText, trustCards } from "../trust/logic";
import type { EvalReport } from "../api";

describe("guided tour", () => {
  it("has the 5 steps in order: queue -> timeline -> evidence -> options -> approve", () => {
    expect(TOUR.map((s) => s.key)).toEqual(["queue", "timeline", "evidence", "options", "approve"]);
    expect(TOUR.every((s) => s.selector.startsWith("[data-tour="))).toBe(true);
  });
  it("opens a case waiting for approval, else an open one", () => {
    const rows = [
      { paypal_dispute_id: "R", status: "RESOLVED", latest_decision: null },
      { paypal_dispute_id: "O", status: "OPEN", latest_decision: { id: 1, recommendation: "FIGHT", confidence: "high", status: "EXECUTED" } },
      { paypal_dispute_id: "W", status: "OPEN", latest_decision: { id: 2, recommendation: "SETTLE", confidence: "low", status: "PROPOSED" } },
    ];
    expect(pickTourCase(rows)).toBe("W");
    expect(pickTourCase(rows.slice(0, 2))).toBe("O");
    expect(pickTourCase([])).toBeNull();
    expect(stepPath(TOUR[0], "W")).toBe("/queue");
    expect(stepPath(TOUR[2], "W")).toBe("/cases/W");
    expect(stepPath(TOUR[2], null)).toBe("/queue");
  });
});

describe("trust cards", () => {
  const m = (pass_rate: number | null, average: number | null, measured = 10, passed = 8, errored = 0) => ({ kind: "x", measured, passed, errored, average, pass_rate });
  const report: EvalReport = {
    generated: "2026-11-02T18:47:08Z", model_under_test: ["groq"], judge: "j", goldens: 51, ran: 51,
    gates: { passed: false, failures: ["ActionCorrectness 78% < 90%"] },
    metrics: { ActionCorrectness: m(0.784, 0.784), Faithfulness: m(1, 0.954), NoOverclaim: m(1, 1), "PII Leakage": m(0.9, 0.9), "Tool Correctness": m(1, 1) },
  };
  it("headline cards: accuracy, faithfulness (average), safety (the weakest), tools", () => {
    const cards = trustCards(report);
    expect(cards.map((c) => [c.key, c.value])).toEqual([["accuracy", 0.784], ["faithfulness", 0.954], ["safety", 0.9], ["tools", 1]]);
    expect(cards[2].detail).toBe("weakest of NoOverclaim, PII Leakage");
    expect(pctText(0.784)).toBe("78%");
  });
  it("code references point at backend files", () => {
    expect(codeRef("app.api.decisions:_claim")).toEqual({ file: "backend/app/api/decisions.py", name: "_claim" });
  });
});

describe("scenario checklist", () => {
  it("copies as numbered text with links", () => {
    expect(checklistText({ title: "Strong INR", checklist: [{ step: "Pay", link: "https://x" }, { step: "Wait" }] })).toBe(
      "Strong INR\n1. Pay (https://x)\n2. Wait",
    );
  });
});

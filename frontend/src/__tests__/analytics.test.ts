import { describe, expect, it } from "vitest";
import type { Fact, QueryResult } from "../api";
import { calibrationData, chipPrefix, chipText, formatMetric, groupFacts, pct, resultChart, SERIES, withFilterValue, withoutFilter } from "../analytics/logic";

const f = (over: Partial<Fact>): Fact => ({
  id: 1, ref: "R", is_demo: true, reason: "X", reason_label: "Not received", stage: null, amount: 100, currency: "USD",
  outcome: "SELLER_FAVOUR", outcome_code: null, final_action: "FIGHT", recommended_action: "FIGHT", followed: true,
  overridden: false, won: true, contested: true, correct: true, rule_score: 0.8, offer_amount: null, money_protected: 100,
  money_refunded: 0, created_at: null, response_at: null, resolved_at: null, due_at: null, month: "2026-10",
  hours_to_response: 5, before_deadline: true, ...over,
});

describe("grouping the fact table (client-side: row grouping is an Enterprise feature)", () => {
  const facts = [
    f({}),
    f({ won: false, correct: false, outcome: "BUYER_FAVOUR", money_protected: 0, money_refunded: 50 }),
    f({ reason_label: "Not as described", contested: false, correct: null, won: false, outcome: "OFFER_ACCEPTED", money_protected: 60, money_refunded: 40, month: "2026-09" }),
  ];
  it("aggregates win rate over contested cases, money and accuracy", () => {
    const g = groupFacts(facts, "reason");
    expect(g.map((r) => r.key)).toEqual(["Not as described", "Not received"]);
    expect(g[1]).toEqual({ key: "Not received", cases: 2, won: 1, contested: 2, win_rate: 0.5, protected: 100, refunded: 50, accuracy: 0.5 });
    expect(g[0].win_rate).toBeNull(); // nothing contested: no rate, not 0%
  });
  it("groups by month and outcome", () => {
    expect(groupFacts(facts, "month").map((r) => [r.key, r.cases])).toEqual([["2026-09", 1], ["2026-10", 2]]);
    expect(groupFacts(facts, "outcome").map((r) => r.key)).toEqual(["BUYER_FAVOUR", "OFFER_ACCEPTED", "SELLER_FAVOUR"]);
  });
});

describe("query chips", () => {
  const q = { filters: [{ column: "amount", op: "lt", value: "25" }, { column: "created_at", op: "last_days", value: "30" }], group_by: [], metric: "count", chart: "table" as const, title: "" };
  it("reads like a sentence", () => {
    expect(chipText(q.filters[0])).toBe("Amount < 25");
    expect(chipText(q.filters[1])).toBe("Opened in the last 30 days");
    expect(chipPrefix(q.filters[0])).toBe("Amount <");
    expect(chipText({ column: "final_action", op: "in", value: "SETTLE,SETTLE_PARTIAL" })).toBe("Action sent is one of settle,settle partial");
  });
  it("edits and removes filters without touching the rest", () => {
    expect(withoutFilter(q, 0).filters).toEqual([q.filters[1]]);
    expect(withFilterValue(q, 0, "50").filters[0]).toEqual({ column: "amount", op: "lt", value: "50" });
    expect(q.filters[0].value).toBe("25"); // pure
  });
});

describe("formatting and charts", () => {
  it("formats metrics", () => {
    expect(formatMetric("win_rate", 0.567)).toBe("57%");
    expect(formatMetric("money_protected", 4148.05)).toBe("$4,148");
    expect(formatMetric("count", 12)).toBe("12");
    expect(formatMetric("accuracy", null)).toBe("—");
    expect(pct(null)).toBe("—");
  });
  const res = (chart: "bar" | "line" | "pie" | "table", kind: QueryResult["kind"] = "groups"): QueryResult => ({
    dataset: "demo", kind, columns: ["reason", "win_rate", "cases"], matched: 3, value: 0.6,
    query: { filters: [], group_by: ["reason"], metric: "win_rate", chart, title: "" },
    rows: [{ reason: "Not received", win_rate: 0.68, cases: 50 }, { reason: "Unauthorised", win_rate: null, cases: 2 }],
  });
  it("builds bar/line/pie options in the app colours with keyed axes (AG Charts 14)", () => {
    const bar = resultChart(res("bar")) as Record<string, any>;
    expect(bar.series[0]).toMatchObject({ type: "bar", xKey: "reason", yKey: "win_rate", fill: SERIES.primary });
    expect(Object.keys(bar.axes)).toEqual(["x", "y"]); // an array is silently ignored by AG Charts 14
    expect(bar.title.text).toBe("Win rate by reason");
    expect(bar.data[1].win_rate).toBeNull();
    expect((resultChart(res("line")) as Record<string, any>).series[0].type).toBe("line");
    expect((resultChart(res("pie")) as Record<string, any>).series[0].type).toBe("pie");
  });
  it("no chart for a table or a single value", () => {
    expect(resultChart(res("table"))).toBeNull();
    expect(resultChart(res("bar", "value"))).toBeNull();
  });
  it("calibration data uses band midpoints", () => {
    expect(calibrationData([{ lower: 0.7, upper: 0.85, n: 4, wins: 3, raw: 0.75, default: 0.65, p: 0.7 }])).toEqual([
      { score: 0.78, fitted: 0.7, starting: 0.65, observed: 0.75, n: 4 },
    ]);
  });
});

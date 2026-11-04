// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MoneyOption } from "../api";
import { acceptProbability, offerNotePreview, offerProblems, partialNet, sendable, withOffer } from "../offers/logic";
import { OptionsCard } from "../pages/CaseDetail";
import { moneyProblem } from "../pages/Settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const OPTS: MoneyOption[] = [
  { action: "FIGHT", label: "Fight", available: true, net: "-195.25", chance: "0.15", offer_amount: null, why: "15% chance" },
  { action: "SETTLE", label: "Full refund", available: true, net: "-200.00", chance: "1", offer_amount: null, why: "refund 200" },
  { action: "SETTLE_PARTIAL", label: "Partial refund offer", available: true, net: "-146.85", chance: "0.42", offer_amount: "80.00", why: "offer 80" },
  { action: "REPLACEMENT", label: "Send a replacement", available: false, net: null, chance: null, offer_amount: null, why: "n/a" },
];

describe("offer math mirrors backend/app/rules/money.py", () => {
  it("accept probability", () => {
    expect(acceptProbability(44.5, 89)).toBeCloseTo(0.5);
    expect(acceptProbability(0, 89)).toBeCloseTo(0.1);
    expect(acceptProbability(200, 89)).toBeCloseTo(0.95); // clamped
  });
  it("partial net equals the backend test value", () => {
    expect(partialNet(44.5, 89, -100.9)).toBe(-72.7); // test_settle_and_partial_net
  });
  it("re-ranks the options when the merchant edits the offer", () => {
    const { rows, best } = withOffer(OPTS, "80", 200);
    expect(rows.find((r) => r.action === "SETTLE_PARTIAL")?.net).toBe("-146.85"); // = backend partial_net(80, 200, -195.25)
    expect(best).toBe("SETTLE_PARTIAL");
    const greedy = withOffer(OPTS, "190", 200); // = backend partial_net(190, 200, -195.25)
    expect(greedy.rows.find((r) => r.action === "SETTLE_PARTIAL")?.net).toBe("-190.74");
    const strongFight = withOffer(OPTS.map((o) => (o.action === "FIGHT" ? { ...o, net: "-40.00" } : o)), "190", 200);
    expect(strongFight.best).toBe("FIGHT");
    expect(greedy.rows.find((r) => r.action === "SETTLE_PARTIAL")?.why).toMatch(/offer 190.00 \(95% of the amount\)/);
  });
});

describe("offer bounds and note", () => {
  const bounds = { min: "20.00", max: "140.00" };
  it("accepts amounts within 10%..max", () => {
    expect(offerProblems("20.00", 200, bounds)).toEqual([]);
    expect(offerProblems("140", 200, bounds)).toEqual([]);
  });
  it("explains what's wrong", () => {
    expect(offerProblems("19.99", 200, bounds)[0]).toMatch(/at least 20.00/);
    expect(offerProblems("150", 200, bounds)[0]).toMatch(/at most 140.00/);
    expect(offerProblems("250", 200, bounds)).toContain("The offer can't be more than the disputed amount.");
    expect(offerProblems("1.234", 200, bounds)[0]).toMatch(/like 25.00/);
    expect(offerProblems("50", 200, null)[0]).toMatch(/doesn't allow a partial offer/);
  });
  it("the note preview matches the server's template", () => {
    expect(offerNotePreview("SETTLE_PARTIAL", "PP-D-1", "60", "200.00", "USD")).toBe(
      "We're sorry this order didn't work out. To resolve dispute PP-D-1, we offer a refund of 60.00 USD (the disputed amount is 200.00 USD).",
    );
    expect(offerNotePreview("REPLACEMENT", "PP-D-1", "", "200.00", "USD")).toMatch(/replacement item at no cost/);
  });
  it("only actions PayPal allows are sendable", () => {
    expect(sendable({ can_fight: true, can_accept_claim: true, can_offer: true, can_replace: false, can_settle: true })).toEqual([
      "FIGHT", "SETTLE", "SETTLE_PARTIAL",
    ]);
    expect(sendable({ can_fight: false, can_accept_claim: false, can_offer: false, can_replace: true, can_settle: true })).toEqual(["REPLACEMENT"]);
  });
});

describe("settings: money inputs", () => {
  const ok = { dispute_fee: "15.00", handling_minutes: "30", hourly_value: "25", max_offer_pct: "70" };
  it("validates", () => {
    expect(moneyProblem(ok)).toBeNull();
    expect(moneyProblem({ ...ok, dispute_fee: "-1" })).toMatch(/dispute fee/);
    expect(moneyProblem({ ...ok, handling_minutes: "2.5" })).toMatch(/whole number/);
    expect(moneyProblem({ ...ok, max_offer_pct: "95" })).toMatch(/between 10 and 90/);
  });
});

describe("Options card", () => {
  let root: Root | null = null;
  let el: HTMLDivElement;
  afterEach(() => {
    act(() => root?.unmount());
    root = null;
    el?.remove();
  });
  it("shows net outcomes, marks the best and lets the merchant choose", () => {
    const onPick = vi.fn();
    el = document.createElement("div");
    document.body.appendChild(el);
    root = createRoot(el);
    act(() => root!.render(<OptionsCard options={OPTS} offer="80.00" amount={200} currency="USD" chosen="SETTLE_PARTIAL" canPick onPick={onPick} />));
    const items = [...el.querySelectorAll("[data-testid=options] li")];
    expect(items).toHaveLength(4);
    expect(items[2].textContent).toContain("best");
    expect(items[2].textContent).toContain("42% chance");
    expect(items[3].className).toContain("opacity-60"); // replacement not allowed here
    const choose = [...el.querySelectorAll("button")].find((b) => b.textContent === "Choose fight") as HTMLButtonElement;
    act(() => choose.click());
    expect(onPick).toHaveBeenCalledWith("FIGHT");
  });
});

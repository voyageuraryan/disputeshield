// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { fetchedAgo, sourceLabel, utcMs } from "../format";
import { Freshness } from "../pages/CaseDetail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.parse("2026-11-03T12:00:00Z");

describe("fetchedAgo (Phase 11 freshness line)", () => {
  it("is null without a time", () => expect(fetchedAgo(null, NOW)).toBeNull());
  it("reads a bare (SQLite) timestamp as UTC", () => {
    expect(utcMs("2026-11-03T11:53:00")).toBe(Date.parse("2026-11-03T11:53:00Z"));
    expect(fetchedAgo("2026-11-03T11:53:00", NOW)).toBe("Based on data fetched 7 min ago");
  });
  it("keeps an explicit offset", () => expect(fetchedAgo("2026-11-03T12:53:00+01:00", NOW)).toBe("Based on data fetched 7 min ago"));
  it("says just now under a minute", () => expect(fetchedAgo("2026-11-03T11:59:30Z", NOW)).toBe("Based on data fetched just now"));
  it("uses hours and days for older data", () => {
    expect(fetchedAgo("2026-11-03T09:45:00Z", NOW)).toBe("Based on data fetched 2 h 15 min ago");
    expect(fetchedAgo("2026-11-01T09:00:00Z", NOW)).toBe("Based on data fetched 2 d ago");
  });
  it("names sources in words", () => {
    expect(sourceLabel("paypal:capture")).toBe("the PayPal transaction");
    expect(sourceLabel("store")).toBe("store");
  });
});

describe("Freshness banner on the recommendation", () => {
  let root: Root | null = null;
  let el: HTMLDivElement;
  afterEach(() => { act(() => root?.unmount()); root = null; el?.remove(); });
  const show = (d: { data_as_of?: string | null; stale_sources?: string[] | null }) => {
    el = document.createElement("div"); document.body.appendChild(el);
    root = createRoot(el);
    act(() => root!.render(<Freshness d={d} />));
    return el;
  };

  it("shows the fetch time and no banner when everything was refreshed", () => {
    const c = show({ data_as_of: new Date(Date.now() - 5 * 60_000).toISOString(), stale_sources: [] });
    expect(c.querySelector("[data-testid=data-freshness]")?.textContent).toBe("Based on data fetched 5 min ago");
    expect(c.querySelector("[data-testid=stale-banner]")).toBeNull();
  });
  it("lists stale sources in an amber banner", () => {
    const c = show({ data_as_of: new Date().toISOString(), stale_sources: ["paypal:capture", "paypal:trackers"] });
    const banner = c.querySelector("[data-testid=stale-banner]");
    expect(banner?.textContent).toContain("Couldn't refresh the PayPal transaction, PayPal tracking from PayPal");
    expect(banner?.className).toContain("amber");
  });
  it("renders nothing for an old decision without freshness data", () => {
    expect(show({}).innerHTML).toBe("");
  });
});

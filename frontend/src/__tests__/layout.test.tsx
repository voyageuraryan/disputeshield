// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mode = { value: "paypal" as "paypal" | "off" };
const demo = { enabled: false };
vi.mock("../api", () => ({
  authApi: { config: () => Promise.resolve({ mode: mode.value }), me: () => Promise.resolve({ name: "Shop", email: null }) },
  demoApi: { status: () => Promise.resolve({ enabled: demo.enabled }) },
  disputesApi: { list: () => Promise.resolve([]) },
  newDisputesUrl: () => "/api/events/new",
}));
vi.mock("../NewDisputeAlerts", () => ({ default: () => null }));

import Layout from "../Layout";
import { clearToken, setToken } from "../auth";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const tok = (exp: number) => `${btoa(JSON.stringify({ kind: "session", exp })).replace(/=+$/, "")}.sig`;

async function buttons(): Promise<string[]> {
  const el = document.createElement("div");
  const root = createRoot(el);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={["/store"]}>
        <Layout />
      </MemoryRouter>,
    );
  });
  const labels = [...el.querySelectorAll("header button")].map((b) => b.textContent ?? "");
  act(() => root.unmount());
  return labels;
}

async function navLabels(): Promise<string[]> {
  const el = document.createElement("div");
  const root = createRoot(el);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={["/store"]}>
        <Layout />
      </MemoryRouter>,
    );
  });
  const labels = [...el.querySelectorAll("nav a")].map((a) => a.textContent ?? "");
  act(() => root.unmount());
  return labels;
}

describe("top navigation", () => {
  afterEach(() => {
    clearToken();
    mode.value = "paypal";
  });
  it("shows only the Store to buyers / signed-out visitors", async () => {
    expect(await navLabels()).toEqual(["Store", "About"]);
  });
  it("shows Disputes and Settings to a signed-in merchant", async () => {
    setToken(tok(Date.now() / 1000 + 3600));
    expect(await navLabels()).toEqual(["Disputes", "Orders", "Deadlines", "Analytics", "Trust", "Store", "Settings", "About"]);
  });
  it("hides merchant tabs when the session has expired", async () => {
    setToken(tok(Date.now() / 1000 - 1));
    expect(await navLabels()).toEqual(["Store", "About"]);
  });
  it("Scenarios only when the server says demo mode is on; the tour for any merchant", async () => {
    mode.value = "off";
    demo.enabled = false;
    expect(await buttons()).toEqual(["Take the tour"]);
    demo.enabled = true;
    expect(await buttons()).toEqual(["Take the tour", "Scenarios"]);
    demo.enabled = false;
  });
  it("no demo tools for buyers / signed-out visitors", async () => {
    demo.enabled = true;
    expect(await buttons()).not.toContain("Scenarios");
    demo.enabled = false;
  });
  it("shows everything when login is switched off (local dev)", async () => {
    mode.value = "off";
    expect(await navLabels()).toEqual(["Disputes", "Orders", "Deadlines", "Analytics", "Trust", "Store", "Settings", "About"]);
  });
});

// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mode = { value: "paypal" as "paypal" | "off" };
vi.mock("../api", () => ({
  authApi: { config: () => Promise.resolve({ mode: mode.value }), me: () => Promise.resolve({ name: "Shop", email: null }) },
  newDisputesUrl: () => "/api/events/new",
}));
vi.mock("../NewDisputeAlerts", () => ({ default: () => null }));

import Layout from "../Layout";
import { clearToken, setToken } from "../auth";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const tok = (exp: number) => `${btoa(JSON.stringify({ kind: "session", exp })).replace(/=+$/, "")}.sig`;

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
    expect(await navLabels()).toEqual(["Store"]);
  });
  it("shows Disputes and Settings to a signed-in merchant", async () => {
    setToken(tok(Date.now() / 1000 + 3600));
    expect(await navLabels()).toEqual(["Disputes", "Orders", "Store", "Settings"]);
  });
  it("hides merchant tabs when the session has expired", async () => {
    setToken(tok(Date.now() / 1000 - 1));
    expect(await navLabels()).toEqual(["Store"]);
  });
  it("shows everything when login is switched off (local dev)", async () => {
    mode.value = "off";
    expect(await navLabels()).toEqual(["Disputes", "Orders", "Store", "Settings"]);
  });
});

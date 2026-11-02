// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import NewDisputeAlerts, { NEW_DISPUTE_EVENT } from "../NewDisputeAlerts";
import { ToastProvider } from "../Toast";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

class FakeES {
  static last: FakeES;
  listeners: Record<string, (e: { data: string }) => void> = {};
  constructor(public url: string) {
    FakeES.last = this;
  }
  addEventListener(name: string, fn: (e: { data: string }) => void) {
    this.listeners[name] = fn;
  }
  close() {}
}

let el: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("EventSource", FakeES);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ mode: "off" }), { status: 200 })));
  el = document.createElement("div");
  document.body.appendChild(el);
  root = createRoot(el);
});
afterEach(() => {
  act(() => root.unmount());
  el.remove();
  vi.unstubAllGlobals();
});

describe("app-wide alerts", () => {
  it("a buyer finishing troubleshooting shows a toast linking to the case and refreshes the queue", async () => {
    const refreshed = vi.fn();
    window.addEventListener(NEW_DISPUTE_EVENT, refreshed);
    await act(async () =>
      root.render(
        <MemoryRouter>
          <ToastProvider>
            <NewDisputeAlerts />
          </ToastProvider>
        </MemoryRouter>,
      ),
    );
    await act(async () => new Promise((r) => setTimeout(r, 0)));
    expect(FakeES.last.url).toContain("/api/events/disputes");
    await act(async () =>
      FakeES.last.listeners.troubleshooting({
        data: JSON.stringify({ session_id: 4, paypal_dispute_id: "PP-DEMO-H", status: "DEFECT_CONFIRMED", text: "Defect confirmed" }),
      }),
    );
    expect(document.body.textContent).toContain("Buyer finished troubleshooting");
    expect(document.body.textContent).toContain("PP-DEMO-H · defect confirmed");
    expect(document.querySelector('a[href="/cases/PP-DEMO-H"]')).not.toBeNull();
    expect(refreshed).toHaveBeenCalledTimes(1);
    window.removeEventListener(NEW_DISPUTE_EVENT, refreshed);
  });
});

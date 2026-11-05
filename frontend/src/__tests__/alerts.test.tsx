// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import NewDisputeAlerts, { NEW_DISPUTE_EVENT } from "../NewDisputeAlerts";
import { ToastProvider } from "../Toast";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The live stream is read with fetch() (session token in a header, never in the URL): the test pushes SSE text.
let push: (text: string) => void;
let streamCalls: { url: string; init?: RequestInit }[];

let el: HTMLDivElement;
let root: Root;
beforeEach(() => {
  streamCalls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/api/events/disputes")) {
      streamCalls.push({ url, init });
      const enc = new TextEncoder();
      const stream = new ReadableStream<Uint8Array>({ start: (c) => void (push = (t) => c.enqueue(enc.encode(t))) });
      return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    return new Response(JSON.stringify({ mode: "off" }), { status: 200 });
  }));
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
    expect(streamCalls[0].url).toContain("/api/events/disputes");
    expect(streamCalls[0].url).not.toContain("token=");
    await act(async () => {
      push(`event: troubleshooting\ndata: ${JSON.stringify({ session_id: 4, paypal_dispute_id: "PP-DEMO-H", status: "DEFECT_CONFIRMED", text: "Defect confirmed" })}\n\n`);
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(document.body.textContent).toContain("Buyer finished troubleshooting");
    expect(document.body.textContent).toContain("PP-DEMO-H · defect confirmed");
    expect(document.querySelector('a[href="/cases/PP-DEMO-H"]')).not.toBeNull();
    expect(refreshed).toHaveBeenCalledTimes(1);
    window.removeEventListener(NEW_DISPUTE_EVENT, refreshed);
  });
});

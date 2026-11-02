// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DisputeDetail } from "../api";
import Help from "../pages/Help";
import { TroubleshootingCard } from "../pages/CaseDetail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Reply = { status: number; body: unknown };
let replies: Record<string, Reply[]>;
let requests: { url: string; init?: RequestInit }[];
const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  requests.push({ url, init });
  const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
  const r = replies[key]?.shift() ?? { status: 500, body: { detail: `no reply for ${key}` } };
  return new Response(JSON.stringify(r.body), { status: r.status });
});

let el: HTMLDivElement;
let root: Root;
beforeEach(() => {
  replies = {};
  requests = [];
  vi.stubGlobal("fetch", fetchMock);
  sessionStorage.clear();
  el = document.createElement("div");
  document.body.appendChild(el);
  root = createRoot(el);
});
afterEach(() => {
  act(() => root.unmount());
  el.remove();
  vi.unstubAllGlobals();
});
const flush = () => act(async () => new Promise((r) => setTimeout(r, 0)));
const button = (text: string) => [...el.querySelectorAll("button")].find((b) => b.textContent?.includes(text)) as HTMLButtonElement;

const SESSION = {
  id: 5, status: "ACTIVE", product_name: "Smart LED desk lamp", product_model: "Lumina LD-200",
  expires_at: "2026-11-10T10:00:00Z", steps: [], messages: [],
};

async function renderHelp(url: string) {
  window.history.pushState(null, "", url);
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={["/help/5"]}>
        <Routes>
          <Route path="/help/:sessionId" element={<Help />} />
        </Routes>
      </MemoryRouter>,
    ),
  );
  await flush();
}

describe("buyer help page", () => {
  it("uses only the link token (never the merchant session) and hides it from the address bar", async () => {
    sessionStorage.setItem("ds_session", "merchant-token"); // a merchant signed in on the same browser
    replies["GET /api/help/5"] = [{ status: 200, body: SESSION }];
    await renderHelp("/help/5?t=secret-token");
    const headers = requests[0].init?.headers as Record<string, string>;
    expect(headers["X-Help-Token"]).toBe("secret-token");
    expect(headers.Authorization).toBeUndefined();
    expect(requests[0].url).not.toContain("secret-token");
    expect(window.location.search).toBe("");
    expect(sessionStorage.getItem("ds_help_5")).toBe("secret-token"); // a reload still works
    expect(el.textContent).toContain("Let's get your Smart LED desk lamp working");
    expect(el.textContent).toContain("Model Lumina LD-200");
    expect(el.textContent).toContain("being prepared");
  });

  it("explains an expired link", async () => {
    replies["GET /api/help/5"] = [{ status: 410, body: { detail: "This troubleshooting link has expired. Please contact the seller." } }];
    await renderHelp("/help/5?t=old");
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("has expired");
  });

  it("says when the link is incomplete, without calling the server", async () => {
    await renderHelp("/help/5");
    expect(el.textContent).toContain("link is incomplete");
    expect(requests).toHaveLength(0);
  });
});

const detail = (status = "WAITING_FOR_SELLER_RESPONSE") =>
  ({ dispute: { paypal_dispute_id: "PP-1", status }, timeline: [] }) as unknown as DisputeDetail;
const ELIGIBLE = {
  eligible: true, why: "Eligible", product_name: "Wireless earbuds", product_model: "SoundPeak E5",
  can_message: true, buyer_email_known: true, session: null,
};
const BRIEF = {
  id: 9, status: "INVITED", product_name: "Wireless earbuds", product_model: "SoundPeak E5",
  expires_at: "2026-11-10T10:00:00Z", invited_at: "2026-11-02T10:00:00Z", invited_via: "email", buyer_last_seen_at: null,
};

describe("case page troubleshooting card", () => {
  it("offers email when PayPal refuses the message, and shows the stub link once", async () => {
    replies["GET /api/disputes/PP-1/troubleshooting"] = [
      { status: 200, body: ELIGIBLE },
      { status: 200, body: { ...ELIGIBLE, session: BRIEF } },
    ];
    replies["POST /api/disputes/PP-1/troubleshooting/invite"] = [
      { status: 409, body: { detail: { reason: "PayPal doesn't allow messages on this dispute right now.", email_available: true } } },
      { status: 200, body: { sent: true, already_invited: false, via: "email", session: BRIEF, link: "http://x/help/9?t=tok" } },
    ];
    await act(async () => root.render(<TroubleshootingCard detail={detail()} />));
    await flush();
    expect(el.textContent).toContain("SoundPeak E5");
    await act(async () => button("Invite via PayPal message").click());
    await flush();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("doesn't allow messages");
    await act(async () => button("Email it instead").click());
    await flush();
    expect(JSON.parse(requests.at(-2)!.init!.body as string)).toEqual({ via: "email", resend: false });
    expect((el.querySelector('[aria-label="Buyer\'s private link"]') as HTMLInputElement).value).toBe("http://x/help/9?t=tok");
    expect(el.textContent).toContain("Invited, not opened yet");
    expect(button("Send a new link")).toBeDefined();
  });

  it("explains why a case isn't eligible and offers no buttons", async () => {
    replies["GET /api/disputes/PP-1/troubleshooting"] = [
      { status: 200, body: { ...ELIGIBLE, eligible: false, why: "Not eligible for buyer troubleshooting: the order has no electrical/electronic item" } },
    ];
    await act(async () => root.render(<TroubleshootingCard detail={detail()} />));
    await flush();
    expect(el.textContent).toContain("Not available: the order has no electrical/electronic item.");
    expect(el.querySelectorAll("button")).toHaveLength(0);
  });

  it("disables the PayPal invite when messaging isn't allowed and hides actions once resolved", async () => {
    replies["GET /api/disputes/PP-1/troubleshooting"] = [{ status: 200, body: { ...ELIGIBLE, can_message: false } }];
    await act(async () => root.render(<TroubleshootingCard detail={detail()} />));
    await flush();
    expect(button("Invite via PayPal message").disabled).toBe(true);
    expect(button("Email the link instead").disabled).toBe(false);
    act(() => root.unmount());
    root = createRoot(el);
    replies["GET /api/disputes/PP-1/troubleshooting"] = [{ status: 200, body: { ...ELIGIBLE, session: BRIEF } }];
    await act(async () => root.render(<TroubleshootingCard detail={detail("RESOLVED")} />));
    await flush();
    expect(button("Send a new link")).toBeUndefined();
  });
});

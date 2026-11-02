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
// Newer Node ships its own (unconfigured, undefined) localStorage global that hides jsdom's; use a simple one.
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, String(v)),
  };
}

beforeEach(() => {
  replies = {};
  requests = [];
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("localStorage", memoryStorage());
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

const OPENER = { role: "agent", body: "Hi! I'm here to help get your Smart LED desk lamp working. What's going wrong with it?", step_id: null, created_at: "2026-11-02T10:00:00Z" };
const SESSION = {
  id: 5, status: "ACTIVE", product_name: "Smart LED desk lamp", product_model: "Lumina LD-200",
  expires_at: "2026-11-10T10:00:00Z", language: null, plan_generic: null, awaiting_fixed_confirm: false, ended: false,
  steps: [] as unknown[], sources: [] as unknown[], messages: [OPENER] as unknown[], device_key: null as string | null,
};
const STEP = {
  id: 31, ordinal: 1, title: "Reset the lamp", instruction: "Unplug it for 30 seconds.", why: "Clears faults.",
  safety_level: "safe", requires_proof: true, proof_kind: "video", status: "PENDING", source_ids: [7], proofs: 0,
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
    localStorage.clear();
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, device_key: "dev-key-1" } }];
    await renderHelp("/help/5?t=secret-token");
    const headers = requests[0].init?.headers as Record<string, string>;
    expect(headers["X-Help-Token"]).toBe("secret-token");
    expect(headers.Authorization).toBeUndefined();
    expect(requests[0].url).not.toContain("secret-token");
    expect(window.location.search).toBe("");
    expect(sessionStorage.getItem("ds_help_5")).toBe("secret-token"); // a reload still works
    expect(el.textContent).toContain("Let's get your Smart LED desk lamp working");
    expect(el.textContent).toContain("Model Lumina LD-200");
    expect(el.textContent).toContain("What's going wrong with it?"); // the agent's first, open question
    expect(localStorage.getItem("ds_help_dev_5")).toBe("dev-key-1"); // this browser is now the link's device
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

describe("buyer chat", () => {
  const typeAndSend = async (text: string) => {
    const box = el.querySelector("#msg") as HTMLTextAreaElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, text);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => (el.querySelector("form") as HTMLFormElement).requestSubmit());
    await flush();
  };

  it("sends messages with the token and device key, and shows steps with sources and proof upload", async () => {
    localStorage.setItem("ds_help_dev_5", "dev-key-1");
    replies["GET /api/help/5"] = [{ status: 200, body: SESSION }];
    replies["POST /api/help/5/messages"] = [
      {
        status: 200,
        body: {
          ...SESSION,
          steps: [STEP],
          sources: [{ id: 7, title: "lumina.example", url: "https://support.lumina.example/ld200" }],
          messages: [OPENER, { role: "buyer", body: "It won't turn on", step_id: null, created_at: "x" }, { role: "agent", body: "Let's try step 1.", step_id: 31, created_at: "x" }],
          reply: "Let's try step 1.",
        },
      },
    ];
    await renderHelp("/help/5?t=tok");
    await typeAndSend("It won't turn on");
    const post = requests.find((q) => q.init?.method === "POST")!;
    expect((post.init!.headers as Record<string, string>)["X-Help-Device"]).toBe("dev-key-1");
    expect(JSON.parse(post.init!.body as string)).toEqual({ body: "It won't turn on", step_id: null, step_status: null });
    expect(el.textContent).toContain("Let's try step 1.");
    expect(el.textContent).toContain("1. Reset the lamp");
    expect((el.querySelector('a[href="https://support.lumina.example/ld200"]') as HTMLAnchorElement).rel).toContain("noopener");
    expect(el.querySelector('[aria-label="Upload a video for step 1"]')).not.toBeNull();
  });

  it("step buttons report a status; 'it works' needs the confirm button", async () => {
    localStorage.setItem("ds_help_dev_5", "d");
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, steps: [STEP] } }];
    replies["POST /api/help/5/messages"] = [{ status: 200, body: { ...SESSION, steps: [{ ...STEP, status: "DONE_FIXED" }], awaiting_fixed_confirm: true } }];
    replies["POST /api/help/5/confirm"] = [{ status: 200, body: { ...SESSION, status: "RESOLVED_FIXED", ended: true } }];
    await renderHelp("/help/5?t=tok");
    await act(async () => button("That fixed it").click());
    await flush();
    const sent = JSON.parse(requests.find((q) => q.url.endsWith("/messages"))!.init!.body as string);
    expect(sent).toEqual({ body: "Step 1: that fixed it", step_id: 31, step_status: "DONE_FIXED" });
    expect(el.querySelector('[aria-label="Is it working now?"]')).not.toBeNull();
    await act(async () => button("Yes, it works now").click());
    await flush();
    expect(JSON.parse(requests.at(-1)!.init!.body as string)).toEqual({ fixed: true });
    expect(el.textContent).toContain("Fixed. Thank you");
    expect(el.querySelector("#msg")).toBeNull(); // ended: no more chat
  });

  it("explains a link already open on another device", async () => {
    replies["GET /api/help/5"] = [{ status: 403, body: { detail: "This troubleshooting link is already open in another browser or device." } }];
    await renderHelp("/help/5?t=tok");
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("already open in another");
  });

  it("talk to the seller asks first, then hands over", async () => {
    replies["GET /api/help/5"] = [{ status: 200, body: SESSION }];
    replies["POST /api/help/5/handoff"] = [{ status: 200, body: { ...SESSION, status: "HANDED_TO_MERCHANT", ended: true } }];
    await renderHelp("/help/5?t=tok");
    vi.stubGlobal("confirm", () => false);
    await act(async () => button("Talk to the seller").click());
    expect(requests.some((q) => q.url.endsWith("/handoff"))).toBe(false);
    vi.stubGlobal("confirm", () => true);
    await act(async () => button("Talk to the seller").click());
    await flush();
    expect(el.textContent).toContain("The seller is taking it from here");
  });

  it("shows a rate-limit refusal inline and keeps the page", async () => {
    replies["GET /api/help/5"] = [{ status: 200, body: SESSION }];
    replies["POST /api/help/5/messages"] = [{ status: 429, body: { detail: "You're sending messages very fast; please wait a moment." } }];
    await renderHelp("/help/5?t=tok");
    await typeAndSend("hello");
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("very fast");
    expect(el.textContent).toContain("What's going wrong"); // still on the page
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

  it("shows the steps, proof files, sources and conversation to the merchant", async () => {
    replies["GET /api/disputes/PP-1/troubleshooting"] = [
      {
        status: 200,
        body: {
          ...ELIGIBLE,
          session: { ...BRIEF, status: "DEFECT_CONFIRMED", symptom: "Left earbud won't charge", ended_reason: "Defect confirmed" },
          steps: [{ ...STEP, status: "DONE_NOT_FIXED", proofs: 1 }],
          sources: [{ id: 7, title: "soundpeak.example", url: "https://support.soundpeak.example/e5" }],
          messages: [OPENER, { role: "buyer", body: "left one is dead", step_id: null, created_at: "x" }],
          proofs: [{ id: 3, step_id: 31, mime: "video/mp4", size: 10, created_at: "x" }],
        },
      },
    ];
    await act(async () => root.render(<TroubleshootingCard detail={detail()} />));
    await flush();
    expect(el.textContent).toContain("Defect confirmed");
    expect(el.textContent).toContain("Left earbud won't charge");
    expect(el.textContent).toContain("1. Reset the lamp (done, not fixed)");
    expect((el.querySelector('a[href*="/troubleshooting/proofs/3"]') as HTMLAnchorElement).textContent).toBe("video 1");
    expect(el.textContent).toContain("Conversation (2)");
    expect(el.textContent).toContain("soundpeak.example");
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

describe("first page load", () => {
  it("concurrent loads share one request (StrictMode double effects must not trip the device binding)", async () => {
    const { helpApi } = await import("../api");
    replies["GET /api/help/9"] = [{ status: 200, body: { ...SESSION, id: 9, device_key: "k" } }];
    const [a, b] = await Promise.all([helpApi.get("9", "tok"), helpApi.get("9", "tok")]);
    expect(a).toBe(b);
    expect(requests.filter((q) => q.url.endsWith("/api/help/9"))).toHaveLength(1);
  });
});

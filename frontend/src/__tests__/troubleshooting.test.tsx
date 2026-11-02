// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DisputeDetail } from "../api";
import Help from "../pages/Help";
import { TroubleshootingCard } from "../pages/CaseDetail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Reply = { status: number; body?: unknown; stream?: ReadableStream<Uint8Array> };
let replies: Record<string, Reply[]>;
let requests: { url: string; init?: RequestInit }[];
const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  requests.push({ url, init });
  const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
  const r = replies[key]?.shift() ?? { status: 500, body: { detail: `no reply for ${key}` } };
  if (r.stream) return new Response(r.stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  return new Response(JSON.stringify(r.body), { status: r.status });
});

/** A server-sent-events response the test can push events into. */
function sseStream() {
  let ctl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start: (c) => void (ctl = c) });
  const enc = new TextEncoder();
  return {
    reply: { status: 200, stream } as Reply,
    send: (event: string, data: unknown, id?: number) =>
      act(async () => {
        ctl.enqueue(enc.encode(`${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
        await new Promise((r) => setTimeout(r, 10));
      }),
  };
}

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

const OPENER = { id: 1, role: "agent", body: "Hi! I'm here to help get your Smart LED desk lamp working. What's going wrong with it?", step_id: null, created_at: "2026-11-02T10:00:00Z" };
const SESSION = {
  id: 5, status: "ACTIVE", product_name: "Smart LED desk lamp", product_model: "Lumina LD-200",
  expires_at: "2026-11-10T10:00:00Z", language: null, plan_generic: null, awaiting_fixed_confirm: false, ended: false,
  steps: [] as unknown[], sources: [] as unknown[], messages: [OPENER] as unknown[], device_key: null as string | null,
};
const STEP = {
  id: 31, ordinal: 1, title: "Reset the lamp", instruction: "Unplug it for 30 seconds.", why: "Clears faults.",
  safety_level: "safe", requires_proof: true, proof_kind: "video", status: "PENDING", source_ids: [7], proofs: 0,
};
const HEADER = { store_name: "Lumina Home", order_ref: "INV-1042", paypal_case_id: "PP-D-77", product_image: "💡", agent_working: false };

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
    expect(el.textContent).toContain("left"); // the countdown
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

  it("sends a message (202), shows it at once with the typing indicator, then streams the reply, steps and sources", async () => {
    localStorage.setItem("ds_help_dev_5", "dev-key-1");
    const sse = sseStream();
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, ...HEADER } }];
    replies["GET /api/help/5/events"] = [sse.reply];
    replies["POST /api/help/5/messages"] = [{ status: 202, body: { accepted: true, agent_working: true } }];
    await renderHelp("/help/5?t=tok");
    expect(el.querySelector("h1")?.textContent).toBe("Help with your order #INV-1042");
    expect(el.textContent).toContain("Lumina Home");
    expect(el.textContent).toContain("PayPal case PP-D-77");
    const stream = requests.find((q) => q.url.includes("/events"))!;
    expect(stream.url).toContain("after=1"); // resumes after the newest message it already has
    expect(stream.url).not.toContain("tok"); // token only in headers, never in the URL
    expect((stream.init!.headers as Record<string, string>)["X-Help-Device"]).toBe("dev-key-1");
    await typeAndSend("It won't turn on");
    const post = requests.find((q) => q.init?.method === "POST")!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ body: "It won't turn on" });
    expect(el.textContent).toContain("It won't turn on"); // shown before the server answers
    expect(el.textContent).toContain("The assistant is typing");
    // the stream delivers the stored message, the reply and the new state
    await sse.send("message", { id: 2, role: "buyer", body: "It won't turn on", step_id: null, created_at: "x" }, 2);
    await sse.send("message", { id: 3, role: "agent", body: "Let's try step 1.", step_id: 31, created_at: "x" }, 3);
    await sse.send("state", {
      status: "ACTIVE", ended: false, awaiting_fixed_confirm: false, agent_working: false,
      steps: [STEP], sources: [{ id: 7, title: "Lumina support", url: "https://support.lumina.example/ld200" }],
    });
    expect(el.textContent!.match(/It won't turn on/g)).toHaveLength(1); // the optimistic bubble was replaced
    expect(el.textContent).not.toContain("The assistant is typing");
    expect(el.textContent).toContain("Let's try step 1.");
    expect(el.textContent).toContain("Reset the lamp");
    expect(el.textContent).toContain("Steps 1/1");
    const chip = el.querySelector('[aria-label="Sources"] a') as HTMLAnchorElement; // source chip under the reply
    expect(chip.href).toBe("https://support.lumina.example/ld200");
    expect(chip.target).toBe("_blank");
    expect(chip.rel).toContain("noopener");
    expect(chip.textContent).toContain("support.lumina.example");
    expect(el.querySelector('[aria-live="polite"]')).not.toBeNull();
    await sse.send("message", { id: 4, role: "merchant", body: "Hi, seller here.", step_id: null, created_at: "x" }, 4);
    const seller = [...el.querySelectorAll("li")].find((li) => li.textContent?.includes("Hi, seller here."))!;
    expect(seller.textContent).toContain("Seller");
  });

  it("step buttons report a status; 'it works' needs the confirm button; then the thank-you screen", async () => {
    localStorage.setItem("ds_help_dev_5", "d");
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, steps: [STEP] } }];
    replies["POST /api/help/5/steps/31"] = [{ status: 200, body: { ...SESSION, steps: [{ ...STEP, status: "DONE_FIXED" }], awaiting_fixed_confirm: true } }];
    replies["POST /api/help/5/confirm"] = [{ status: 200, body: { ...SESSION, status: "RESOLVED_FIXED", ended: true } }];
    await renderHelp("/help/5?t=tok");
    await act(async () => button("Done, it works now").click());
    await flush();
    expect(JSON.parse(requests.find((q) => q.url.endsWith("/steps/31"))!.init!.body as string)).toEqual({ status: "DONE_FIXED", note: "" });
    expect(el.querySelector('[aria-label="Is it working now?"]')).not.toBeNull();
    await act(async () => button("Yes, it works now").click());
    await flush();
    expect(JSON.parse(requests.at(-1)!.init!.body as string)).toEqual({ fixed: true });
    expect(el.textContent).toContain("Great, it's working!");
    expect(el.textContent).toContain("Resolution Center"); // how to close the case, without pressure
    expect(el.textContent).toContain("no rush");
    expect(el.querySelector("#msg")).toBeNull(); // ended: no more chat
  });

  it.each([
    ["DEFECT_CONFIRMED", "We've sent your results to the seller"],
    ["SAFETY_STOP", "Stopped for your safety"],
    ["HANDED_TO_MERCHANT", "The seller is taking it from here"],
    ["EXPIRED", "This help page has closed"],
  ])("outcome screen for %s", async (status, text) => {
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, status, ended: true, steps: [{ ...STEP, status: "DONE_NOT_FIXED" }] } }];
    await renderHelp("/help/5?t=tok");
    expect(el.querySelector('[role="status"]')?.textContent).toContain(text);
    expect(el.querySelector("#msg")).toBeNull();
    expect(button("Done, still not working")).toBeUndefined();
    expect(button("Talk to the seller")).toBeUndefined();
  });

  it("the countdown running out closes the page even before the server says so", async () => {
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, expires_at: "2020-01-01T00:00:00Z" } }];
    await renderHelp("/help/5?t=tok");
    expect(el.textContent).toContain("This help page has closed");
    expect(el.querySelector("#msg")).toBeNull();
  });

  it("a stream refusal (link no longer valid) replaces the page", async () => {
    replies["GET /api/help/5"] = [{ status: 200, body: SESSION }];
    replies["GET /api/help/5/events"] = [{ status: 410, body: { detail: "This troubleshooting link has expired. Please contact the seller." } }];
    await renderHelp("/help/5?t=tok");
    await flush();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("has expired");
  });

  it("phones get a sticky 'Steps 1/2' bar that opens the checklist as a bottom sheet", async () => {
    replies["GET /api/help/5"] = [{ status: 200, body: { ...SESSION, steps: [STEP, { ...STEP, id: 32, ordinal: 2, title: "Charge it" }] } }];
    await renderHelp("/help/5?t=tok");
    const bar = button("Steps 1/2");
    expect(bar.textContent).toContain("Reset the lamp");
    expect(el.querySelector('[role="dialog"]')).toBeNull();
    await act(async () => bar.click());
    const sheet = el.querySelector('[role="dialog"]') as HTMLElement;
    expect(sheet.getAttribute("aria-modal")).toBe("true");
    expect(sheet.textContent).toContain("Charge it");
    expect(document.activeElement).toBe(sheet); // focus moves into the sheet
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(el.querySelector('[role="dialog"]')).toBeNull();
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
    expect((el.querySelector("#msg") as HTMLTextAreaElement).value).toBe("hello"); // the text isn't lost
    expect(el.textContent).not.toContain("The assistant is typing");
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

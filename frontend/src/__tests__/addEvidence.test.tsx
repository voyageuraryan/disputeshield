// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CoachItem, MerchantUpload } from "../api";
import { CoachPanel, ConfirmUpload } from "../evidence/AddEvidence";
import { checkFile, checkInput, coachLine, prefill, readNote, toConfirmBody } from "../evidence/logic";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MB = 1024 * 1024;
const upload = (over: Partial<MerchantUpload> = {}): MerchantUpload => ({
  id: 7, evidence_type: "delivery_confirmation", status: "PENDING", kind: "file", original_name: "pod.pdf",
  mime: "application/pdf", size: 1000, fact: null, fields: null, flags: null, summary: null,
  created_at: "2026-11-03T10:00:00Z", confirmed_at: null,
  extraction: {
    document_kind: "USPS proof of delivery", confidence: "high",
    key_fields: { tracking_number: "9400 1118", carrier: "USPS", delivery_date: "2026-10-07",
      delivered_to_address: "2211 N First St, San Jose, CA 95131", amount: "", currency: "" },
  },
  ...over,
});
const coach = (over: Partial<CoachItem> = {}): CoachItem => ({
  type: "delivery_confirmation", required: true, score_now: 0.5, score_after: 0.85, gain: 0.35,
  action_now: "GATHER_MORE", action_after: "FIGHT", howto: ["USPS: save the delivered page as PDF."],
  assumption: "assuming it is clean", ...over,
});

describe("upload checks (same limits PayPal enforces)", () => {
  it("accepts PDF/JPG/PNG under 10 MB", () => {
    expect(checkFile({ name: "a.pdf", size: 5 * MB, type: "application/pdf" })).toBeNull();
    expect(checkFile({ name: "photo.JPG", size: 100, type: "" })).toBeNull();
  });
  it("rejects other types, empty and 10 MB+ files", () => {
    expect(checkFile({ name: "a.docx", size: 100, type: "application/msword" })).toMatch(/Only PDF, JPG or PNG/);
    expect(checkFile({ name: "a.pdf", size: 0, type: "application/pdf" })).toMatch(/empty/);
    expect(checkFile({ name: "a.pdf", size: 10 * MB, type: "application/pdf" })).toMatch(/under 10 MB/);
  });
  it("needs a file or a fact", () => {
    expect(checkInput(null, "  ")).toMatch(/Attach a file or type/);
    expect(checkInput(null, "USPS confirmation 123")).toBeNull();
    expect(checkInput(null, "x".repeat(501))).toMatch(/under 500/);
  });
});

describe("fields: prefilled from the read, sent only what the merchant confirms", () => {
  it("prefills what Gemini read and drops malformed values", () => {
    const v = prefill(upload({ extraction: { key_fields: { tracking_number: "1Z", carrier: "UPS", delivery_date: "7 Oct",
      delivered_to_address: "", amount: "about 89", currency: "USD" } } }));
    expect(v).toMatchObject({ tracking_number: "1Z", carrier: "UPS", delivery_date: "", amount: "", currency: "USD" });
  });
  it("sends trimmed, non-blank fields that belong to the type", () => {
    const v = { ...prefill(upload()), amount: "89.00", note: "  " };
    expect(toConfirmBody(v, "delivery_confirmation")).toEqual({
      evidence_type: "delivery_confirmation", document_kind: "USPS proof of delivery", tracking_number: "9400 1118",
      carrier: "USPS", delivery_date: "2026-10-07", delivered_to_address: "2211 N First St, San Jose, CA 95131",
    }); // amount isn't a delivery-confirmation field
    expect(toConfirmBody(v, "invoice")).toMatchObject({ evidence_type: "invoice", amount: "89.00" });
  });
  it("says how the fields were filled", () => {
    expect(readNote(upload())).toMatch(/high confidence.*nothing is used until you confirm/);
    expect(readNote(upload({ extraction: { skipped: "typed fact: fill in the fields that apply" } }))).toBe("Typed fact: fill in the fields that apply.");
    expect(readNote(upload({ extraction: { error: "x" } }))).toMatch(/fill in the fields yourself/);
  });
});

describe("coach", () => {
  it("reads as score before -> after -> action", () => {
    expect(coachLine(coach())).toBe("Add delivery confirmation → score 50 → 85 → Fight");
    expect(coachLine(coach({ type: "tracking+delivery_confirmation", action_after: "GATHER_MORE" }))).toBe(
      "Add tracking + delivery confirmation → score 50 → 85",
    );
  });
});

describe("components", () => {
  let root: Root | null = null;
  let el: HTMLDivElement;
  afterEach(() => {
    act(() => root?.unmount());
    root = null;
    el?.remove();
    vi.unstubAllGlobals();
  });
  const mount = (node: React.ReactNode) => {
    el = document.createElement("div");
    document.body.appendChild(el);
    root = createRoot(el);
    act(() => root!.render(node));
    return el;
  };

  it("coach panel lists items with how-to and an Add action", () => {
    const onAdd = vi.fn();
    const c = mount(<CoachPanel items={[coach(), coach({ type: "invoice", required: false, gain: 0.1 })]} onAdd={onAdd} canAdd />);
    expect(c.textContent).toContain("Add delivery confirmation → score 50 → 85 → Fight");
    expect(c.textContent).toContain("USPS: save the delivered page as PDF.");
    act(() => (c.querySelector("button") as HTMLButtonElement).click());
    expect(onAdd).toHaveBeenCalledWith("delivery_confirmation");
  });

  it("confirm sends the (edited) fields; nothing is sent before the click", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ upload: upload({ status: "CONFIRMED" }),
      run: { started: true, run_key: "k", message: "re-analysing" } }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const onDone = vi.fn();
    const c = mount(<ConfirmUpload disputeId="PP-D-1" upload={upload()} types={["delivery_confirmation", "tracking"]} onDone={onDone} />);
    const inputs = c.querySelectorAll("input");
    expect((inputs[1] as HTMLInputElement).value).toBe("9400 1118"); // prefilled tracking number
    expect(fetchMock).not.toHaveBeenCalled();
    const confirm = [...c.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Confirm")) as HTMLButtonElement;
    await act(async () => confirm.click());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/disputes/PP-D-1/uploads/7/confirm");
    expect(JSON.parse(init.body as string)).toMatchObject({ evidence_type: "delivery_confirmation", carrier: "USPS" });
    expect(onDone).toHaveBeenCalledWith({ started: true, run_key: "k", message: "re-analysing" });
  });
});

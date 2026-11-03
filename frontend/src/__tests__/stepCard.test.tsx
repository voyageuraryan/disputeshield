// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TSStepView } from "../api";
import { StepCard, type StepCardProps } from "../help/StepCard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let el: HTMLDivElement;
let root: Root;
beforeEach(() => {
  el = document.createElement("div");
  document.body.appendChild(el);
  root = createRoot(el);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob(["x"], { type: "image/jpeg" }))));
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  act(() => root.unmount());
  el.remove();
  vi.unstubAllGlobals();
});
const flush = () => act(async () => new Promise((r) => setTimeout(r, 0)));
const buttons = () => [...el.querySelectorAll("button")].map((b) => b.textContent);

const STEP: TSStepView = {
  id: 31, ordinal: 2, title: "Record the fault", instruction: "Film the light not coming on.", why: "Shows the seller.",
  safety_level: "safe", requires_proof: true, proof_kind: "video", status: "PENDING", source_ids: [7], proofs: 0,
};

function render(over: Partial<StepCardProps> = {}, step: Partial<TSStepView> = {}) {
  const props: StepCardProps = {
    step: { ...STEP, ...step },
    sources: [{ id: 7, title: "Lumina", url: "https://support.lumina.example/x" }, { id: 8, title: "Generic", url: "internal:generic" }],
    open: true, busy: false, current: true, sessionId: "5", token: "tok", onAnswer: vi.fn(), onUploaded: vi.fn(), ...over,
  };
  act(() => root.render(<ul><StepCard {...props} /></ul>));
  return props;
}

/** Fake XHR: the test decides when the upload progresses, fails or finishes. */
class FakeXHR {
  static last: FakeXHR;
  upload: { onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
  headers: Record<string, string> = {};
  status = 0;
  responseText = "";
  onload?: () => void;
  onerror?: () => void;
  url = "";
  body: FormData | null = null;
  constructor() {
    FakeXHR.last = this;
  }
  open(_m: string, url: string) {
    this.url = url;
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }
  send(body: FormData) {
    this.body = body;
  }
}

describe("step card", () => {
  it("shows number, title, instruction, why, safety and proof badges, sources (web only) and the three answers", () => {
    render();
    expect(el.textContent).toContain("Record the fault");
    expect(el.textContent).toContain("Why this helps: Shows the seller.");
    expect(el.textContent).toContain("Safe step");
    expect(el.textContent).toContain("Needs a video");
    expect(el.querySelectorAll('a[href^="https://"]')).toHaveLength(1);
    expect(el.querySelector('a[href^="internal:"]')).toBeNull();
    expect(buttons()).toEqual(expect.arrayContaining(["Done, it works now", "Done, still not working", "I can't do this"]));
    expect(el.querySelector("li")?.getAttribute("aria-current")).toBe("step");
  });

  it("answers carry the optional note", () => {
    const p = render();
    act(() => [...el.querySelectorAll("button")].find((b) => b.textContent === "Add a note")!.click());
    const input = el.querySelector("#note-31") as HTMLInputElement;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "light blinks twice");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => [...el.querySelectorAll("button")].find((b) => b.textContent === "I can't do this")!.click());
    expect(p.onAnswer).toHaveBeenCalledWith("CANT_DO", "light blinks twice");
  });

  it("caution steps say so", () => {
    render({}, { safety_level: "caution" });
    expect(el.textContent).toContain("Take care");
  });

  it("finished steps show their result and no answer buttons; the upload stays while proof is missing", () => {
    render({}, { status: "DONE_NOT_FIXED" });
    expect(el.textContent).toContain("Done, still not working");
    expect(buttons()).not.toContain("Done, it works now");
    expect(el.querySelector('[aria-label="Upload a video for step 2"]')).not.toBeNull();
  });

  it("busy disables the answers; an ended session shows neither answers nor upload", () => {
    render({ busy: true });
    expect([...el.querySelectorAll("button")].filter((b) => b.textContent?.startsWith("Done")).every((b) => b.disabled)).toBe(true);
    render({ open: false });
    expect(buttons()).not.toContain("Done, it works now");
    expect(el.querySelector('input[type="file"]')).toBeNull();
  });

  it("offers the phone camera (capture) and a file picker for images and videos", () => {
    render();
    const cam = el.querySelector('[aria-label="Use the camera for step 2"]') as HTMLInputElement;
    expect(cam.getAttribute("accept")).toBe("image/*,video/*");
    expect(cam.hasAttribute("capture")).toBe(true);
    const file = el.querySelector('[aria-label="Upload a video for step 2"]') as HTMLInputElement;
    expect(file.hasAttribute("capture")).toBe(false);
    expect(el.querySelector('label[for="up-31-cam"]')?.textContent).toBe("Use camera"); // keyboard: label for a real input
  });

  it("refuses a wrong file before uploading", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    render();
    const input = el.querySelector('[aria-label="Upload a video for step 2"]') as HTMLInputElement;
    const pdf = new File(["%PDF"], "doc.pdf", { type: "application/pdf" });
    await act(async () => {
      Object.defineProperty(input, "files", { value: [pdf], configurable: true });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("Please choose a photo");
    expect(buttons()).not.toContain("Retry"); // nothing to retry: pick another file
  });

  it("uploads with a thumbnail and progress bar; a failure offers Retry, which succeeds", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    const p = render({}, { proof_kind: "photo" });
    const input = el.querySelector('[aria-label="Upload a photo for step 2"]') as HTMLInputElement;
    const photo = new File([new Uint8Array(2000)], "fault.jpg", { type: "image/jpeg" });
    await act(async () => {
      Object.defineProperty(input, "files", { value: [photo], configurable: true });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    const xhr = FakeXHR.last;
    expect(xhr.url).toBe("/api/help/5/steps/31/proof");
    expect(xhr.headers["X-Help-Token"]).toBe("tok");
    expect((xhr.body!.get("file") as File).name).toBe("fault.jpg");
    expect((el.querySelector("img") as HTMLImageElement).src).toBe("blob:preview"); // thumbnail at once
    await act(async () => xhr.upload.onprogress!({ lengthComputable: true, loaded: 500, total: 1000 }));
    expect(el.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("50");
    await act(async () => xhr.onerror!());
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("didn't go through");
    await act(async () => [...el.querySelectorAll("button")].find((b) => b.textContent === "Retry")!.click());
    await flush();
    const again = FakeXHR.last;
    expect(again).not.toBe(xhr);
    again.status = 200;
    again.responseText = JSON.stringify({ id: 5, steps: [], proof: { id: 9, already_uploaded: false } });
    await act(async () => again.onload!());
    expect(el.textContent).toContain("Uploaded");
    expect(p.onUploaded).toHaveBeenCalledWith(expect.objectContaining({ proof: expect.objectContaining({ id: 9 }) }));
  });

  it("a server refusal is shown with its reason", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    render({}, { proof_kind: "photo" });
    const input = el.querySelector('[aria-label="Upload a photo for step 2"]') as HTMLInputElement;
    await act(async () => {
      Object.defineProperty(input, "files", { value: [new File(["x"], "a.jpg", { type: "image/jpeg" })], configurable: true });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    FakeXHR.last.status = 422;
    FakeXHR.last.responseText = JSON.stringify({ detail: "We couldn't open this photo. Please take it again or upload another one." });
    await act(async () => FakeXHR.last.onload!());
    expect(el.querySelector('[role="alert"]')?.textContent).toContain("couldn't open this photo");
  });

  it("earlier uploads: photos load as thumbnails with the link headers, videos are a tile (not downloaded)", async () => {
    render({}, { proofs: 2, proof_files: [{ id: 3, kind: "image" }, { id: 4, kind: "video" }] });
    await flush();
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    expect(calls.map((c) => c[0])).toEqual(["/api/help/5/proofs/3"]);
    expect((calls[0][1].headers as Record<string, string>)["X-Help-Token"]).toBe("tok");
    expect(el.querySelector('img[alt="Your photo"]')).not.toBeNull();
    expect(el.querySelector('[aria-label="Your video"]')).not.toBeNull();
    expect(el.textContent).toContain("Uploaded ✓ (2)");
  });
});

describe("deleting an upload (G44)", () => {
  it("asks first, then deletes with the link headers and passes the fresh session up", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (init?.method === "DELETE") return new Response(JSON.stringify({ id: 5, steps: [], messages: [] }), { status: 200 });
      return new Response(new Blob(["x"], { type: "image/jpeg" }));
    }));
    const onChanged = vi.fn();
    render({ onChanged }, { proofs: 1, proof_files: [{ id: 3, kind: "image" }] });
    await flush();
    const del = el.querySelector('[aria-label="Delete this photo"]') as HTMLButtonElement;
    vi.stubGlobal("confirm", () => false);
    await act(async () => del.click());
    expect(calls.some((c) => c.init?.method === "DELETE")).toBe(false);
    vi.stubGlobal("confirm", () => true);
    await act(async () => del.click());
    await flush();
    const d = calls.find((c) => c.init?.method === "DELETE")!;
    expect(d.url).toBe("/api/help/5/proofs/3");
    expect((d.init!.headers as Record<string, string>)["X-Help-Token"]).toBe("tok");
    expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ id: 5 }));
  });

  it("no delete button when the page has ended (no onChanged)", async () => {
    render({}, { proofs: 1, proof_files: [{ id: 3, kind: "image" }] });
    await flush();
    expect(el.querySelector('[aria-label="Delete this photo"]')).toBeNull();
  });
});

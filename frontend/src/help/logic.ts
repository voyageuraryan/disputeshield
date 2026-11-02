// Pure logic for the buyer help page (unit-tested in __tests__/helpLogic.test.ts).
import type { BuyerSession, TSMessageView, TSSourceView, TSStepView } from "../api";

export type StepStatus = TSStepView["status"];

/** "Steps 2/5": steps the buyer has finished, and which one to look at now. */
export function progress(steps: TSStepView[]): { done: number; total: number; current: TSStepView | null } {
  const done = steps.filter((s) => s.status !== "PENDING").length;
  return { done, total: steps.length, current: steps.find((s) => s.status === "PENDING") ?? null };
}

/** What a step card offers. Buttons only while the step is open; uploads while the session is open. */
export function stepActions(step: TSStepView, sessionOpen: boolean, busy: boolean) {
  const pending = step.status === "PENDING";
  const proofMissing = step.requires_proof && step.proofs === 0;
  return {
    canAnswer: sessionOpen && pending && !busy,
    canUpload: sessionOpen && step.requires_proof && step.status !== "DONE_FIXED",
    proofMissing,
    // a finished-but-not-fixed step whose proof is still missing: the next thing the buyer needs to do
    needsProofNow: sessionOpen && proofMissing && (step.status === "DONE_NOT_FIXED" || step.status === "CANT_DO"),
  };
}

export const STEP_STATUS_LABEL: Record<StepStatus, string> = {
  PENDING: "To do",
  DONE_FIXED: "Worked",
  DONE_NOT_FIXED: "Done, still not working",
  SKIPPED: "Skipped",
  CANT_DO: "Couldn't do it",
};

export type Outcome = "fixed" | "defect" | "safety" | "expired" | "handed" | null;

export function outcomeOf(s: Pick<BuyerSession, "status" | "ended">): Outcome {
  switch (s.status) {
    case "RESOLVED_FIXED":
      return "fixed";
    case "DEFECT_CONFIRMED":
      return "defect";
    case "SAFETY_STOP":
      return "safety";
    case "HANDED_TO_MERCHANT":
      return "handed";
    case "EXPIRED":
      return "expired";
    default:
      return s.ended ? "expired" : null;
  }
}

/** Time left before the help window closes, for the header. */
export function timeLeft(expiresAt: string, now: number = Date.now()): { text: string; urgent: boolean; over: boolean } {
  const ms = new Date(expiresAt).getTime() - now;
  if (!Number.isFinite(ms) || ms <= 0) return { text: "Closed", urgent: true, over: true };
  const min = Math.floor(ms / 60000);
  const d = Math.floor(min / 1440);
  const h = Math.floor((min % 1440) / 60);
  const m = min % 60;
  const text = d > 0 ? `${d} day${d === 1 ? "" : "s"} ${h} h left` : h > 0 ? `${h} h ${m} min left` : `${Math.max(m, 1)} min left`;
  return { text, urgent: ms < 6 * 3600 * 1000, over: false };
}

export function domainOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** Source chips under an agent message: the web sources of the step the message is about (never internal ones). */
export function sourceChips(m: TSMessageView, steps: TSStepView[], sources: TSSourceView[]) {
  if (m.role !== "agent" || m.step_id == null) return [];
  const step = steps.find((s) => s.id === m.step_id);
  const byId = new Map(sources.map((s) => [s.id, s]));
  return (step?.source_ids ?? [])
    .map((id) => byId.get(id))
    .filter((s): s is TSSourceView => !!s && /^https?:\/\//.test(s.url))
    .map((s) => ({ id: s.id, url: s.url, domain: domainOf(s.url), title: s.title && s.title !== domainOf(s.url) ? s.title : "" }));
}

/** Add messages from the stream: no duplicates (by id), in id order; a pending (optimistic) buyer bubble with the
 * same text is replaced by the real one. */
export function mergeMessages(current: TSMessageView[], incoming: TSMessageView[]): TSMessageView[] {
  const out = [...current];
  for (const m of incoming) {
    if (out.some((x) => x.id === m.id)) continue;
    const pending = out.findIndex((x) => x.id < 0 && x.role === m.role && x.body === m.body);
    if (pending >= 0) out.splice(pending, 1);
    out.push(m);
  }
  const real = out.filter((x) => x.id >= 0).sort((a, b) => a.id - b.id);
  return [...real, ...out.filter((x) => x.id < 0)];
}

export const IMAGE_MAX = 10 * 1024 * 1024;
export const VIDEO_MAX = 50 * 1024 * 1024;
export const VIDEO_MAX_SECONDS = 30;
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/heic", "image/heif"];
const VIDEO_TYPES = ["video/mp4", "video/quicktime"];

/** Quick check before uploading (the server checks the real bytes again). Returns a message, or null if fine. */
export function checkFile(file: { name: string; type: string; size: number }): string | null {
  const ext = file.name.toLowerCase().split(".").pop() ?? "";
  const type = file.type.toLowerCase();
  const isVideo = VIDEO_TYPES.includes(type) || (!type && ["mp4", "mov"].includes(ext));
  const isImage = IMAGE_TYPES.includes(type) || (!type && ["jpg", "jpeg", "png", "heic", "heif"].includes(ext));
  if (!isVideo && !isImage) return "Please choose a photo (JPEG, PNG or HEIC) or a video (MP4 or MOV).";
  if (file.size === 0) return "That file is empty.";
  if (isImage && file.size > IMAGE_MAX) return "This photo is larger than 10 MB. Please take a new one or choose a smaller one.";
  if (isVideo && file.size > VIDEO_MAX) return "This video is larger than 50 MB. Please record a shorter one (up to 30 seconds).";
  return null;
}

export type SSEEvent = { event: string; data: string; id?: string };

/** Split a text/event-stream buffer into complete events; returns the unfinished rest. */
export function parseSSE(buffer: string): { events: SSEEvent[]; rest: string } {
  const events: SSEEvent[] = [];
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    let id: string | undefined;
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const i = line.indexOf(":");
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
      else if (field === "id") id = value;
    }
    if (data.length || event !== "message") events.push({ event, data: data.join("\n"), id });
  }
  return { events, rest };
}

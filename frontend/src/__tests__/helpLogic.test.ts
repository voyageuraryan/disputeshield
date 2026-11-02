import { describe, expect, it } from "vitest";
import type { TSStepView } from "../api";
import { checkFile, mergeMessages, outcomeOf, parseSSE, progress, sourceChips, stepActions, timeLeft } from "../help/logic";

const step = (o: Partial<TSStepView>): TSStepView => ({
  id: 1, ordinal: 1, title: "Reset", instruction: "Do it", why: null, safety_level: "safe", requires_proof: false,
  proof_kind: "none", status: "PENDING", source_ids: [], proofs: 0, ...o,
});
const msg = (id: number, role = "agent", body = `m${id}`, step_id: number | null = null) => ({ id, role, body, step_id, created_at: "" });

describe("step progress", () => {
  it("counts finished steps and points at the first open one", () => {
    const steps = [step({ id: 1, status: "DONE_NOT_FIXED" }), step({ id: 2, ordinal: 2 }), step({ id: 3, ordinal: 3, status: "CANT_DO" })];
    expect(progress(steps)).toMatchObject({ done: 2, total: 3, current: { id: 2 } });
    expect(progress([])).toEqual({ done: 0, total: 0, current: null });
    expect(progress([step({ status: "DONE_FIXED" })]).current).toBeNull();
  });
});

describe("step card state", () => {
  it.each([
    // status, requires proof, proofs, session open, busy -> canAnswer, canUpload, needsProofNow
    ["PENDING", false, 0, true, false, true, false, false],
    ["PENDING", true, 0, true, false, true, true, false],
    ["PENDING", false, 0, true, true, false, false, false], // busy: buttons off
    ["PENDING", true, 0, false, false, false, false, false], // session ended: nothing
    ["DONE_NOT_FIXED", true, 0, true, false, false, true, true], // proof is the next thing to do
    ["DONE_NOT_FIXED", true, 1, true, false, false, true, false],
    ["CANT_DO", true, 0, true, false, false, true, true],
    ["DONE_FIXED", true, 0, true, false, false, false, false], // it worked: no proof needed
  ] as const)("%s proof=%s uploads=%i open=%s busy=%s", (status, req, proofs, open, busy, answer, upload, needs) => {
    const a = stepActions(step({ status, requires_proof: req, proofs }), open, busy);
    expect([a.canAnswer, a.canUpload, a.needsProofNow]).toEqual([answer, upload, needs]);
  });
});

describe("outcome screen", () => {
  it.each([
    ["RESOLVED_FIXED", true, "fixed"],
    ["DEFECT_CONFIRMED", true, "defect"],
    ["SAFETY_STOP", true, "safety"],
    ["HANDED_TO_MERCHANT", true, "handed"],
    ["EXPIRED", true, "expired"],
    ["ACTIVE", false, null],
    ["AWAITING_PROOF", false, null],
  ])("%s -> %s", (status, ended, out) => expect(outcomeOf({ status, ended })).toBe(out));
});

describe("countdown", () => {
  const now = Date.parse("2026-11-02T10:00:00Z");
  it.each([
    ["2026-11-04T14:30:00Z", "2 days 4 h left", false],
    ["2026-11-03T10:00:00Z", "1 day 0 h left", false],
    ["2026-11-02T13:20:00Z", "3 h 20 min left", true],
    ["2026-11-02T10:00:20Z", "1 min left", true],
  ])("%s", (at, text, urgent) => expect(timeLeft(at, now)).toEqual({ text, urgent, over: false }));
  it("is over at or after the expiry", () => {
    expect(timeLeft("2026-11-02T10:00:00Z", now).over).toBe(true);
    expect(timeLeft("not a date", now).over).toBe(true);
  });
});

describe("source chips", () => {
  const steps = [step({ id: 4, source_ids: [1, 2, 3] })];
  const sources = [
    { id: 1, title: "Lumina support", url: "https://www.lumina.example/support/ld200" },
    { id: 2, title: "General safe checks", url: "internal:generic-safe-checklist" },
    { id: 3, title: "forum.example", url: "https://forum.example/t/1" },
  ];
  it("shows web sources of the step an agent message is about, with their domain", () => {
    expect(sourceChips(msg(1, "agent", "x", 4), steps, sources)).toEqual([
      { id: 1, url: "https://www.lumina.example/support/ld200", domain: "lumina.example", title: "Lumina support" },
      { id: 3, url: "https://forum.example/t/1", domain: "forum.example", title: "" },
    ]);
  });
  it("nothing for buyer messages or messages without a step", () => {
    expect(sourceChips(msg(1, "buyer", "x", 4), steps, sources)).toEqual([]);
    expect(sourceChips(msg(1, "agent"), steps, sources)).toEqual([]);
  });
});

describe("merging streamed messages", () => {
  it("dedupes by id, keeps order and replaces the optimistic bubble", () => {
    const cur = [msg(1), msg(2), msg(-99, "buyer", "hello")];
    const out = mergeMessages(cur, [msg(2), msg(3, "buyer", "hello"), msg(4)]);
    expect(out.map((m) => m.id)).toEqual([1, 2, 3, 4]);
    expect(mergeMessages([msg(5)], [msg(3), msg(4)]).map((m) => m.id)).toEqual([3, 4, 5]); // late arrivals sort in
  });
});

describe("file check before upload", () => {
  const MB = 1024 * 1024;
  it.each([
    [{ name: "a.jpg", type: "image/jpeg", size: MB }, null],
    [{ name: "IMG_1.HEIC", type: "", size: MB }, null], // some browsers don't know HEIC's type
    [{ name: "a.png", type: "image/png", size: 11 * MB }, "larger than 10 MB"],
    [{ name: "v.mov", type: "video/quicktime", size: 40 * MB }, null],
    [{ name: "v.mp4", type: "video/mp4", size: 51 * MB }, "larger than 50 MB"],
    [{ name: "doc.pdf", type: "application/pdf", size: 1000 }, "Please choose a photo"],
    [{ name: "a.gif", type: "image/gif", size: 1000 }, "Please choose a photo"],
    [{ name: "a.jpg", type: "image/jpeg", size: 0 }, "empty"],
  ])("%o", (file, problem) => {
    const r = checkFile(file);
    if (problem === null) expect(r).toBeNull();
    else expect(r).toContain(problem);
  });
});

describe("SSE parsing", () => {
  it("splits complete events and keeps the unfinished rest", () => {
    const { events, rest } = parseSSE('retry: 3000\n\nid: 7\nevent: message\ndata: {"id":7}\n\n: ping\n\nevent: state\ndata: {"a":\ndata: 1}\n\nevent: rea');
    expect(events).toEqual([
      { event: "message", data: '{"id":7}', id: "7" },
      { event: "state", data: '{"a":\n1}', id: undefined },
    ]);
    expect(rest).toBe("event: rea");
  });
});

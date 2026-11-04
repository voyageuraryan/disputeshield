import { describe, expect, it } from "vitest";
import { fingerprintSource, sha256Hex } from "../fingerprint";
import { signalChips } from "../pages/CaseDetail";

describe("device fingerprint (Phase 15)", () => {
  it("is built from user agent + screen + time zone only", () => {
    expect(fingerprintSource({ userAgent: "UA/1" }, { width: 1920, height: 1080, colorDepth: 24 }, "Asia/Kolkata")).toBe(
      "UA/1|1920x1080x24|Asia/Kolkata",
    );
  });
  it("only a SHA-256 hex leaves the browser", async () => {
    const h = await sha256Hex("UA/1|1920x1080x24|Asia/Kolkata");
    expect(h).toMatch(/^[0-9a-f]{64}$/);
    expect(h).not.toContain("UA");
    expect(await sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });
});

describe("signal chips say what matched, never values", () => {
  it("names the match kinds", () => {
    expect(signalChips(["ce3_like_match", "device_reused", "address_reused"])).toEqual(["CE 3.0-like match", "matched on device + address"]);
    expect(signalChips(["partial_match", "ip_reused"])).toEqual(["partial match", "matched on IP"]);
    expect(signalChips(["buyer_prior_disputes", "buyer_serial_disputes"])).toEqual(["serial disputer"]);
    expect(signalChips(["address_match"])).toEqual([]);
  });
});

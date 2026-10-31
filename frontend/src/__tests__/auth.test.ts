// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { clearToken, getToken, setToken, tokenExpired } from "../auth";

const tok = (exp: number) => `${btoa(JSON.stringify({ kind: "session", exp })).replace(/=+$/, "")}.sig`;

describe("session token helpers", () => {
  afterEach(() => clearToken());
  it("stores and clears the session", () => {
    setToken("abc");
    expect(getToken()).toBe("abc");
    clearToken();
    expect(getToken()).toBeNull();
  });
  it("treats missing or garbage tokens as expired", () => {
    expect(tokenExpired(null)).toBe(true);
    expect(tokenExpired("garbage")).toBe(true);
  });
  it("reads expiry from the payload", () => {
    expect(tokenExpired(tok(Date.now() / 1000 + 3600))).toBe(false);
    expect(tokenExpired(tok(Date.now() / 1000 - 1))).toBe(true);
  });
});

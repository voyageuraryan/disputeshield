// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { clearToken, getToken, setToken, tokenExpired } from "../auth";
import { LOGIN_ERRORS, loginError } from "../pages/AuthCallback";

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

describe("login error messages", () => {
  it("shows only fixed messages, never text from the URL", () => {
    expect(loginError(new URLSearchParams("code=not_allowed&error=anything"))).toBe(LOGIN_ERRORS.not_allowed);
    expect(loginError(new URLSearchParams("error=Call+%2B1-555+to+unlock"))).toBe("Login did not complete.");
    expect(loginError(new URLSearchParams("code=__proto__"))).toBe("Login did not complete.");
    expect(loginError(null)).toBe("Login did not complete.");
  });
});

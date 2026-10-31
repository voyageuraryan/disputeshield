// Merchant session (issued by the backend after Log in with PayPal).
// Kept in sessionStorage: survives reloads, ends when the tab/browser closes.
const KEY = "ds_session";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(t: string) {
  sessionStorage.setItem(KEY, t);
}

export function clearToken() {
  sessionStorage.removeItem(KEY);
}

/** Expiry from the token payload (not trusted for security, only to avoid using a stale token). */
export function tokenExpired(t: string | null): boolean {
  if (!t) return true;
  try {
    const body = t.split(".")[0].replace(/-/g, "+").replace(/_/g, "/");
    const exp = JSON.parse(atob(body + "=".repeat((4 - (body.length % 4)) % 4))).exp as number;
    return !exp || exp * 1000 < Date.now();
  } catch {
    return true;
  }
}

export function goToLogin(reason?: string) {
  clearToken();
  const next = window.location.pathname + window.location.search;
  const q = new URLSearchParams({ next });
  if (reason) q.set("reason", reason);
  if (!window.location.pathname.startsWith("/login")) window.location.assign(`/login?${q}`);
}

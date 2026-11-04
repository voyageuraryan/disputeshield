import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { setToken } from "../auth";

/**
 * Lands here from the backend after Log in with PayPal: #token=...&next=... or #error=...
 * The result is derived from the router's location (so a new #fragment in the same tab is picked up) and
 * remembered in a ref, because we wipe it from the address bar right away (the router keeps its own copy,
 * so StrictMode's double effects in development still see it).
 */
/** Fixed messages per error code from the backend. Free text in the URL is never shown: anyone can craft a
 *  /auth/callback#error=... link, and showing its text on our domain would let them put words in our mouth. */
export const LOGIN_ERRORS: Record<string, string> = {
  cancelled: "Login was cancelled or refused at PayPal.",
  no_code: "PayPal did not return a login code.",
  state: "This login link has expired or was already used. Please start again.",
  paypal_failed: "PayPal login failed. Please try again.",
  not_configured: "No merchant is configured on the server yet.",
  not_allowed: "This PayPal account is not allowed to manage this store.",
};

export const loginError = (params: URLSearchParams | null): string => {
  const code = params?.get("code") ?? "";
  return Object.prototype.hasOwnProperty.call(LOGIN_ERRORS, code) ? LOGIN_ERRORS[code] : "Login did not complete.";
};

export default function AuthCallback() {
  const navigate = useNavigate();
  const { hash, pathname } = useLocation();
  const last = useRef<URLSearchParams | null>(null);
  const f = useMemo(() => (hash.length > 1 ? new URLSearchParams(hash.slice(1)) : null), [hash]);
  if (f) last.current = f;
  const params = last.current;
  const token = params?.get("token") ?? null;
  const error = token ? null : loginError(params);

  useEffect(() => {
    if (window.location.hash) history.replaceState(history.state, "", pathname); // drop the token from the bar
    if (!token) return;
    const next = params?.get("next");
    setToken(token);
    navigate(next && next.startsWith("/") && !next.startsWith("//") ? next : "/queue", { replace: true });
  }, [token, params, pathname, navigate]);

  if (!error) return <p className="text-sm text-slate-500">Signing you in…</p>;
  return (
    <div className="mx-auto mt-10 max-w-sm rounded-xl border border-slate-200 bg-white p-6 text-center shadow-sm" role="alert">
      <p className="font-medium">Couldn't sign you in</p>
      <p className="mt-1 text-sm text-slate-600">{error}</p>
      <Link to="/login" className="mt-4 inline-block rounded-md bg-indigo-600 px-4 py-2 text-sm text-white">Try again</Link>
    </div>
  );
}

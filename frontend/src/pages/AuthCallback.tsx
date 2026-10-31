import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { setToken } from "../auth";

/**
 * Lands here from the backend after Log in with PayPal: #token=...&next=... or #error=...
 * The result is derived from the router's location (so a new #fragment in the same tab is picked up) and
 * remembered in a ref, because we wipe it from the address bar right away (the router keeps its own copy,
 * so StrictMode's double effects in development still see it).
 */
export default function AuthCallback() {
  const navigate = useNavigate();
  const { hash, pathname } = useLocation();
  const last = useRef<URLSearchParams | null>(null);
  const f = useMemo(() => (hash.length > 1 ? new URLSearchParams(hash.slice(1)) : null), [hash]);
  if (f) last.current = f;
  const params = last.current;
  const token = params?.get("token") ?? null;
  const error = token ? null : params?.get("error") || "Login did not complete.";

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

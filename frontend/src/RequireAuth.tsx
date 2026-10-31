import { useEffect, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { authApi } from "./api";
import { getToken, tokenExpired } from "./auth";

/** Gate for merchant pages. Asks the backend whether login is on (AUTH_MODE=off in local dev). */
export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const loc = useLocation();
  const [mode, setMode] = useState<"paypal" | "off" | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    authApi.config().then((c) => setMode(c.mode)).catch((e) => setError(e.message));
  }, []);
  if (error) return <p className="rounded-md bg-red-50 p-3 text-sm text-red-700" role="alert">{error}</p>;
  if (mode === null) return <p className="text-sm text-slate-500">Loading…</p>;
  if (mode === "paypal" && tokenExpired(getToken())) {
    const next = loc.pathname + loc.search;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}

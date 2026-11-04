import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { authApi } from "./api";
import { clearToken, getToken, tokenExpired } from "./auth";
import NewDisputeAlerts from "./NewDisputeAlerts";

const link = ({ isActive }: { isActive: boolean }) =>
  `px-2.5 py-2 rounded-md text-sm font-medium sm:px-3 ${isActive ? "bg-indigo-50 text-indigo-700" : "text-slate-600 hover:bg-slate-100"}`;

function Account() {
  const loc = useLocation();
  const navigate = useNavigate();
  const [who, setWho] = useState<string | null>(null);
  const signedIn = !tokenExpired(getToken());
  useEffect(() => {
    if (!signedIn) return setWho(null);
    authApi
      .me()
      .then((m) => setWho(m.name || m.email))
      .catch(() => setWho(null));
  }, [signedIn, loc.pathname]);
  if (!signedIn) return null;
  return (
    <div className="ml-auto flex items-center gap-2 text-sm">
      {who && <span className="hidden max-w-[16rem] truncate text-slate-500 md:inline" title={who}>{who}</span>}
      <button
        onClick={() => {
          clearToken();
          navigate("/login", { replace: true });
        }}
        className="rounded-md border border-slate-300 px-2.5 py-1.5 text-slate-700 hover:bg-slate-50"
      >
        Log out
      </button>
    </div>
  );
}

/** Merchant pages (queue, orders, settings) appear only for a signed-in merchant (or when login is switched off
 * for local dev). Buyers using the store, and anyone signed out, see only the Store tab. */
export function useMerchantNav(): boolean {
  useLocation(); // re-check the session on navigation (e.g. right after logging in or out)
  const [authOff, setAuthOff] = useState(false);
  useEffect(() => {
    authApi
      .config()
      .then((c) => setAuthOff(c.mode === "off"))
      .catch(() => setAuthOff(false));
  }, []);
  return authOff || !tokenExpired(getToken());
}

export default function Layout() {
  const merchant = useMerchantNav();
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-2 px-3 py-3 sm:px-4">
          <span className="text-lg font-semibold">DisputeShield</span>
          <nav className="flex gap-1 sm:gap-2">
            {merchant && <NavLink to="/queue" className={link}>Disputes</NavLink>}
            {merchant && <NavLink to="/orders" className={link}>Orders</NavLink>}
            {merchant && <NavLink to="/analytics" className={link}>Analytics</NavLink>}
            <NavLink to="/store" className={link}>Store</NavLink>
            {merchant && <NavLink to="/settings" className={link}>Settings</NavLink>}
          </nav>
          <Account />
        </div>
      </header>
      <NewDisputeAlerts />
      <main className="mx-auto max-w-7xl px-3 py-4 sm:px-4 sm:py-6">
        <Outlet />
      </main>
    </div>
  );
}

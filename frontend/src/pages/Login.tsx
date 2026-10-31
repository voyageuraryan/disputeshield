import { useSearchParams } from "react-router-dom";
import { authApi } from "../api";

export default function Login() {
  const [q] = useSearchParams();
  const next = q.get("next") || "/queue";
  const reason = q.get("reason");
  return (
    <div className="mx-auto mt-10 max-w-sm rounded-xl border border-slate-200 bg-white p-6 text-center shadow-sm">
      <h1 className="text-lg font-semibold">Sign in to DisputeShield</h1>
      <p className="mt-1 text-sm text-slate-500">Use the PayPal account of the store you manage.</p>
      {reason && <p className="mt-3 rounded-md bg-amber-50 p-2 text-sm text-amber-800" role="status">{reason}</p>}
      <a
        href={authApi.startUrl(next)}
        className="mt-5 flex w-full items-center justify-center gap-2 rounded-md bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-indigo-700"
      >
        Log in with PayPal
      </a>
      <p className="mt-4 text-xs text-slate-400">Sandbox only. Only the store's approved PayPal accounts can sign in.</p>
    </div>
  );
}

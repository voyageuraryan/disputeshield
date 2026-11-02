import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, helpApi, type BuyerSession } from "../api";
import { when } from "../format";

const KEY = (id: string) => `ds_help_${id}`;

function readToken(id: string): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get("t");
  try {
    if (fromUrl) {
      sessionStorage.setItem(KEY(id), fromUrl);
      // Keep the private token out of the address bar, history and screenshots; this tab remembers it.
      window.history.replaceState(null, "", window.location.pathname);
      return fromUrl;
    }
    return sessionStorage.getItem(KEY(id));
  } catch {
    return fromUrl; // storage blocked: the URL still works
  }
}

const STATUS_TEXT: Record<string, string> = {
  INVITED: "Ready when you are.",
  ACTIVE: "In progress.",
  AWAITING_PROOF: "Waiting for your photo or video.",
  RESOLVED_FIXED: "Fixed. Thank you!",
  DEFECT_CONFIRMED: "Thanks: the seller now has what they need to put this right.",
  SAFETY_STOP: "Stopped for your safety. Please don't use the item; the seller will contact you.",
  HANDED_TO_MERCHANT: "The seller is taking it from here.",
};

/** The buyer's private troubleshooting page (opened from the link in the PayPal dispute message). */
export default function Help() {
  const { sessionId = "" } = useParams();
  const [token] = useState(() => readToken(sessionId));
  const [data, setData] = useState<BuyerSession | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);

  useEffect(() => {
    if (!token) {
      setError({ status: 404, text: "This link is incomplete. Please open the full link from the seller's message." });
      return;
    }
    helpApi
      .get(sessionId, token)
      .then(setData)
      .catch((e: Error) => setError({ status: e instanceof ApiError ? e.status : 0, text: e.message }));
  }, [sessionId, token]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-2xl px-4 py-3 text-sm font-semibold">Product help</div>
      </header>
      <main className="mx-auto max-w-2xl space-y-4 px-4 py-6">
        {error ? (
          <div role="alert" className={`rounded-lg p-4 text-sm ${error.status === 410 ? "bg-slate-100 text-slate-700" : "bg-red-50 text-red-800"}`}>
            {error.text}
          </div>
        ) : !data ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : (
          <>
            <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
              <h1 className="text-lg font-semibold">
                Let's get your {data.product_name} working
              </h1>
              {data.product_model && <p className="text-sm text-slate-500">Model {data.product_model}</p>}
              <p className="mt-3 text-sm text-slate-700">{STATUS_TEXT[data.status] ?? ""}</p>
              <p className="mt-1 text-xs text-slate-500">This page is private to you and works until {when(data.expires_at)}.</p>
            </section>
            <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="font-semibold">Steps</h2>
              {data.steps.length === 0 ? (
                <p className="mt-2 text-sm text-slate-600">
                  Your guided steps are being prepared. Please check back on this page soon; the link stays the same.
                </p>
              ) : (
                <ol className="mt-2 space-y-3">
                  {data.steps.map((s) => (
                    <li key={s.id} className="rounded-md border border-slate-200 p-3 text-sm">
                      <p className="font-medium">
                        {s.ordinal}. {s.title}
                      </p>
                      <p className="mt-1 text-slate-700">{s.instruction}</p>
                      {s.why && <p className="mt-1 text-xs text-slate-500">Why: {s.why}</p>}
                    </li>
                  ))}
                </ol>
              )}
            </section>
            {data.messages.length > 0 && (
              <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                <h2 className="font-semibold">Messages</h2>
                <ul className="mt-2 space-y-2 text-sm">
                  {data.messages.map((m, i) => (
                    <li key={i}>
                      <span className="font-medium">{m.role === "buyer" ? "You" : m.role === "merchant" ? "Seller" : "Assistant"}:</span> {m.body}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
        <p className="text-center text-xs text-slate-400">If something smells hot, sparks or smokes, unplug it and stop using it.</p>
      </main>
    </div>
  );
}

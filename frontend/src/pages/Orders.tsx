import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ordersApi, type StoreOrder } from "../api";
import { addressLines, day, money } from "../format";
import ShippingPanel from "../ShippingPanel";

/** Paid store orders: where the merchant adds tracking when shipping, and records the carrier's delivery. */
export default function Orders() {
  const [orders, setOrders] = useState<StoreOrder[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const load = useCallback(
    () =>
      ordersApi
        .list()
        .then((o) => (setOrders(o), setError(null)))
        .catch((e: Error) => setError(e.message)),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold sm:text-xl">Orders</h1>
        <p className="text-sm text-slate-500">
          Add tracking when you ship; record delivery when the carrier confirms it. Both become evidence if the buyer disputes the payment.
        </p>
      </div>
      {saved && (
        <p role="status" className="rounded-md bg-emerald-50 p-2 text-sm text-emerald-800">
          {saved}
        </p>
      )}
      {error && (
        <p className="rounded-md bg-red-50 p-2 text-sm text-red-800">
          {error}{" "}
          <button onClick={load} className="font-medium underline">
            Retry
          </button>
        </p>
      )}
      {!orders && !error && <p className="text-sm text-slate-500">Loading…</p>}
      {orders && orders.length === 0 && <p className="text-sm text-slate-500">No paid orders yet. Buy something in the Store first.</p>}
      <div className="grid gap-4 md:grid-cols-2">
        {orders?.map((o) => (
          <section key={o.id} className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" aria-label={`Order ${o.id}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="font-semibold">
                Order #{o.id} · {money(o.amount, o.currency)}
              </h2>
              <span className="text-xs text-slate-500">{day(o.created_at)}</span>
            </div>
            <p className="text-sm">{(o.items ?? []).map((i) => `${i.quantity ?? 1}× ${i.name}`).join(", ")}</p>
            <p className="text-xs text-slate-500">
              {o.buyer_email ?? "buyer unknown"}
              {o.shipping_address && ` · ships to ${addressLines(o.shipping_address).join(", ")}`}
            </p>
            {o.disputes.length > 0 && (
              <p className="mt-1 text-xs">
                Disputed:{" "}
                {o.disputes.map((id) => (
                  <Link key={id} to={`/cases/${id}`} className="mr-2 font-medium text-indigo-700 hover:underline">
                    {id}
                  </Link>
                ))}
              </p>
            )}
            <div className="mt-3 border-t border-slate-100 pt-3">
              <ShippingPanel
                order={o}
                digitalOnly={o.digital_only}
                onSaved={(what) => {
                  setSaved(
                    `${what === "tracking" ? "Tracking saved and uploaded to PayPal" : "Delivery recorded"} for order #${o.id}.` +
                      (o.disputes.length ? " Open the dispute and click Re-analyse to include it." : ""),
                  );
                  load();
                }}
              />
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

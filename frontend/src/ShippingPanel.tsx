import { useState } from "react";
import { ordersApi, type DeliveryAddress, type Order, type Shipment } from "./api";
import { addressLines, day, label } from "./format";

// Carriers PayPal knows by code (anything else is sent as OTHER + the name you type).
const CARRIERS = ["USPS", "UPS", "FEDEX", "DHL"];

const input = "w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm focus:border-indigo-500 focus:outline-none";
const btn = "rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50";
const linkBtn = "text-xs font-medium text-indigo-700 hover:underline";

export const todayIso = () => new Date().toISOString().slice(0, 10);

/** Tracking + delivery for one order. Used on the Orders page and on a case page. */
export default function ShippingPanel({
  order,
  digitalOnly,
  onSaved,
}: {
  order: Order;
  digitalOnly?: boolean;
  onSaved: (what: "tracking" | "delivery") => void;
}) {
  const [adding, setAdding] = useState(false);
  if (digitalOnly) return <p className="text-sm text-slate-500">Digital item: nothing to ship.</p>;
  return (
    <div className="space-y-3">
      {order.shipments.length === 0 && <p className="text-sm text-slate-500">No tracking yet.</p>}
      {order.shipments.map((s) => (
        <ShipmentRow key={s.id} order={order} s={s} onSaved={() => onSaved("delivery")} />
      ))}
      {adding ? (
        <AddTracking
          orderId={order.id}
          onCancel={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            onSaved("tracking");
          }}
        />
      ) : (
        <button className={linkBtn} onClick={() => setAdding(true)}>
          + Add tracking
        </button>
      )}
    </div>
  );
}

function AddTracking({ orderId, onCancel, onSaved }: { orderId: number; onCancel: () => void; onSaved: () => void }) {
  const [carrier, setCarrier] = useState("USPS");
  const [other, setOther] = useState("");
  const [number, setNumber] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = carrier === "OTHER" ? other.trim() : carrier;
  const valid = name.length > 0 && number.trim().length > 0 && number.trim().length <= 64;
  return (
    <form
      className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await ordersApi.addTracking(orderId, name, number.trim());
          onSaved();
        } catch (err) {
          setError((err as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="text-xs text-slate-600">Saved here and uploaded to PayPal, so the buyer sees it in their PayPal activity.</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="text-xs text-slate-600">
          Carrier
          <select aria-label="Carrier" className={input} value={carrier} onChange={(e) => setCarrier(e.target.value)}>
            {CARRIERS.map((c) => (
              <option key={c}>{c}</option>
            ))}
            <option value="OTHER">Other…</option>
          </select>
        </label>
        {carrier === "OTHER" && (
          <label className="text-xs text-slate-600">
            Carrier name
            <input aria-label="Carrier name" className={input} value={other} onChange={(e) => setOther(e.target.value)} maxLength={64} />
          </label>
        )}
        <label className="text-xs text-slate-600 sm:col-span-2">
          Tracking number
          <input aria-label="Tracking number" className={input} value={number} onChange={(e) => setNumber(e.target.value)} maxLength={64} />
        </label>
      </div>
      {error && <p className="text-xs text-red-700">{error}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={!valid || busy} className={btn}>
          {busy ? "Saving…" : "Save tracking"}
        </button>
        <button type="button" onClick={onCancel} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
          Cancel
        </button>
      </div>
    </form>
  );
}

function ShipmentRow({ order, s, onSaved }: { order: Order; s: Shipment; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-slate-200 p-2 text-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">
          {s.carrier ?? "Carrier?"} {s.tracking_number ?? "(untracked)"}
        </span>
        <span className={s.delivered_at ? "text-emerald-700" : "text-slate-500"}>
          {s.delivered_at ? `Delivered ${day(s.delivered_at)}` : label(s.status)}
        </span>
      </div>
      {s.delivered_address && (
        <div className="text-xs text-slate-500">
          to {addressLines(s.delivered_address).join(", ")}
          {s.delivery_recorded_by === "merchant" && " · recorded by you"}
        </div>
      )}
      {s.tracking_number &&
        (open ? (
          <DeliveryForm
            order={order}
            s={s}
            onCancel={() => setOpen(false)}
            onSaved={() => {
              setOpen(false);
              onSaved();
            }}
          />
        ) : (
          <button className={`${linkBtn} mt-1`} onClick={() => setOpen(true)}>
            {s.delivered_at ? "Change delivery details" : "Record delivery"}
          </button>
        ))}
    </div>
  );
}

const EMPTY_ADDR: DeliveryAddress = { address_line_1: "", admin_area_2: "", admin_area_1: "", postal_code: "", country_code: "US" };

function DeliveryForm({ order, s, onCancel, onSaved }: { order: Order; s: Shipment; onCancel: () => void; onSaved: () => void }) {
  const [date, setDate] = useState(todayIso());
  const [same, setSame] = useState(Boolean(order.shipping_address));
  const [addr, setAddr] = useState<DeliveryAddress>(EMPTY_ADDR);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const minDate = order.created_at.slice(0, 10);
  const addrOk = same || (addr.address_line_1.trim() && addr.postal_code.trim() && addr.country_code.trim().length === 2);
  const dateOk = date >= minDate && date <= todayIso();
  const set = (k: keyof DeliveryAddress) => (e: React.ChangeEvent<HTMLInputElement>) => setAddr({ ...addr, [k]: e.target.value });
  return (
    <form
      className="mt-2 space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await ordersApi.recordDelivery(order.id, s.id, date, same ? undefined : addr);
          onSaved();
        } catch (err) {
          setError((err as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="text-xs text-slate-600">
        Copy this from the carrier's tracking page for {s.tracking_number}. Only record a delivery the carrier confirms: it is
        sent to PayPal as proof.
      </p>
      <label className="block text-xs text-slate-600">
        Delivered on
        <input aria-label="Delivered on" type="date" className={input} value={date} min={minDate} max={todayIso()} onChange={(e) => setDate(e.target.value)} />
      </label>
      {!dateOk && <p className="text-xs text-red-700">Pick a date between the order date ({minDate}) and today.</p>}
      <fieldset className="space-y-1 text-sm">
        <legend className="text-xs text-slate-600">Delivered to</legend>
        <label className="flex items-start gap-2">
          <input type="radio" checked={same} disabled={!order.shipping_address} onChange={() => setSame(true)} />
          <span>
            The order's shipping address
            {order.shipping_address && <span className="block text-xs text-slate-500">{addressLines(order.shipping_address).join(", ")}</span>}
          </span>
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" checked={!same} onChange={() => setSame(false)} />A different address
        </label>
      </fieldset>
      {!same && (
        <div className="grid gap-2 sm:grid-cols-2">
          <input aria-label="Street" placeholder="Street *" className={`${input} sm:col-span-2`} value={addr.address_line_1} onChange={set("address_line_1")} />
          <input aria-label="City" placeholder="City" className={input} value={addr.admin_area_2} onChange={set("admin_area_2")} />
          <input aria-label="State" placeholder="State" className={input} value={addr.admin_area_1} onChange={set("admin_area_1")} />
          <input aria-label="Postal code" placeholder="Postal code *" className={input} value={addr.postal_code} onChange={set("postal_code")} />
          <input aria-label="Country" placeholder="Country (2 letters) *" maxLength={2} className={input} value={addr.country_code} onChange={set("country_code")} />
        </div>
      )}
      {error && <p className="text-xs text-red-700">{error}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={!addrOk || !dateOk || busy} className={btn}>
          {busy ? "Saving…" : "Save delivery"}
        </button>
        <button type="button" onClick={onCancel} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
          Cancel
        </button>
      </div>
    </form>
  );
}

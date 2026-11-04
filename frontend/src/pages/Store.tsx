import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Product } from "../api";
import { deviceFingerprint } from "../fingerprint";
import { initPayPal, type SdkInstance } from "../paypal";

type Status = { kind: "idle" | "working" | "ok" | "error"; text?: string };

function BuyButton({ sdk, product, payOrderId }: { sdk: SdkInstance; product: Product; payOrderId?: string }) {
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const onClick = async () => {
    setStatus({ kind: "working", text: "Opening PayPal…" });
    const session = sdk.createPayPalOneTimePaymentSession({
      onApprove: async ({ orderId }) => {
        setStatus({ kind: "working", text: "Capturing payment…" });
        try {
          const r = await api.captureOrder(orderId, await deviceFingerprint());
          setStatus({ kind: "ok", text: `Paid. Order #${r.order_id}, capture ${r.capture_id}` });
        } catch (e) {
          setStatus({ kind: "error", text: (e as Error).message });
        }
      },
      onCancel: () => setStatus({ kind: "idle" }),
      onError: (e) => setStatus({ kind: "error", text: e.message ?? "PayPal error" }),
    });
    try {
      // v6 expects a promise resolving to { orderId }; the server sets the price.
      // Phase 15: only a hash of the device fingerprint is sent (see fingerprint.ts)
      // Phase 16: a demo scenario already created the PayPal order: pay that exact order
      const order = payOrderId
        ? Promise.resolve({ orderId: payOrderId })
        : deviceFingerprint().then((fp) => api.createOrder(product.sku, fp)).then((o) => ({ orderId: o.id }));
      await session.start({ presentationMode: "auto" }, order);
    } catch (e) {
      setStatus({ kind: "error", text: (e as Error).message });
    }
  };

  return (
    <div className="space-y-2">
      <paypal-button type="pay" onClick={onClick} />
      {status.text && (
        <p className={`text-xs ${status.kind === "error" ? "text-red-600" : status.kind === "ok" ? "text-green-700" : "text-slate-500"}`}>
          {status.text}
        </p>
      )}
    </div>
  );
}

export default function Store() {
  const [params] = useSearchParams();
  // Phase 16: a demo scenario link (?pay=<PayPal order id>&sku=...) pays that exact, already-created order
  const payOrderId = /^[A-Z0-9]{8,32}$/.test(params.get("pay") ?? "") ? (params.get("pay") as string) : undefined;
  const paySku = params.get("sku") ?? undefined;
  const [products, setProducts] = useState<Product[]>([]);
  const [sdk, setSdk] = useState<SdkInstance | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cfg, prods] = await Promise.all([api.storeConfig(), api.products()]);
        if (cancelled) return;
        setProducts(prods);
        const instance = await initPayPal(cfg.sdk_url, cfg.client_id);
        const methods = await instance.findEligibleMethods({ currencyCode: cfg.currency });
        if (!methods.isEligible("paypal")) throw new Error("PayPal is not eligible for this buyer/currency");
        if (!cancelled) setSdk(instance);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Demo store</h1>
        <p className="text-sm text-slate-500">PayPal sandbox checkout. Buy with a sandbox personal account, then open a dispute from that account.</p>
        <p className="mt-2 rounded-md bg-slate-100 p-2 text-xs text-slate-600">
          Use a <span className="font-medium">United States</span> sandbox buyer account: this store's sandbox seller is
          registered in India, and PayPal blocks India-to-India payments (checkout then shows a generic error).
        </p>
      </div>
      {payOrderId && (
        <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900" data-testid="demo-order">
          Demo scenario order {payOrderId}: pay for the highlighted product below with the sandbox buyer. Other products start a
          normal new order.
        </p>
      )}
      {error && <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {products.map((p) => (
          <div key={p.sku} className={`flex flex-col rounded-lg border bg-white p-4 shadow-sm ${payOrderId && p.sku === paySku ? "ring-2 ring-amber-400" : ""}`}>
            <div className="mb-3 flex h-28 items-center justify-center rounded-md bg-slate-100 text-5xl">{p.image}</div>
            <h2 className="font-medium">{p.name}</h2>
            <p className="mb-2 text-lg font-semibold">${p.price}</p>
            <p className="mb-4 flex-1 text-xs text-slate-600">{p.listing}</p>
            {sdk ? <BuyButton sdk={sdk} product={p} payOrderId={p.sku === paySku ? payOrderId : undefined} /> : !error && <p className="text-xs text-slate-400">Loading PayPal…</p>}
          </div>
        ))}
      </div>
    </div>
  );
}

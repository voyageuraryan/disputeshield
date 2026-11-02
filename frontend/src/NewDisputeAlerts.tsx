import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { authApi, newDisputesUrl } from "./api";
import { getToken, tokenExpired } from "./auth";
import { label, money, TS_FINISHED_TEXT } from "./format";
import { useToast } from "./Toast";

export const NEW_DISPUTE_EVENT = "disputeshield:new-dispute";

/**
 * App-wide: listens for new disputes (SSE) while the merchant is signed in and shows a toast on any page.
 * Also broadcasts a window event so the queue can refresh immediately instead of waiting for its poll.
 */
export default function NewDisputeAlerts() {
  const toast = useToast();
  useLocation(); // re-check the session on navigation (e.g. right after logging in)
  const token = getToken();
  const [mode, setMode] = useState<"paypal" | "off" | null>(null);
  useEffect(() => {
    authApi.config().then((c) => setMode(c.mode)).catch(() => setMode(null));
  }, []);
  useEffect(() => {
    if (mode === null || (mode === "paypal" && tokenExpired(token))) return;
    const es = new EventSource(newDisputesUrl());
    es.addEventListener("dispute", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as {
        paypal_dispute_id: string;
        reason: string;
        amount: string;
        currency: string;
      };
      toast({ title: "New dispute", body: `${label(d.reason)} · ${money(d.amount, d.currency)}`, href: `/cases/${d.paypal_dispute_id}` });
      window.dispatchEvent(new CustomEvent(NEW_DISPUTE_EVENT, { detail: d }));
    });
    // Phase 9D: a buyer finished the troubleshooting page (the case is re-analysed with the result).
    es.addEventListener("troubleshooting", (e) => {
      const t = JSON.parse((e as MessageEvent).data) as { paypal_dispute_id: string; status: string };
      toast({
        title: "Buyer finished troubleshooting",
        body: `${t.paypal_dispute_id} · ${TS_FINISHED_TEXT[t.status] ?? label(t.status)}`,
        href: `/cases/${t.paypal_dispute_id}`,
      });
      window.dispatchEvent(new CustomEvent(NEW_DISPUTE_EVENT, { detail: t })); // the queue refreshes too
    });
    return () => es.close();
  }, [toast, token, mode]);
  return null;
}

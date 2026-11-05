import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { ApiError, authApi, merchantStream, newDisputesUrl, type StreamEvent } from "./api";
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
    // fetch() stream (session token in a header, never in the URL); reconnects with Last-Event-ID
    const abort = new AbortController();
    let closed = false;
    let lastId: string | undefined;
    const onEvent = (e: StreamEvent) => {
      if (e.id) lastId = e.id;
      if (e.event === "dispute") {
        const d = JSON.parse(e.data) as { paypal_dispute_id: string; reason: string; amount: string; currency: string };
        toast({ title: "New dispute", body: `${label(d.reason)} · ${money(d.amount, d.currency)}`, href: `/cases/${d.paypal_dispute_id}` });
        window.dispatchEvent(new CustomEvent(NEW_DISPUTE_EVENT, { detail: d }));
      } else if (e.event === "troubleshooting") {
        // Phase 9D: a buyer finished the troubleshooting page (the case is re-analysed with the result).
        const t = JSON.parse(e.data) as { paypal_dispute_id: string; status: string };
        toast({
          title: "Buyer finished troubleshooting",
          body: `${t.paypal_dispute_id} · ${TS_FINISHED_TEXT[t.status] ?? label(t.status)}`,
          href: `/cases/${t.paypal_dispute_id}`,
        });
        window.dispatchEvent(new CustomEvent(NEW_DISPUTE_EVENT, { detail: t })); // the queue refreshes too
      }
    };
    (async () => {
      let failures = 0;
      while (!closed) {
        try {
          await merchantStream(newDisputesUrl(), onEvent, abort.signal, lastId, () => {
            failures = 0;
          });
        } catch (err) {
          if (closed || (err instanceof ApiError && err.status >= 400 && err.status < 500)) return;
          failures += 1;
        }
        if (!closed) await new Promise((r) => setTimeout(r, Math.min(30_000, 3_000 * Math.max(1, failures))));
      }
    })();
    return () => {
      closed = true;
      abort.abort();
    };
  }, [toast, token, mode]);
  useEffect(() => {
    const onFileError = (e: Event) => toast({ title: "Couldn't open the file", body: String((e as CustomEvent).detail ?? "") });
    window.addEventListener("disputeshield:file-error", onFileError);
    return () => window.removeEventListener("disputeshield:file-error", onFileError);
  }, [toast]);
  return null;
}

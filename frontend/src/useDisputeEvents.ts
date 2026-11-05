import { useEffect, useRef, useState } from "react";
import { ApiError, disputesApi, eventsUrl, merchantStream, type Step } from "./api";

export type LiveState = "connecting" | "live" | "reconnecting" | "polling";

const MAX_ERRORS_BEFORE_POLLING = 5;
const POLL_MS = 5_000;
const RETRY_MS = 3_000;

/**
 * Live agent timeline for one dispute via Server-Sent Events, read with fetch() so the session token stays in a header
 * (never in the URL: security review 2026-11-05).
 * - Reconnects after the server ends a stream or the connection drops, sending Last-Event-ID, so no step is missed
 *   or duplicated (we also de-duplicate by id).
 * - If the stream keeps failing (proxy that blocks SSE, server down), fall back to polling the case.
 * - `onStep` fires for each new step so the page can refresh decisions when an agent finishes.
 */
export function useDisputeEvents(disputeId: string, onStep?: (s: Step) => void) {
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [state, setState] = useState<LiveState>("connecting");
  const cb = useRef(onStep);
  cb.current = onStep;

  useEffect(() => {
    let closed = false;
    let errors = 0;
    let poll: ReturnType<typeof setInterval> | undefined;
    const seen = new Set<number>();
    const abort = new AbortController();
    let lastId: string | undefined;
    let backlogDone = false; // steps before the first "ready" are history, not news
    setSteps(null);
    setState("connecting");

    const add = (incoming: Step[], notify: boolean) => {
      const fresh = incoming.filter((s) => !seen.has(s.id));
      if (!fresh.length) return;
      fresh.forEach((s) => seen.add(s.id));
      setSteps((prev) => [...(prev ?? []), ...fresh].sort((a, b) => a.id - b.id));
      if (notify) fresh.forEach((s) => cb.current?.(s));
    };

    const startPolling = () => {
      setState("polling");
      const tick = () => disputesApi.detail(disputeId).then((d) => add(d.timeline, true)).catch(() => undefined);
      tick();
      poll = setInterval(tick, POLL_MS);
    };

    (async () => {
      while (!closed) {
        try {
          await merchantStream(
            eventsUrl(disputeId),
            (e) => {
              if (e.id) lastId = e.id;
              if (e.event === "ready") {
                backlogDone = true;
                setSteps((prev) => prev ?? []);
              } else if (e.event === "step") add([JSON.parse(e.data)], backlogDone);
            },
            abort.signal,
            lastId,
            () => {
              errors = 0;
              if (!closed) setState("live");
            },
          );
          if (closed) return; // the server ended the stream ("bye"): reconnect at once with Last-Event-ID
        } catch (e) {
          if (closed) return;
          if (e instanceof ApiError && e.status >= 400 && e.status < 500) return startPolling();
          errors += 1;
          if (errors >= MAX_ERRORS_BEFORE_POLLING) return startPolling();
          setState("reconnecting");
          await new Promise((r) => setTimeout(r, RETRY_MS));
        }
      }
    })();

    return () => {
      closed = true;
      abort.abort();
      if (poll) clearInterval(poll);
    };
  }, [disputeId]);

  return { steps, state };
}

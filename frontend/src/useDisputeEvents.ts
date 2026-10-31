import { useEffect, useRef, useState } from "react";
import { disputesApi, eventsUrl, type Step } from "./api";

export type LiveState = "connecting" | "live" | "reconnecting" | "polling";

const MAX_ERRORS_BEFORE_POLLING = 5;
const POLL_MS = 5_000;

/**
 * Live agent timeline for one dispute via Server-Sent Events.
 * - EventSource reconnects by itself and resends Last-Event-ID, so no step is missed or duplicated
 *   (we also de-duplicate by id).
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
    setSteps(null);
    setState("connecting");

    const add = (incoming: Step[], notify: boolean) => {
      const fresh = incoming.filter((s) => !seen.has(s.id));
      if (!fresh.length) return;
      fresh.forEach((s) => seen.add(s.id));
      setSteps((prev) => [...(prev ?? []), ...fresh].sort((a, b) => a.id - b.id));
      if (notify) fresh.forEach((s) => cb.current?.(s));
    };

    const es = new EventSource(eventsUrl(disputeId));
    let backlogDone = false; // steps before the first "ready" are history, not news
    es.onopen = () => {
      errors = 0;
      if (!closed) setState("live");
    };
    es.addEventListener("ready", () => {
      backlogDone = true;
      setSteps((prev) => prev ?? []);
    });
    es.addEventListener("step", (e) => add([JSON.parse((e as MessageEvent).data)], backlogDone));
    es.onerror = () => {
      if (closed) return;
      errors += 1;
      if (errors < MAX_ERRORS_BEFORE_POLLING) {
        setState("reconnecting"); // EventSource retries on its own (server sends retry: 3000)
        return;
      }
      es.close();
      setState("polling");
      const tick = () => disputesApi.detail(disputeId).then((d) => add(d.timeline, true)).catch(() => undefined);
      tick();
      poll = setInterval(tick, POLL_MS);
    };

    return () => {
      closed = true;
      es.close();
      if (poll) clearInterval(poll);
    };
  }, [disputeId]);

  return { steps, state };
}

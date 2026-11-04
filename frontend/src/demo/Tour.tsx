// Phase 16: guided tour overlay. Highlights one element per step (by data-tour attribute) with a short explanation.
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { disputesApi } from "../api";
import { pickTourCase, stepPath, TOUR } from "./tour";

type Rect = { top: number; left: number; width: number; height: number };

export default function Tour({ onClose }: { onClose: () => void }) {
  const [i, setI] = useState(0);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [rect, setRect] = useState<Rect | null>(null);
  const nav = useNavigate();
  const loc = useLocation();
  const step = TOUR[i];

  useEffect(() => {
    disputesApi.list().then((rows) => setCaseId(pickTourCase(rows)), () => setCaseId(null));
  }, []);

  useEffect(() => {
    const path = stepPath(step, caseId);
    if (loc.pathname !== path) nav(path);
  }, [i, caseId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setRect(null);
    let tries = 0;
    const find = () => {
      const el = (document.querySelector(step.selector) ?? (step.fallback ? document.querySelector(step.fallback) : null)) as HTMLElement | null;
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        const place = () => {
          const r = el.getBoundingClientRect();
          setRect({ top: r.top - 6, left: r.left - 6, width: r.width + 12, height: r.height + 12 });
        };
        setTimeout(place, 350);
        window.addEventListener("resize", place);
        window.addEventListener("scroll", place, true);
        return () => {
          window.removeEventListener("resize", place);
          window.removeEventListener("scroll", place, true);
        };
      }
      if (tries++ < 30) timer = setTimeout(find, 150);
      return undefined;
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const cleanup = find();
    return () => {
      clearTimeout(timer);
      cleanup?.();
    };
  }, [i, loc.pathname]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);

  const last = i === TOUR.length - 1;
  const below = rect ? rect.top + rect.height + 200 < window.innerHeight : true;
  return (
    <div className="fixed inset-0 z-[60]" role="dialog" aria-modal="true" aria-label="Guided tour" data-testid="tour">
      {rect ? (
        <div className="pointer-events-none fixed rounded-lg ring-4 ring-indigo-500 transition-all"
          style={{ ...rect, boxShadow: "0 0 0 9999px rgba(15,23,42,0.55)" }} />
      ) : (
        <div className="fixed inset-0 bg-slate-900/55" />
      )}
      <div
        className="fixed left-1/2 w-[min(92vw,26rem)] -translate-x-1/2 rounded-lg bg-white p-4 shadow-xl"
        style={rect ? (below ? { top: Math.min(rect.top + rect.height + 12, window.innerHeight - 220) } : { top: Math.max(12, rect.top - 212) }) : { top: "30%" }}
      >
        <p className="text-xs font-medium text-indigo-700">Step {i + 1} of {TOUR.length}</p>
        <h2 className="mt-1 font-semibold">{step.title}</h2>
        <p className="mt-1 text-sm text-slate-700">{step.body}</p>
        {!rect && <p className="mt-2 text-xs text-slate-500">Looking for this part of the page… (it appears once a case has been analysed)</p>}
        <div className="mt-3 flex items-center justify-between">
          <button onClick={onClose} className="text-sm text-slate-500 hover:underline">End tour</button>
          <div className="flex gap-2">
            {i > 0 && <button onClick={() => setI(i - 1)} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Back</button>}
            <button onClick={() => (last ? onClose() : setI(i + 1))} className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700">
              {last ? "Finish" : "Next"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

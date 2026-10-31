import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

type ToastItem = { id: number; title: string; body?: string; href?: string };
const Ctx = createContext<(t: Omit<ToastItem, "id">) => void>(() => undefined);

export const useToast = () => useContext(Ctx);

function ToastView({ t, onClose }: { t: ToastItem; onClose: () => void }) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const timer = setTimeout(() => close.current(), 8000); // once per toast, not per re-render
    return () => clearTimeout(timer);
  }, []);
  return (
    <div role="status" className="pointer-events-auto w-full rounded-lg border border-indigo-200 bg-white p-3 shadow-lg sm:w-80">
      <div className="flex items-start gap-3">
        <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-indigo-600" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-slate-900">{t.title}</p>
          {t.body && <p className="truncate text-sm text-slate-600">{t.body}</p>}
          {t.href && (
            <Link to={t.href} onClick={onClose} className="mt-1 inline-block text-sm font-medium text-indigo-700 hover:underline">
              Open case →
            </Link>
          )}
        </div>
        <button onClick={onClose} aria-label="Dismiss" className="rounded px-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600">
          ✕
        </button>
      </div>
    </div>
  );
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((t: Omit<ToastItem, "id">) => setItems((xs) => [...xs.slice(-2), { ...t, id: Date.now() + Math.random() }]), []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed inset-x-3 bottom-3 z-50 flex flex-col items-end gap-2 sm:inset-x-auto sm:right-4">
        {items.map((t) => (
          <ToastView key={t.id} t={t} onClose={() => setItems((xs) => xs.filter((x) => x.id !== t.id))} />
        ))}
      </div>
    </Ctx.Provider>
  );
}

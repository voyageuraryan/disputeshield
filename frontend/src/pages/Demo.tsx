// Phase 16: public, read-only landing page for judges (no sign-in). Content in src/demo/content.ts (= docs/DEVPOST.md).
import { Link } from "react-router-dom";
import { AI_USES, EXPLAINER, GUARDRAILS_SHORT, PAYPAL_USES, SECTIONS, TAGLINE } from "../demo/content";

function Architecture() {
  // Inline SVG: boxes = components, arrows = data flow. Colours: the app's indigo + slate.
  const box = (x: number, y: number, w: number, label: string, sub: string, accent = false) => (
    <g>
      <rect x={x} y={y} width={w} height={54} rx={8} fill={accent ? "#eef2ff" : "#ffffff"} stroke={accent ? "#4f46e5" : "#94a3b8"} strokeWidth={1.5} />
      <text x={x + w / 2} y={y + 23} textAnchor="middle" fontSize={13} fontWeight={600} fill="#0f172a">{label}</text>
      <text x={x + w / 2} y={y + 41} textAnchor="middle" fontSize={11} fill="#475569">{sub}</text>
    </g>
  );
  const arrow = (x1: number, y1: number, x2: number, y2: number) => (
    <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#64748b" strokeWidth={1.5} markerEnd="url(#arrow)" />
  );
  return (
    <svg viewBox="0 0 760 330" role="img" aria-label="Architecture: PayPal webhooks and APIs feed the FastAPI backend, where a LangGraph pipeline of investigator, rules, analyst and resolution agents writes to SQLite and the timeline; the React dashboard shows it and the merchant approves before anything is sent back to PayPal." className="w-full">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="#64748b" />
        </marker>
      </defs>
      {box(10, 130, 170, "PayPal (sandbox)", "Disputes · Orders · Webhooks")}
      {box(10, 250, 170, "Buyer", "store checkout · help page")}
      <rect x={220} y={8} width={330} height={314} rx={10} fill="#f8fafc" stroke="#cbd5e1" strokeDasharray="4 4" />
      <text x={385} y={28} textAnchor="middle" fontSize={11} fill="#475569">FastAPI backend · LangGraph</text>
      {box(240, 40, 135, "Investigator", "PayPal + store evidence")}
      {box(395, 40, 135, "Rules + money", "checklist · calculator")}
      {box(240, 130, 135, "Resolution", "note · PDF · request")}
      {box(395, 130, 135, "Analyst (LLM)", "explains · cites ids", true)}
      {box(240, 220, 290, "Approval gate (rule 5) → PayPal call", "nothing is sent until the merchant approves", true)}
      {box(590, 40, 160, "SQLite", "cases · evidence · steps")}
      {box(590, 130, 160, "Groq / Gemini", "structured output")}
      {box(590, 220, 160, "Merchant dashboard", "React · AG Grid · AG Charts")}
      {/* webhooks / reads in; the approved response goes back out */}
      {arrow(180, 145, 238, 78)}
      {arrow(238, 255, 182, 172)}
      {arrow(95, 250, 95, 186)}
      {/* the pipeline */}
      {arrow(375, 67, 393, 67)}
      {arrow(462, 94, 462, 128)}
      {arrow(530, 157, 588, 157)}
      {arrow(395, 157, 377, 157)}
      {arrow(307, 184, 307, 218)}
      {/* storage and the merchant */}
      {arrow(530, 60, 588, 60)}
      {arrow(588, 247, 532, 247)}
    </svg>
  );
}

export default function Demo() {
  return (
    <article className="mx-auto max-w-4xl space-y-8 pb-12" data-testid="demo-landing">
      <header className="space-y-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-indigo-700">PayPal AI Hackathon · DisputeShield</p>
        <h1 className="text-2xl font-semibold sm:text-3xl">DisputeShield</h1>
        <h2 className="text-lg font-semibold">{SECTIONS[0]}</h2>
        <p className="text-base text-slate-700">{TAGLINE}</p>
        <div className="flex flex-wrap gap-2 text-sm">
          <Link to="/store" className="rounded-md bg-indigo-600 px-3 py-1.5 font-medium text-white hover:bg-indigo-700">Try the demo store</Link>
          <Link to="/queue" className="rounded-md border border-slate-300 px-3 py-1.5 hover:bg-slate-50">Merchant dashboard (sign in)</Link>
        </div>
      </header>
      <section>
        <h2 className="mb-3 text-lg font-semibold">{SECTIONS[1]}</h2>
        <ol className="space-y-2">
          {EXPLAINER.map((e) => (
            <li key={e.t} className="flex gap-3 text-sm">
              <span className="w-10 shrink-0 font-mono text-xs text-indigo-700">{e.t}</span>
              <span className="text-slate-700">{e.text}</span>
            </li>
          ))}
        </ol>
      </section>
      <section className="grid gap-6 md:grid-cols-2">
        {([[SECTIONS[2], PAYPAL_USES], [SECTIONS[3], AI_USES]] as const).map(([title, items]) => (
          <div key={title}>
            <h2 className="mb-3 text-lg font-semibold">{title}</h2>
            <ul className="space-y-2 text-sm">
              {items.map((i) => (
                <li key={i.name}><span className="font-medium">{i.name}:</span> <span className="text-slate-700">{i.text}</span></li>
              ))}
            </ul>
          </div>
        ))}
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">{SECTIONS[4]}</h2>
        <div className="rounded-lg border border-slate-200 bg-white p-3"><Architecture /></div>
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">{SECTIONS[5]}</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
          {GUARDRAILS_SHORT.map((g) => <li key={g}>{g}</li>)}
        </ul>
      </section>
    </article>
  );
}

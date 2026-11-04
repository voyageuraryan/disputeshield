// Phase 16: the safety story. Latest eval report (cards + table), the guardrails with where each is enforced in code,
// and live decision counters.
import { useEffect, useState } from "react";
import { trustApi, type EvalReport, type Guardrail, type TrustCounters } from "../api";
import { when } from "../format";
import { codeRef, pctText, trustCards } from "../trust/logic";

export default function Trust() {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [reportErr, setReportErr] = useState<string | null>(null);
  const [trust, setTrust] = useState<{ guardrails: Guardrail[]; counters: TrustCounters } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    trustApi.evals().then(setReport, (e: Error) => setReportErr(e.message));
    trustApi.trust().then(setTrust, (e: Error) => setErr(e.message));
  }, []);
  const c = trust?.counters;
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Trust & safety</h1>
        <p className="text-sm text-slate-600">How the agents are tested, what stops them from doing harm, and what they have done so far.</p>
      </div>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" data-testid="eval-section">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Latest evaluation (DeepEval)</h2>
          {report && (
            <span className="text-xs text-slate-500">
              {when(report.generated)} · {report.ran} of {report.goldens} test cases · model {report.model_under_test.join(", ")} · judge {report.judge}
            </span>
          )}
        </div>
        {reportErr && <p className="text-sm text-slate-600">No eval report yet ({reportErr}). Run <code>make evals</code>.</p>}
        {report && (
          <>
            {!report.gates.passed && (
              <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900" role="status">
                Quality gates not passed{report.gates.partial ? " (partial run)" : ""}: {report.gates.failures.join("; ")}. Shown as measured, not rounded up.
              </p>
            )}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="eval-cards">
              {trustCards(report).map((k) => (
                <div key={k.key} className="rounded-lg border border-slate-200 p-3">
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{k.name}</p>
                  <p className="mt-1 text-2xl font-semibold tabular-nums">{pctText(k.value)}</p>
                  <p className="mt-0.5 text-xs text-slate-500">{k.detail}</p>
                </div>
              ))}
            </div>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-sm" data-testid="eval-table">
                <thead className="text-xs uppercase text-slate-500">
                  <tr>
                    <th className="py-1 pr-3">Metric</th><th className="py-1 pr-3">Kind</th><th className="py-1 pr-3">Passed</th>
                    <th className="py-1 pr-3">Pass rate</th><th className="py-1 pr-3">Average</th><th className="py-1">Errored</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report.metrics).map(([name, m]) => (
                    <tr key={name} className="border-t border-slate-100">
                      <td className="py-1 pr-3 font-medium">{name}</td>
                      <td className="py-1 pr-3 text-slate-600">{m.kind}</td>
                      <td className="py-1 pr-3 tabular-nums">{m.passed}/{m.measured}</td>
                      <td className="py-1 pr-3 tabular-nums">{pctText(m.pass_rate)}</td>
                      <td className="py-1 pr-3 tabular-nums">{m.average == null ? "—" : m.average.toFixed(2)}</td>
                      <td className="py-1 tabular-nums">{m.errored}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">What the agents have done (live)</h2>
        {err && <p className="text-sm text-red-700">{err}</p>}
        {c && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6" data-testid="counters">
            {([["Proposed", c.proposed], ["Approved by you", c.approved], ["Rejected", c.rejected], ["You overrode", c.overridden],
              ["Sent to PayPal", c.executed], ["Needed your review", c.needs_human]] as const).map(([k, v]) => (
              <div key={k} className="rounded-lg border border-slate-200 p-3">
                <p className="text-xs uppercase tracking-wide text-slate-500">{k}</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">{v}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Guardrails and where they live in the code</h2>
        <ul className="space-y-3" data-testid="guardrails">
          {trust?.guardrails.map((g) => (
            <li key={g.key} className="rounded-md border border-slate-200 p-3">
              <p className="font-medium">
                {g.name}
                {g.rule && <span className="ml-2 rounded bg-indigo-50 px-1.5 py-0.5 text-xs text-indigo-800">hard rule {g.rule}</span>}
              </p>
              <p className="mt-1 text-sm text-slate-700">{g.what}</p>
              <p className="mt-1 flex flex-wrap gap-1.5 text-xs">
                {g.where.map((r) => {
                  const { file, name } = codeRef(r);
                  return (
                    <code key={r} className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700" title={file}>
                      {file.replace("backend/app/", "")} · {name}
                    </code>
                  );
                })}
              </p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

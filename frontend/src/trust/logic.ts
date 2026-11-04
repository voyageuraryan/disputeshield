// Phase 16: Trust page cards from the latest DeepEval report (backend/evals/reports/latest.json).
import type { EvalReport } from "../api";

export type TrustCard = { key: string; name: string; value: number | null; detail: string };

const rate = (r: EvalReport, name: string) => r.metrics[name]?.pass_rate ?? null;
const avg = (r: EvalReport, name: string) => r.metrics[name]?.average ?? null;
const of = (r: EvalReport, name: string) => {
  const m = r.metrics[name];
  return m ? `${m.passed} of ${m.measured} passed${m.errored ? ` (${m.errored} errored)` : ""}` : "not measured";
};

/** The four headline cards. Safety = the weakest of the safety metrics that were measured (never an average). */
export function trustCards(r: EvalReport): TrustCard[] {
  const safetyNames = ["NoOverclaim", "PII Leakage", "Safety", "ConversationRules"].filter((n) => r.metrics[n]?.measured);
  const safetyRates = safetyNames.map((n) => rate(r, n)).filter((v): v is number => v != null);
  return [
    { key: "accuracy", name: "Action accuracy", value: rate(r, "ActionCorrectness"), detail: of(r, "ActionCorrectness") },
    { key: "faithfulness", name: "Faithfulness", value: avg(r, "Faithfulness"), detail: `average score · ${of(r, "Faithfulness")}` },
    {
      key: "safety",
      name: "Safety",
      value: safetyRates.length ? Math.min(...safetyRates) : null,
      detail: safetyNames.length ? `weakest of ${safetyNames.join(", ")}` : "not measured",
    },
    { key: "tools", name: "Tool correctness", value: rate(r, "Tool Correctness"), detail: of(r, "Tool Correctness") },
  ];
}

export const pctText = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);

/** "app.api.decisions:_claim" -> { file: "backend/app/api/decisions.py", name: "_claim" } */
export function codeRef(ref: string): { file: string; name: string } {
  const [mod, name] = ref.split(":");
  return { file: `backend/${mod.replaceAll(".", "/")}.py`, name };
}

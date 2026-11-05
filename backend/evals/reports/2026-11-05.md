# Eval report 2026-11-05

**Gates: PARTIAL / FAILED**

- incomplete run: 3 golden(s) skipped, 194 metric result(s) errored (set EVALS_ALLOW_PARTIAL=1 to accept a partial run)

Model under test: groq:openai/gpt-oss-120b:t0.0:s7 (providers: groq:openai/gpt-oss-120b); judge: gemini-judge (gemini-3.8-flash). Goldens 54, ran 51, skipped 3.
Latency per golden p50 43.4 s, p95 155.08 s. Tokens (model under test): 174698 in / 58035 out over 51 golden(s) with usage data.

| Metric | Kind | Passed | Average | Not measured |
|---|---|---|---|---|
| ActionCorrectness | deterministic | 51/51 (100%) | 1.0 | 0 |
| CitationValidity | deterministic | 51/51 (100%) | 1.0 | 0 |
| AllowedActionRespected | deterministic | 51/51 (100%) | 1.0 | 0 |
| BaselineDistance | deterministic | 50/51 (98%) | 0.98 | 0 |
| NoOverclaim | deterministic | 51/51 (100%) | 1.0 | 0 |
| LengthLimits | deterministic | 51/51 (100%) | 1.0 | 0 |
| Tool Correctness | agentic | 51/51 (100%) | 1.0 | 0 |
| Faithfulness | judged | - | None | 51 |
| Economic reasoning [GEval] | judged | - | None | 51 |
| Decision explanation [DAG] | judged | - | None | 51 |
| Seller note quality [GEval] | judged | - | None | 20 |
| PII Leakage | judged | - | None | 7 |
| Step Efficiency | agentic | - | None | 7 |
| Task Completion | agentic | - | None | 7 |

| Reason | Goldens | Action correct | Failed metric results |
|---|---|---|---|
| Not received | 19 | 19/19 (100%) | 0 |
| Not as described | 19 | 19/19 (100%) | 1 |
| Unauthorised | 13 | 13/13 (100%) | 0 |

Confusion matrix (rows: expected, columns: recommended)

| expected \ got | FIGHT | SETTLE | SETTLE_PARTIAL | REPLACEMENT | GATHER_MORE | NEEDS_HUMAN |
|---|---|---|---|---|---|---|
| FIGHT | 20 | 0 | 0 | 0 | 0 | 0 |
| SETTLE | 0 | 18 | 0 | 0 | 1 | 0 |
| GATHER_MORE | 0 | 4 | 0 | 0 | 8 | 0 |

## Worst cases

- **ts-defect** (expected GATHER_MORE/SETTLE, got SETTLE)
  - BaselineDistance (0.0): moved GATHER_MORE -> SETTLE without citing a buyer_message or listing

# Eval report 2026-11-02

**Gates: PARTIAL / FAILED**

- ActionCorrectness 40/51 = 78% < 90%
- incomplete run: 0 golden(s) skipped, 38 metric result(s) errored (set EVALS_ALLOW_PARTIAL=1 to accept a partial run)

Model under test: groq:openai/gpt-oss-120b (providers: groq:openai/gpt-oss-120b); judge: gemini-judge (gemini-3.5-flash-lite). Goldens 51, ran 51.
Latency per golden p50 30.9 s, p95 72.58 s. Tokens (model under test): 21484 in / 9808 out over 11 golden(s) with usage data.

| Metric | Kind | Passed | Average | Not measured |
|---|---|---|---|---|
| ActionCorrectness | deterministic | 40/51 (78%) | 0.784 | 0 |
| CitationValidity | deterministic | 51/51 (100%) | 1.0 | 0 |
| AllowedActionRespected | deterministic | 51/51 (100%) | 1.0 | 0 |
| BaselineDistance | deterministic | 51/51 (100%) | 1.0 | 0 |
| NoOverclaim | deterministic | 51/51 (100%) | 1.0 | 0 |
| LengthLimits | deterministic | 51/51 (100%) | 1.0 | 0 |
| Tool Correctness | agentic | 51/51 (100%) | 1.0 | 0 |
| Faithfulness | judged | 49/49 (100%) | 0.954 | 1 |
| Economic reasoning [GEval] | judged | 40/49 (82%) | 0.865 | 1 |
| Decision explanation [DAG] | judged | 8/14 (57%) | 0.743 | 36 |
| Seller note quality [GEval] | judged | 18/19 (95%) | 0.879 | 0 |
| PII Leakage | judged | 8/8 (100%) | 1.0 | 0 |
| Step Efficiency | agentic | 8/8 (100%) | 1.0 | 0 |
| Task Completion | agentic | 8/8 (100%) | 0.963 | 0 |

| Reason | Goldens | Action correct | Failed metric results |
|---|---|---|---|
| Not received | 21 | 17/21 (81%) | 9 |
| Not as described | 18 | 11/18 (61%) | 14 |
| Unauthorised | 12 | 12/12 (100%) | 4 |

Confusion matrix (rows: expected, columns: recommended)

| expected \ got | FIGHT | SETTLE | GATHER_MORE | NEEDS_HUMAN |
|---|---|---|---|---|
| FIGHT | 19 | 0 | 0 | 0 |
| SETTLE | 0 | 11 | 9 | 0 |
| GATHER_MORE | 1 | 4 | 6 | 1 |

## Worst cases

- **grid-snad-partial-below-inquiry** (expected SETTLE, got GATHER_MORE)
  - ActionCorrectness (0.0): got GATHER_MORE, expected SETTLE
  - Economic reasoning [GEval] (0.5): The output partially follows the evaluation steps. It correctly identifies the missing evidence (refund policy), uses plain English, and weighs the small 15 USD dispute amount against the chances of winning. However, it fails to compare the disputed amount with the merchant's settle threshold of 25 USD where that matters for the decision, and it hallucinates an internal rule score (0.53) and thres
  - Decision explanation [DAG] (0.4): The score is 0.4 because the BinaryJudgementNode at Level == 0 successfully identified the recommended action as 'GATHER_MORE', but the BinaryJudgementNode at Level == 1 failed the criteria for naming decisive specific evidence, resulting in a final VerdictNode of False.
- **ts-defect** (expected GATHER_MORE/SETTLE, got FIGHT)
  - ActionCorrectness (0.0): got FIGHT, expected GATHER_MORE/SETTLE
  - Seller note quality [GEval] (0.3): The output violates step 5 by explicitly admitting that the buyer completed all required troubleshooting and provided proof of the fault, which directly weakens the merchant's case against a 'not as described' dispute. Additionally, it misinterprets the refund policy from evidence 3 by claiming the buyer has not satisfied the conditions when the text states faulty items are repaired, replaced, or 
- **grid-inr-weak-below-inquiry** (expected SETTLE, got GATHER_MORE)
  - ActionCorrectness (0.0): got GATHER_MORE, expected SETTLE
  - Decision explanation [DAG] (0.4): The score is 0.4 because the Level 0 BinaryJudgementNode verified that the explanation states the recommended action, but the Level 1 BinaryJudgementNode found that it failed to name specific evidence that decided the outcome, leading to a False verdict at the VerdictNode.
- **grid-snad-partial-below-chargeback** (expected SETTLE, got GATHER_MORE)
  - ActionCorrectness (0.0): got GATHER_MORE, expected SETTLE
  - Economic reasoning [GEval] (0.3): The output fails to recommend SETTLE despite the dispute amount of 15.00 USD being below the merchant settle threshold of 25.00 USD, ignoring the baseline rule from the context. It also fails to properly weigh the cost against fighting, missing the core threshold logic.
- **adv-address-mismatch** (expected GATHER_MORE, got NEEDS_HUMAN)
  - ActionCorrectness (0.0): got NEEDS_HUMAN, expected GATHER_MORE; problems: ['GATHER_MORE must name the missing items']

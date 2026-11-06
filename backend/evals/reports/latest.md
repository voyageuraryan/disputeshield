# Eval report 2026-11-04

**Gates: PARTIAL / FAILED**

- incomplete run: 3 golden(s) skipped, 1 metric result(s) errored (set EVALS_ALLOW_PARTIAL=1 to accept a partial run)

Model under test: groq:openai/gpt-oss-120b:t0.0:s7 (providers: groq:openai/gpt-oss-120b); judge: gemini-judge (gemini-2.5-flash). Goldens 54, ran 51, skipped 3.
Latency per golden p50 59.4 s, p95 90.72 s. Tokens (model under test): 175140 in / 58838 out over 51 golden(s) with usage data.

| Metric | Kind | Passed | Average | Not measured |
|---|---|---|---|---|
| ActionCorrectness | deterministic | 50/51 (98%) | 0.98 | 0 |
| CitationValidity | deterministic | 51/51 (100%) | 1.0 | 0 |
| AllowedActionRespected | deterministic | 51/51 (100%) | 1.0 | 0 |
| BaselineDistance | deterministic | 51/51 (100%) | 1.0 | 0 |
| NoOverclaim | deterministic | 51/51 (100%) | 1.0 | 0 |
| LengthLimits | deterministic | 51/51 (100%) | 1.0 | 0 |
| Tool Correctness | agentic | 51/51 (100%) | 1.0 | 0 |
| Faithfulness | judged | 49/50 (98%) | 0.967 | 0 |
| Economic reasoning [GEval] | judged | 39/49 (80%) | 0.831 | 1 |
| Decision explanation [DAG] | judged | 44/50 (88%) | 0.932 | 0 |
| Seller note quality [GEval] | judged | 18/20 (90%) | 0.895 | 0 |
| PII Leakage | judged | 8/8 (100%) | 1.0 | 0 |
| Step Efficiency | agentic | 8/8 (100%) | 1.0 | 0 |
| Task Completion | agentic | 8/8 (100%) | 0.988 | 0 |

| Reason | Goldens | Action correct | Failed metric results |
|---|---|---|---|
| Not received | 20 | 20/20 (100%) | 4 |
| Not as described | 17 | 16/17 (94%) | 8 |
| Unauthorised | 14 | 14/14 (100%) | 8 |

Confusion matrix (rows: expected, columns: recommended)

| expected \ got | FIGHT | SETTLE | SETTLE_PARTIAL | REPLACEMENT | GATHER_MORE | NEEDS_HUMAN |
|---|---|---|---|---|---|---|
| FIGHT | 20 | 0 | 0 | 0 | 0 | 0 |
| SETTLE | 0 | 18 | 0 | 0 | 1 | 0 |
| GATHER_MORE | 0 | 2 | 0 | 0 | 9 | 1 |

## Worst cases

- **grid-unauth-strong-above-inquiry** (expected FIGHT, got FIGHT)
  - Faithfulness (0.625): 
  - Seller note quality [GEval] (0.6): The output maintains a professional and calm tone, references specific tracking and invoice evidence without inventing facts, and correctly avoids including sensitive personal data like the buyer email or full address. However, it minorly overstates seller protection coverage by mentioning item not received eligibility when the dispute is actually an unauthorized transaction inquiry, and it includ
- **ts-defect** (expected GATHER_MORE/SETTLE, got NEEDS_HUMAN)
  - ActionCorrectness (0.0): got NEEDS_HUMAN, expected GATHER_MORE/SETTLE; problems: ['moving from GATHER_MORE to SETTLE must cite a buyer_message or listing item that justifies it']
- **grid-unauth-strong-below-chargeback** (expected FIGHT, got FIGHT)
  - Economic reasoning [GEval] (0.3): The output completely fails to compare the disputed amount of 15.00 USD with the merchant's settle threshold of 25 USD, violating the first evaluation step. Furthermore, it incorrectly states that fighting costs about $18.50, which is an external number not present in the retrieval context, violating the penalty for hallucinations.
- **adv-address-mismatch** (expected GATHER_MORE, got GATHER_MORE)
  - Decision explanation [DAG] (0.6): The score is 0.6 because while the Level 0 and Level 1 nodes confirmed the explanation states the recommended action and specific evidence, and the Level 2 node identified the action as GATHER_MORE, the Level 3 node failed because the explanation does not concretely state what the merchant themselves should collect or check next, resulting in a False verdict at Level 4.
- **grid-unauth-weak-below-chargeback** (expected SETTLE, got SETTLE)
  - Decision explanation [DAG] (0.4): The score is 0.4 because the DAG traversal shows that while the BinaryJudgementNode at Level 0 correctly verified the recommended action as 'SETTLE', the subsequent BinaryJudgementNode at Level 1 failed because the explanation did not name a specific piece of deciding evidence, resulting in a final VerdictNode of False.

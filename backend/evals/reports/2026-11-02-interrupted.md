# Eval run 2026-11-02 (interrupted during judging; deterministic re-score)

Deterministic checks only (no judge). Model under test: groq:openai/gpt-oss-120b

| Metric | Passed |
|---|---|
| Action | 37/48 |
| Citations | 48/48 |
| No false claims | 48/48 |
| PayPal request | 47/48 |

| Golden | Expected | Got | Failed checks |
|---|---|---|---|
| adv-address-mismatch | GATHER_MORE | NEEDS_HUMAN | Action: got NEEDS_HUMAN, expected GATHER_MORE; problems: ['GATHER_MORE must name the missing items'] |
| adv-cancelled-tracker | SETTLE | SETTLE | - |
| adv-listing-contradicts | FIGHT/GATHER_MORE | FIGHT | PayPal request: resolution gave up (no valid seller note / request) |
| adv-no-order-match | GATHER_MORE | GATHER_MORE | - |
| adv-paypal-fetch-failed | FIGHT | FIGHT | - |
| adv-prior-disputes | FIGHT | FIGHT | - |
| adv-prompt-injection | FIGHT | FIGHT | - |
| demo-b-phone-case | SETTLE | (needs a fresh run) | - |
| grid-inr-partial-above-chargeback | GATHER_MORE | GATHER_MORE | - |
| grid-inr-partial-above-inquiry | GATHER_MORE | GATHER_MORE | - |
| grid-inr-partial-below-chargeback | SETTLE | SETTLE | - |
| grid-inr-partial-below-inquiry | SETTLE | SETTLE | - |
| grid-inr-strong-above-chargeback | FIGHT | FIGHT | - |
| grid-inr-strong-above-inquiry | FIGHT | FIGHT | - |
| grid-inr-strong-below-chargeback | FIGHT | FIGHT | - |
| grid-inr-strong-below-inquiry | FIGHT | FIGHT | - |
| grid-inr-weak-above-chargeback | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-inr-weak-above-inquiry | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-inr-weak-below-chargeback | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-inr-weak-below-inquiry | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-snad-partial-above-chargeback | GATHER_MORE/SETTLE | SETTLE | - |
| grid-snad-partial-above-inquiry | GATHER_MORE/SETTLE | SETTLE | - |
| grid-snad-partial-below-chargeback | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-snad-partial-below-inquiry | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-snad-strong-above-chargeback | FIGHT | FIGHT | - |
| grid-snad-strong-above-inquiry | FIGHT | FIGHT | - |
| grid-snad-strong-below-chargeback | FIGHT | FIGHT | - |
| grid-snad-strong-below-inquiry | FIGHT | FIGHT | - |
| grid-snad-weak-above-chargeback | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-snad-weak-above-inquiry | SETTLE | SETTLE | - |
| grid-snad-weak-below-chargeback | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-snad-weak-below-inquiry | SETTLE | GATHER_MORE | Action: got GATHER_MORE, expected SETTLE |
| grid-unauth-partial-above-chargeback | GATHER_MORE | GATHER_MORE | - |
| grid-unauth-partial-above-inquiry | GATHER_MORE | GATHER_MORE | - |
| grid-unauth-partial-below-chargeback | SETTLE | SETTLE | - |
| grid-unauth-partial-below-inquiry | SETTLE | SETTLE | - |
| grid-unauth-strong-above-chargeback | FIGHT | FIGHT | - |
| grid-unauth-strong-above-inquiry | FIGHT | FIGHT | - |
| grid-unauth-strong-below-chargeback | FIGHT | FIGHT | - |
| grid-unauth-strong-below-inquiry | FIGHT | FIGHT | - |
| grid-unauth-weak-above-chargeback | SETTLE | SETTLE | - |
| grid-unauth-weak-above-inquiry | SETTLE | SETTLE | - |
| grid-unauth-weak-below-chargeback | SETTLE | SETTLE | - |
| grid-unauth-weak-below-inquiry | SETTLE | SETTLE | - |
| rec-mjz-inr-inquiry | GATHER_MORE/SETTLE | (needs a fresh run) | - |
| rec-yxs-snad-inquiry | GATHER_MORE/SETTLE | (needs a fresh run) | - |
| rec-zwl-chargeback | FIGHT | FIGHT | - |
| rec-zwl-under-review | FIGHT | FIGHT | - |
| ts-defect | GATHER_MORE/SETTLE | FIGHT | Action: got FIGHT, expected GATHER_MORE/SETTLE |
| ts-fixed | FIGHT | FIGHT | - |
| ts-safety | GATHER_MORE | GATHER_MORE | - |

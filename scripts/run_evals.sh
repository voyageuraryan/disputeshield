#!/usr/bin/env bash
# DisputeShield eval suite: the REAL LLM pipeline (Groq = model under test) scored by deterministic metrics and a
# Gemini judge. Uses real API quota (cached under backend/evals/.cache/). Never sends anything to PayPal.
#
#   scripts/run_evals.sh                 all goldens (cached outputs are reused)
#   scripts/run_evals.sh --refresh       call the model under test and the judge again
#   scripts/run_evals.sh --only grid-inr,adv-prompt-injection    some goldens (ids or id prefixes)
#   scripts/run_evals.sh --no-judge      deterministic metrics only (no Gemini calls)
#   scripts/run_evals.sh --only grid- --merge   chunked runs: update the latest report instead of replacing it
#   DEEPEVAL_TRACING=1 scripts/run_evals.sh   also trace the pipeline spans (@observe), kept local
#
# Report: backend/evals/reports/latest.md (+ latest.json). Unit tests stay `cd backend && pytest -q` (offline).
set -euo pipefail
cd "$(dirname "$0")/../backend"
export DEEPEVAL_TELEMETRY_OPT_OUT=1   # no usage telemetry from deepeval
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
# NB: "-c" is deepeval's own --use-cache flag, so the pytest config goes in the long form. Our caches (evals/.cache)
# already make re-runs free; deepeval's metric cache would report stale results for changed metrics.
exec .venv/bin/deepeval test run evals/ --config-file=evals/pytest.ini "$@"

# Convenience targets (run from the repo root).
.PHONY: test evals evals-refresh e2e

test:            ## backend + frontend unit tests (offline, fake LLM)
	cd backend && .venv/bin/python -m pytest -q
	cd frontend && npm test

evals:           ## real-LLM eval suite (uses API quota; cached)
	scripts/run_evals.sh

evals-refresh:   ## eval suite, ignoring the cache
	scripts/run_evals.sh --refresh

e2e:             ## Playwright UI suite (own throwaway stack)
	.venv-e2e/bin/pytest -q e2e

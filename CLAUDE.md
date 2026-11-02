# DisputeShield

AI agent that investigates PayPal disputes for small merchants, decides
whether to FIGHT, SETTLE or GATHER MORE evidence, drafts the response, and
executes it through the PayPal Disputes API only after merchant approval.
Built for the PayPal AI Hackathon (deadline 12 Nov 2026, 12:00 PM PST).

## Stack
- backend/: Python 3.11, FastAPI, httpx, SQLModel (SQLite), LangGraph, Pydantic v2
- LLM: Groq (primary) -> Gemini (fallback), over httpx, structured JSON-schema output (app/agents/llm.py)
- frontend/: React + Vite + TypeScript + Tailwind, AG Grid Community
- Hosting: Render (one web service for API, one static site for frontend)

## Commands
- Everything at once (tunnel + backend + frontend, auto-rewires the tunnel URL): ./dev.sh  (stop: ./dev.sh stop)
- Backend dev: cd backend && uvicorn app.main:app --reload --port 8000
- Backend tests: cd backend && pytest -q
- Frontend dev: cd frontend && npm run dev
- Seed demo data: cd backend && python -m app.scripts.seed
- Frontend unit tests: cd frontend && npm test
- E2E UI tests: see e2e/README.md (or `make e2e`)
- LLM evals (REAL models, uses API quota, cached): `make evals` = scripts/run_evals.sh [--refresh] [--only ids]
  [--no-judge] [--deep ids|all]; gates in backend/evals/gates.py; report in backend/evals/reports/latest.md
  (served at GET /api/admin/evals/latest). Never part of `pytest -q` (which stays offline). Limitations:
  backend/evals/README.md. Re-score cached outputs offline: cd backend && python -m evals.offline_report
- CI: .github/workflows/ci.yml (pytest + frontend on push), evals.yml (evals on dispatch / nightly)
  Optional tracing: DEEPEVAL_TRACING=1 (spans via app/tracing.py; local unless a Confident AI key is set).

## Hard rules
1. PayPal: sandbox only (api-m.sandbox.paypal.com). Never hardcode secrets;
   read from env via app/config.py.
2. Before writing or changing any PayPal call, check the official docs or
   OpenAPI spec (docs.paypal.ai, developer.paypal.com/api/customer-disputes/v1).
   Do not guess endpoint paths, enum values or payload fields.
3. The official PayPal server SDK does not cover Disputes. Use the shared
   httpx client in app/paypal/client.py for every PayPal call.
4. The LLM never invents evidence. Agents may only cite items that exist in
   the case file, by evidence_item id. Rules (app/rules/) decide which
   evidence a dispute reason requires; the LLM reads text and explains.
5. No money-moving PayPal call (provide-evidence, accept-claim, make-offer)
   runs without an approved Decision row. Enforce this in code, not just UI.
6. Every agent step is logged to the agent_steps table (powers the timeline).
7. LLM outputs use structured output parsed into Pydantic models. On parse
   failure, retry once, then mark the case NEEDS_HUMAN.
8. Keep changes small; write or update tests with each change; run pytest
   before saying a task is done.

## Layout
backend/app/{main.py, config.py, db.py, models.py, schemas.py, catalog.py, ingest.py, merchant.py, auth.py, events_bus.py}
backend/app/paypal/   client.py, disputes.py, orders.py, payments.py, tracking.py, webhooks.py, identity.py
backend/app/agents/   graph.py, investigator.py, analyst.py, resolution.py, llm.py, runner.py, steps.py,
                      troubleshooter.py (buyer troubleshooting agent), web_research.py (grounded search), redact.py
backend/app/rules/    evidence_rules.py
backend/app/troubleshooting/  buyer troubleshooting package: eligibility.py, access.py (link tokens, expiry,
                              rate limit), service.py (invite), proofs.py (upload checks: type/size/EXIF/GPS/dHash),
                              vision.py (advisory photo check), outcome.py (finished session -> evidence record +
                              flags, idempotent re-analysis, evidence-PDF page)
backend/app/api/      disputes.py, decisions.py, store.py, webhooks.py, admin.py, events.py (SSE), settings.py, auth.py,
                      evals.py (latest eval report for the Trust page),
                      troubleshooting.py (merchant invite + public buyer /api/help routes)
backend/app/scripts/  seed.py, poll_disputes.py, check_paypal.py, register_webhook.py, cleanup_orders.py
backend/evals/        DeepEval harness: goldens/ (51 versioned JSON + build_goldens.py), pipeline.py (golden -> DB ->
                      real analyst + resolution, rate limits, cache, traces, shadow investigation), judge.py
                      (Gemini judge), fake_paypal.py, checks.py, metrics/ (deterministic, judged, agentic),
                      gates.py, report.py, reports/; run with scripts/run_evals.sh (its own pytest.ini)
backend/app/tracing.py  @observe shim (no-op unless DEEPEVAL_TRACING=1)
e2e/                  Playwright UI suite (starts its own throwaway stack); frontend: npm test (Vitest)
frontend/src/pages/   Queue.tsx, CaseDetail.tsx, Store.tsx, Orders.tsx, Settings.tsx, Help.tsx (buyer troubleshooting page)
frontend/src/help/    StepCard.tsx (step card + upload), logic.ts (pure step/upload/SSE logic); the buyer's page is
                      pages/Help.tsx at /help/:sessionId (public, link token); the merchant's panel is
                      TroubleshootingCard in pages/CaseDetail.tsx

## Progress
See PROGRESS.md. Update it at the end of every session.
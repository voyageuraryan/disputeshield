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
- Backend dev: cd backend && uvicorn app.main:app --reload --port 8000
- Backend tests: cd backend && pytest -q
- Frontend dev: cd frontend && npm run dev
- Seed demo data: cd backend && python -m app.scripts.seed

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
backend/app/{main.py, config.py, db.py, models.py, schemas.py, catalog.py, ingest.py, merchant.py}
backend/app/paypal/   client.py, disputes.py, orders.py, payments.py, tracking.py, webhooks.py
backend/app/agents/   graph.py, investigator.py, analyst.py, resolution.py, llm.py, runner.py, steps.py
backend/app/rules/    evidence_rules.py
backend/app/api/      disputes.py, decisions.py, store.py, webhooks.py, admin.py, events.py (SSE), settings.py
backend/app/scripts/  seed.py, poll_disputes.py, check_paypal.py, register_webhook.py
frontend/src/pages/   Queue.tsx, CaseDetail.tsx, Store.tsx, Settings.tsx, Analytics.tsx

## Progress
See PROGRESS.md. Update it at the end of every session.
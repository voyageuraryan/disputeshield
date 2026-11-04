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
- Analytics demo history (labelled is_demo rows only): cd backend && python -m app.scripts.seed --demo-history
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
5. No money-moving PayPal call (provide-evidence, accept-claim, make-offer, escalate)
   runs without an approved Decision row. Enforce this in code, not just UI.
6. Every agent step is logged to the agent_steps table (powers the timeline). Runs end with run_finished /
   run_failed / run_skipped; one run per dispute at a time (re-analyse 409s while one is going).
7. LLM outputs use structured output parsed into Pydantic models. On parse
   failure, retry once, then mark the case NEEDS_HUMAN.
8. Keep changes small; write or update tests with each change; run pytest
   before saying a task is done.

## Layout
backend/app/{main.py, config.py, db.py, models.py, schemas.py, catalog.py, ingest.py, merchant.py, auth.py, events_bus.py,
             signals.py (Phase 15: hashed checkout signals, CE 3.0-LIKE prior history, serial disputes),
             trust.py (Phase 16: guardrails -> code refs, counters), demo_scenarios.py (DEMO_MODE + ADMIN_TOKEN only),
             alerts.py (Phase 17: email/Slack, deduped in processed_events), mcp_server.py (Phase 17: MCP at /mcp),
             merchant_uploads.py (Phase 12: merchant-added evidence: validate, store, Gemini read, confirm, flags),
             ratelimit.py (per-IP limits for public routes; loopback exempt)}
backend/app/paypal/   client.py, disputes.py, orders.py, payments.py, tracking.py, webhooks.py, identity.py
backend/app/analytics/ outcomes.py (capture on RESOLVED), kpis.py, query.py (Ask: whitelisted structured query, no SQL),
                      calibration.py (rule_score -> P(win), >= 10 outcomes), demo.py (seed --demo-history)
backend/app/agents/   graph.py, investigator.py, analyst.py, resolution.py, llm.py, runner.py, steps.py,
                      troubleshooter.py (buyer troubleshooting agent), web_research.py (grounded search), redact.py
backend/app/rules/    evidence_rules.py, coach.py ("What would win this?": hypothetical scores, pure),
                      money.py (Phase 13 calculator: fight / refund / partial offer expected net, pure)
backend/app/troubleshooting/  buyer troubleshooting package: eligibility.py, access.py (link tokens, expiry,
                              rate limit), service.py (invite), proofs.py (upload checks: type/size/EXIF/GPS/dHash),
                              vision.py (advisory photo check), outcome.py (finished session -> evidence record +
                              flags, idempotent re-analysis, evidence-PDF page)
backend/app/api/      disputes.py, decisions.py, store.py, webhooks.py, admin.py, events.py (SSE), settings.py, auth.py,
                      evals.py (latest eval report for the Trust page), uploads.py (merchant evidence uploads),
                      troubleshooting.py (merchant invite + public buyer /api/help routes)
backend/app/scripts/  seed.py, poll_disputes.py, check_paypal.py, register_webhook.py, cleanup_orders.py
backend/evals/        DeepEval harness: goldens/ (51 versioned JSON + build_goldens.py), pipeline.py (golden -> DB ->
                      real analyst + resolution, rate limits, cache, traces, shadow investigation), judge.py
                      (Gemini judge), fake_paypal.py, checks.py, metrics/ (deterministic, judged, agentic),
                      gates.py, report.py, reports/; run with scripts/run_evals.sh (its own pytest.ini);
                      10C: goldens/troubleshooting.json + conversations.py, troubleshooting_sim.py (simulated
                      buyers vs the real agent), metrics/conversational.py, checks_conv.py, record_research.py
backend/app/tracing.py  @observe shim (no-op unless DEEPEVAL_TRACING=1)
e2e/                  Playwright UI suite (starts its own throwaway stack); frontend: npm test (Vitest)
frontend/src/pages/   Queue.tsx, CaseDetail.tsx, Store.tsx, Orders.tsx, Settings.tsx, Help.tsx (buyer troubleshooting page),
                      Analytics.tsx (AG Grid + AG Charts Community; helpers in src/analytics/logic.ts),
                      Trust.tsx (eval report + guardrails + counters), Demo.tsx (public /demo; text = docs/DEVPOST.md),
                      Deadlines.tsx (Phase 18: Bryntum Scheduler TRIAL; mapping in src/deadlines/mapping.ts)
frontend/src/demo/    Tour.tsx + tour.ts (5-step guided tour), Scenarios.tsx (demo-mode drawer), content.ts
frontend/src/offers/    logic.ts (Phase 13: offer math mirroring rules/money.py; Options card is in CaseDetail)
frontend/src/evidence/  AddEvidence.tsx (add/confirm evidence, coach panel), logic.ts (pure)
frontend/src/help/    StepCard.tsx (step card + upload), logic.ts (pure step/upload/SSE logic); the buyer's page is
                      pages/Help.tsx at /help/:sessionId (public, link token); the merchant's panel is
                      TroubleshootingCard in pages/CaseDetail.tsx

## Evidence freshness (Phase 11)
Evidence rows are run-scoped (last_seen_run, active): the case file holds only items the latest run saw; a
source that failed transiently keeps its items flagged `stale:<iso>`, and FIGHT is refused when required
evidence is only stale. Manual re-analyse runs fresh=True (always re-fetches from PayPal).
Merchant uploads (Phase 12) become evidence only after the merchant confirms the fields; Gemini's read is a
suggestion. Summary and flags are built deterministically from the confirmed fields.

## Progress
See PROGRESS.md. Update it at the end of every session.
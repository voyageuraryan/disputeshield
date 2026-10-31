# Progress

## 2026-10-31 — Session 1: scaffold + PayPal client
Done
- backend/: FastAPI app (`/health`), `config.py` (pydantic-settings, reads backend/.env,
  rejects any PAYPAL_BASE_URL other than sandbox), 7 SQLModel tables in `models.py`,
  `db.py` creates them on startup, core schemas in `schemas.py`
  (`Recommendation.check_citations` enforces rule 4).
- `app/paypal/client.py`: shared async httpx client. OAuth token cached, refreshed 60s
  before expiry; 429/5xx/transport errors retried twice (0.5s, 1s backoff); 401 → drop
  token and retry once; logs `Paypal-Debug-Id` header (falls back to body `debug_id`).
- `app/paypal/disputes.py`: `list_disputes()`, `get_dispute(id)`, checked against
  the official OpenAPI spec (GET /v1/customer/disputes, GET /v1/customer/disputes/{id}).
- `app/scripts/check_paypal.py`: prints sandbox disputes.
- Tests (respx): 17 passing.
- frontend/: Vite + React + TS + Tailwind, layout + router (Queue, CaseDetail, Store).
  (Built and verified in session 2.)


## 2026-10-31 — Session 2: idempotency + demo store (phase 2)
Idempotency decision
- The Disputes v1 spec documents NO `PayPal-Request-Id` header on any endpoint
  (Orders v2 create/capture do). So `client.py` now retries 5xx/transport errors only
  for GET, the token call, or a POST sent with `request_id=` (PayPal-Request-Id).
  429 is always retried (request was rejected, not processed). Money-moving dispute
  POSTs are never re-sent after an ambiguous failure; reconcile with `get_dispute()`.
Store
- `app/catalog.py`: 4 products (lamp $89, phone case $12, ebook $9, mug $45); prices server-side only.
- `app/paypal/orders.py`: `create_order()` / `capture_order()` (Orders v2, checked against spec).
  Internal order id in `purchase_units[0].custom_id`; `invoice_id` = `DS-{id}-{nonce}`
  (nonce because PayPal rejects duplicate invoice ids across DB resets). Both calls send
  PayPal-Request-Id; capture uses `Prefer: return=representation`.
- `app/paypal/tracking.py`: `add_tracking()` via `POST /v2/checkout/orders/{id}/track`.
- `app/api/store.py`: `/api/store/{config,products,orders}`, `POST orders/{paypal_id}/capture`
  (idempotent, verifies custom_id), `POST orders/{id}/shipments` (adds PayPal tracker).
- Orders table: added `invoice_id`; `buyer_email` now nullable (known only after capture).
- Frontend Store page: JS SDK **v6** (`web-sdk/v6/core`, `createInstance({clientId})`,
  `createPayPalOneTimePaymentSession`, `<paypal-button>`); v5 `paypal.Buttons` is legacy.
- `app/scripts/seed.py`: 15 store-side orders + shipments + messages, demo cases A/B/C
  (buyer emails case-a/b/c@demo.test). No PayPal data faked. `--reset` to re-seed.
- Tests: 49 passing. Frontend `npm run build` passes.

Not yet verified against real sandbox (no credentials on dev machine)
- `check_paypal`, real checkout on the Store page, tracker call.

Next
- Put sandbox creds in backend/.env, run `python -m app.scripts.check_paypal`, buy something
  on the Store page with a sandbox personal account, add tracking, open a dispute.
- Add AG Grid; poller/webhooks to ingest disputes and link them via invoice_number/custom.
- Evidence rules, agents.

## 2026-10-31 — Session 3: ingestion (phase 3)
- `POST /webhooks/paypal` (`app/api/webhooks.py`): every event is verified via
  `POST /v1/notifications/verify-webhook-signature` with PAYPAL_WEBHOOK_ID; the raw body is
  spliced in byte-for-byte (re-serialising can break verification). Responses: bad/missing
  signature headers 400, FAILURE 401, not configured 503, PayPal/fetch errors 502 (PayPal
  re-delivers non-2xx), non-dispute events 200 "ignored", redelivered event 200 "duplicate".
- Handles CUSTOMER.DISPUTE.CREATED / UPDATED / RESOLVED -> `app/ingest.py`: get_dispute, upsert
  `disputes`, match order by capture id (seller_transaction_id) > invoice_number > custom
  (only orders that went through PayPal checkout, so seeded rows never match).
- New 8th table `processed_events` (idempotency ledger): `evt:<event id>` and
  `run:<dispute id>:<update_time>`. A webhook and the poller seeing the same dispute version
  queue ONE agent run. Event key is only written after a successful ingest, so a failed
  attempt can be redelivered.
- Agent run is a stub (`app/agents/runner.py`) that logs run_started / run_pending /
  run_skipped (resolved) to agent_steps. Real pipeline = phase 4.
- `app/scripts/poll_disputes.py` + startup loop (every POLL_INTERVAL_S=60, only if
  POLL_DISPUTES and creds set): lists disputes (page_size 50, paginates), ingests new or
  changed (`update_time` newer) ones, runs agents.
- `WEBHOOK_RECORD_DIR` (dev): saves raw webhook body + PAYPAL-* headers so a real event can
  replace the sample fixture (see backend/tests/fixtures/README.md). The current fixture is a
  documented-shape SAMPLE, not a real recording: PayPal publishes no dispute event sample.
- `render.yaml`: API web service (plan starter + 1GB disk for SQLite, 1 worker) + static site
  (SPA rewrite, VITE_API_BASE_URL baked at build). Frontend reads VITE_API_BASE_URL.
- Client: `retry_safe=True` for read-only POSTs; `PayPalNotConfiguredError`.
- Tests: 74 passing. Frontend build passes.

Next
- With creds: register webhook URL in the sandbox app, set PAYPAL_WEBHOOK_ID, record a real
  CUSTOMER.DISPUTE.CREATED and swap it into tests/fixtures.
- Phase 4: evidence rules + agent graph replacing the runner stub.


## 2026-10-31 — Session 4: live sandbox checks
- Real sandbox creds in backend/.env: token, list disputes (none yet), create order, read-back
  (invoice_id/custom_id stored), premature capture -> ORDER_NOT_APPROVED (502 to client, no retry).
- Headless Chromium: v6 SDK renders 4 buttons; click creates order and opens sandbox checkout.
- "Things don't appear to be working at the moment" at checkout: real cause was both sandbox
  accounts being INDIA (PayPal blocks IN->IN payments). A US personal buyer paying the IN
  business account works. Also removed payment_source.experience_context from create-order
  to match PayPal's v6 example (orders now CREATED instead of PAYER_ACTION_REQUIRED); not the
  root cause, but kept. Side effect: ebook checkout no longer forces NO_SHIPPING.
- VERIFIED LIVE: order 31 ($89 lamp) paid by US sandbox buyer sb-hizpg53637037@personal.example.com
  -> capture 4S541016HD390220V COMPLETED, custom_id 31, invoice DS-31-19c56b70, seller_protection
  ELIGIBLE; saved capture_id/buyer_email/shipping_address. Tracking USPS 9400111899562837465012
  added via /v2/checkout/orders/{id}/track; PayPal shows tracker SHIPPED.
- Phases 1 and 2 complete. Browser extension: Brave is a Flatpak, can't connect native host;
  use a native Chrome/Brave + `claude --chrome` if browser automation is wanted.
Next
- Phase 3 live check: open a dispute on order 31 from the US buyer (sandbox.paypal.com > Activity >
  Report a problem), run poll_disputes; then public URL + PAYPAL_WEBHOOK_ID for webhooks.

## 2026-10-31 — Session 5: phase 3 live
- Webhook 95P52181NY896745H registered via new `app/scripts/register_webhook.py` (list / create /
  re-point existing URL; `--write-env`) to a cloudflared quick tunnel. Quick-tunnel URLs change on
  restart: rerun register_webhook with the new URL.
- Live: unsigned webhook -> 400; forged-but-plausible headers -> PayPal verify said FAILURE -> 401.
- Real sandbox case PP-R-ZWL-10195422 (item not received, $89, INQUIRY, UNDER_REVIEW) on order 31.
  GET /v1/customer/disputes/{id} works, but the LIST endpoint returned nothing for it under every
  filter (sandbox search lag), so the poller could not see it. Added `poll_disputes --id <case>`
  (same ingest path). Ingested: linked to order 31 via capture id, 1 agent run; re-ingest = no new run.
- Sandbox list caught up after ~165s. PayPal escalated the case INQUIRY -> CHARGEBACK (11:06:43).
- REAL WEBHOOKS received through the tunnel from PayPal (173.0.80.x), both verified (SUCCESS), 200:
  CUSTOMER.DISPUTE.UPDATED (arrived first) -> new version -> agent run #2;
  CUSTOMER.DISPUTE.CREATED (arrived 16s later) -> recorded, no extra run (same version).
  Ledger: 2 run keys + 2 evt keys; agent_steps: exactly 2 runs. Poller then ingests 0 (up to date).
- Real recordings copied byte-for-byte to tests/fixtures/real_dispute_{created,updated}.json
  (+ headers) and real_get_dispute.json; new tests/test_webhooks_real.py (out-of-order delivery,
  redelivery, rejection). Findings: event resource is an older snapshot than GET (we re-fetch);
  PayPal adds a `paypal-auth-version: v2` header.
- Fix: disputes.raw now stores PayPal's original JSON (was our re-serialised copy).
- Tests: 83 passing. PHASE 3 COMPLETE (live).
Open / later
- Poller blind spot while sandbox list lags (webhook + `--id` cover it).
- Quick-tunnel URL is temporary; for a stable webhook deploy to Render and re-run register_webhook.

## 2026-10-31 — Session 6: phase 4 (graph + investigator)
- `app/agents/graph.py`: LangGraph StateGraph investigate -> analyze -> resolve -> approve
  (interrupt) -> execute, compiled with AsyncSqliteSaver (CHECKPOINT_DB, default ./checkpoints.db;
  /var/data on Render). thread_id = run key. Deps bound at build time (nothing unserialisable in
  state/config). analyze/resolve pass through; execute makes NO PayPal call (rule 5 not built yet).
- `app/agents/runner.py`: run_agents (pauses at approval, logs "Waiting for merchant approval";
  failures logged as run_failed), resume_run(run_key, approval) via Command(resume=...).
- `app/agents/investigator.py` (no LLM): PayPal dispute refresh, capture (new
  `paypal/payments.py` GET /v2/payments/captures/{id}), trackers from GET /v2/checkout/orders/{id}
  (Tracking v1 GET returns 403 NOT_AUTHORIZED for this app); store order, shipment + delivery
  address check, messages, listing, buyer's EARLIER orders + their disputes. Evidence upserted by
  source_ref (re-runs don't duplicate); one human-readable agent_steps row per lookup; PayPal
  failures logged and investigation continues.
- `GET /api/disputes/{paypal_dispute_id}/case` -> CaseFile (under /api like the store routes so the
  Vite proxy covers it).
- LIVE on PP-R-ZWL-10195422: 6 evidence items (capture PENDING/BUYER_COMPLAINT + seller protection
  ELIGIBLE, PayPal USPS tracker, order, store shipment, listing, buyer history); run paused at
  approval, checkpoint in backend/checkpoints.db (thread run:PP-R-ZWL-10195422:2026-10-31T11:06:43.889Z).
- New fixtures: real_capture.json, real_order.json. Tests: 94 passing.
Next (phase 5): rules/evidence_rules.py (checklist + rule_score), analyst (LLM explains, cites ids),
resolution draft + PROPOSED Decision, approval API that resumes the paused run.

- Follow-ups checked: (1) Tracking v1 403 = app token has no shipping/tracker scope; PayPal docs call
  Tracking v1 the legacy path for non-Orders-v2 integrations, so reading trackers from the order is
  the supported route (Orders v2 tracker has no carrier field -> carrier from store shipment).
  (2) No due date is correct: case is UNDER_REVIEW / UNDER_PAYPAL_REVIEW, PayPal returns no *_due_date
  and allowed_response_options = accept_claim only. (3) Case endpoint now also at /disputes/{id}/case.
- CaseFile now carries status, stage, dispute_state, allowed_actions (+ computed can_fight /
  can_settle). allowed_actions comes from the dispute's HATEOAS links + allowed_response_options
  (`paypal/disputes.py: allowed_actions`); sandbox-only adjudicate/require_evidence excluded.
  FIGHT = provide_evidence | provide_supporting_info | appeal; SETTLE = accept_claim | make_offer.
  Enforced in code: Recommendation.validate_against(case) = check_citations + check_allowed
  (FIGHT/SETTLE rejected when PayPal doesn't allow them; GATHER_MORE always OK). Investigator logs
  "PayPal currently allows: ...". Correction: the live case CAN be fought via provide_supporting_info.
  Live case now (11:24:31 version): provide_supporting_info + accept_claim. Tests: 106 passing.

## 2026-10-31 — Session 7: phase 5 (rules + analyst)
- `app/rules/evidence_rules.py`: exact PayPal reasons MERCHANDISE_OR_SERVICE_NOT_RECEIVED,
  MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED, UNAUTHORISED (British spelling; "UNAUTHORIZED" is unsupported).
  Required/supporting evidence per reason; red flags from structured evidence flags only
  (address_mismatch -0.4, tracker_cancelled -0.2, not_protected -0.1 INR / -0.3 UNAUTHORISED).
  rule_score = 0.7*required + 0.3*supporting - penalties, exact Decimal, ROUND_HALF_UP.
  economic_baseline: >=0.7 FIGHT; <0.4 SETTLE; amount < SETTLE_THRESHOLD and <0.7 SETTLE; else
  GATHER_MORE; downgraded to GATHER_MORE if PayPal doesn't allow FIGHT/SETTLE now.
- Investigator now writes evidence flags (address_match/mismatch, protected:<category>,
  seller_protection:<status>, tracker_cancelled, delivered, untracked, buyer_prior_disputes).
- `app/agents/llm.py`: Anthropic `beta.messages.parse(output_format=Recommendation)`, model
  claude-opus-5-5 (LLM_MODEL), effort medium, server-side refusal fallback (fallbacks="default").
  ANTHROPIC_API_KEY via config.py. Any failure -> LLMError.
- `app/agents/analyst.py`: checklist -> baseline -> LLM -> validate (ids exist, PayPal allows action,
  <=1 step from baseline and only with a cited buyer_message/listing, FIGHT cites evidence,
  GATHER_MORE names missing items, <=150 words) -> retry once with the problems -> NEEDS_HUMAN
  (disputes.agent_status). Success: decisions row PROPOSED (run_key stored), older PROPOSED for the
  dispute -> SUPERSEDED. Every step logged. Graph ends (no approval pause) when NEEDS_HUMAN.
- Schema: disputes.agent_status, evidence_items.flags, decisions.run_key, DecisionStatus.SUPERSEDED,
  ChecklistResult.red_flags/reason_supported, EvidenceItem.flags. db.py auto-adds new nullable
  columns to existing SQLite tables (applied to dev DB).
- Tests: 159 passing (rules per reason, demo A FIGHT/high, B SETTLE, C GATHER_MORE + weak FIGHT only
  with justifying citation, retry/NEEDS_HUMAN paths, LLM wrapper, migration). Tests never call the
  real API (autouse fake LLM).
- Live (rules only): PP-R-ZWL-10195422 -> rule score 0.58, missing delivery_confirmation, baseline
  GATHER_MORE. LLM step not run live yet: no Anthropic credentials on this machine.

## Open items (tracked)
1. [x] Git repo initialised (main), first commit made; .env/DBs/artifacts/recorded webhooks ignored, secret scan clean.
2. [ ] Webhook re-pointed to new quick tunnel authors-accessories-cell-personals.trycloudflare.com (old one deleted 15:31
       "Unauthorized: Tunnel not found"); needs restart + register_webhook, or Render / a named tunnel.
3. [x] Paused runs per dispute version: new proposal supersedes older PROPOSED decisions; approve/reject
       only accept PROPOSED decisions and resume that decision's run_key (superseded -> 409).
4. [ ] Poller can miss brand-new disputes while PayPal's list lags (webhook + `poll_disputes --id` cover it).
5. [ ] Seller sandbox account is India: test buyers must be US accounts.
6. [~] Queue (AG Grid) + Case detail (approve/reject/retry, evidence, timeline, PDF link) built. User must install Node.
7. [ ] Ebook checkout may ask for shipping (payment_source removed from create-order).
8. [ ] Unpaid test orders #16-30 clutter the dev DB.
9. [ ] Investigator re-fetches the dispute right after ingest (one extra GET per run).
10. [x] SETTLE_THRESHOLD now used by the economic rule.
11. [x] Analyst LLM run live (Groq gpt-oss-120b) on PP-R-ZWL-10195422 -> PROPOSED GATHER_MORE.
12. [ ] No refund_policy evidence source yet: every not-as-described case is missing it by design.
13. [x] LLM prompts redact buyer emails + known shipping/delivery addresses (LLM_REDACT_PII, default on).

## 2026-10-31 — Session 8: LLM switched to Groq -> Gemini
- `app/agents/llm.py` rewritten over httpx (Anthropic SDK removed): Groq chat completions with strict
  json_schema (default openai/gpt-oss-120b, reasoning_effort medium, include_reasoning false,
  max_completion_tokens 4096) -> on any failure Gemini generateContent with responseMimeType
  application/json + responseJsonSchema (default gemini-3.8-flash; thought parts skipped).
  Order/models via LLM_PROVIDERS, GROQ_MODEL, GEMINI_MODEL; keyless providers skipped. Schema made
  strict (all required, additionalProperties false, no titles). Timeline records which provider answered.
- Free-tier facts checked 2026-10-31: Groq free plan gpt-oss-120b / gpt-oss-20b / qwen3.8-27b each
  30 RPM, 1K RPD, 8K TPM, 200K TPD (Llama 3.3 70B now Enterprise only). Gemini free tier text models:
  gemini-3.8-flash, 3.6-flash, 3.5-flash-lite, 3.1-flash-lite, 3-flash-preview, 2.5-pro, 2.5-flash,
  2.5-flash-lite (gemini-4-argon and 3.1-pro-preview are paid only). Real-case prompt ≈ 1.3K tokens.
- Tests: 171 passing (request shapes, every fallback trigger, both-fail, config).
- Keys added. Live checks: Groq gpt-oss-120b OK (6.3s). Gemini key OK on gemini-2.5-flash and
  3.5-flash-lite; gemini-3.8-flash returned 503 "high demand", so GEMINI_MODEL now takes a list
  (default "gemini-3.8-flash,gemini-2.5-flash"). Tests now ignore backend/.env (isolation fix).
- LIVE full pipeline on PP-R-ZWL-10195422: checklist 0.58 -> baseline GATHER_MORE -> Groq recommended
  GATHER_MORE/medium citing evidence #2, missing delivery_confirmation -> decision #1 PROPOSED,
  run paused for approval (thread ...#live-llm-1). Tests: 172 passing.

## 2026-10-31 — Session 9: phase 6 (resolution, approval, execution)
- Formats verified in PayPal's "Use Disputes API" guide (OpenAPI only lists file parts):
  provide-evidence = multipart, part "input" (application/json) {"evidences":[{evidence_type,
  evidence_info.tracking_info[{carrier_name, tracking_number}], notes}]} + "file1" PDF;
  provide-supporting-info = same with {"notes"}; accept-claim = JSON {"note"} (accept_claim_type is not a
  request field). Files .jpg/.jpeg/.gif/.png/.pdf, <10 MB each, <=50 MB per call. Sandbox: require-evidence
  {"action": SELLER_EVIDENCE|BUYER_EVIDENCE}, adjudicate {"adjudication_outcome": SELLER_FAVOR|BUYER_FAVOR}.
- `app/agents/resolution.py`: FIGHT -> LLM seller note (SellerNote, <=1000 chars, cited ids must exist,
  must not claim delivery/receipt without delivery_confirmation evidence; retry once -> NEEDS_HUMAN) + one-page
  ReportLab PDF (KeepInFrame shrink) -> provide-evidence (PROOF_OF_FULFILLMENT or the type PayPal requested,
  tracking_info from store shipments, carrier mapped or OTHER) when allowed, else provide-supporting-info.
  SETTLE -> accept-claim with templated note. GATHER_MORE -> nothing to send. Request stored on
  decisions.action_request. execute_decision refuses unless APPROVED; stores paypal_response; EXECUTED/FAILED.
- Rule 5 enforced twice: execute_decision AND paypal/disputes.py provide_evidence / provide_supporting_info /
  accept_claim take the Decision and raise NotApprovedError unless status == APPROVED (no HTTP call made).
- `POST /decisions/{id}/approve` {action?, note?, approved_by?}: only PROPOSED; override action / edited note
  rebuilds request (+PDF); GATHER_MORE needs an action; resumes the paused run (execute calls PayPal).
  `POST /decisions/{id}/reject`, `GET /decisions/{id}`.
- `POST /admin/sandbox/disputes/{id}/require-evidence|adjudicate` behind X-Admin-Token (ADMIN_TOKEN; empty =
  disabled, 404); re-ingests the dispute afterwards. ADMIN_TOKEN generated into backend/.env.
- Found live: Groq's first FIGHT note said "the buyer received" an item that was only SHIPPED -> added the
  no-overclaim rule (prompt + validator); re-drafted note is accurate.
- New columns decisions.action_request/approved_action/final_note (auto-migrated). Tests: 198 passing.
- Live (no PayPal mutation yet): decision #1 GATHER_MORE approve without action -> 409; admin w/o token -> 403;
  live note + 1-page PDF for PP-R-ZWL-10195422 would go to provide-supporting-info.
14. [ ] PP-R-ZWL-10195422: PayPal refuses PROOF_OF_FULFILLMENT both with and without tracking_info
        (EVIDENCE_TYPE_IS_NOT_ALLOWED, debug ids f233018eb2716, ca534153a1eca, ca534153d2b67) although it is
        listed as REQUESTED_FROM_SELLER. Likely sandbox state after require-evidence (another team's natural
        chargeback accepted the same shape). Unresolved; ask PayPal support with the debug ids. Decision #4 is
        retryable (e.g. as SETTLE) - left to the merchant.
15. [ ] accept-claim / provide-supporting-info / adjudicate not yet proven live (mocked tests only).

## 2026-10-31 — Session 10: hardening + merchant UI
- Real-world fixes: atomic PROPOSED->APPROVED/REJECTED (double click can't double-submit); approval re-reads
  the dispute from PayPal first (resolved / action no longer allowed / PayPal down -> 409/502, nothing sent;
  evidence type taken from PayPal's current request); RESOLVED disputes retire PROPOSED decisions; lost
  checkpoint -> approved decision executes directly (still APPROVED-guarded); EVIDENCE_TYPE_IS_NOT_ALLOWED ->
  one automatic resend with the documented alternative (same type, note + PDF, no tracking_info);
  POST /decisions/{id}/retry for FAILED decisions PayPal definitely didn't process (4xx), once per decision,
  never reusing a FIGHT note for a SETTLE; admin helper waits for PayPal's new state; GET evidence.pdf.
- Bugs found by the new tests/live runs and fixed: retry-as-SETTLE reused the FIGHT seller note; a failed
  decision could be retried twice in parallel; UI showed two retry buttons and an empty retry note.
- PII redaction (app/agents/redact.py) for analyst + seller-note prompts; notes containing placeholders rejected.
- API for UI: GET /api/disputes, GET /api/disputes/{id} (case, decisions, timeline); /api/decisions/* alias.
- Frontend: Queue (AG Grid 36, refresh 10s) and CaseDetail; verified headless against live data.
- Tests: 215 passing (new tests/test_scenarios.py).


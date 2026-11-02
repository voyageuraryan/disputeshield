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

## Open items (tracked) — updated 2026-11-01, session 14 (live testing)
Legend: [ ] open · [~] partly done / waiting on the user · [x] done. P1 = blocks a credible demo/deploy.
User decisions (session 13): login = Log in with PayPal; auto-settle stays advisory; keep India seller;
SQLite on Render disk.

### P1
G1  [x] Log in with PayPal VERIFIED LIVE 2026-11-01 (sandbox business account). PayPal refused http://localhost as
        Return URL, so AUTH_REDIRECT_URI uses the cloudflared tunnel (changes on restart: update .env + PayPal app).
        userinfo: spec path without schema gave 401; now tries the documented variants. Allowed:
        sb-secn253645168@business.example.com, sb-lbmfs53656037@business.example.com. Personal (buyer) accounts
        fail at userinfo, which is fine (buyers never use the dashboard).
G2  [~] Deploy prepared (render.yaml with all settings, AUTH_SECRET generated, DEPLOY.md). WAITING ON USER:
        push to GitHub + Render account. Webhook still on a cloudflared quick tunnel locally.
G3  [~] PROVEN LIVE 2026-11-01: accept-claim (PP-R-YXS-10195747, $45 mug, then RESOLVED via webhook) and
        provide-supporting-info (PP-R-ADP-10195745). Still unproven: provide-evidence (needs a case PayPal itself
        moves to WAITING_FOR_SELLER_RESPONSE with evidence requested; old refusals: f233018eb2716, ca534153a1eca,
        ca534153d2b67).
G4  [x] Node installed (/usr/bin/node).

### P2
G5  [x] Decided: advisory (rule 5). Settings page says nothing is refunded automatically.
G6  [x] Decisions store analysed_threshold; case page offers Re-analyse when the setting changed;
        POST /api/disputes/{id}/reanalyze (new proposal supersedes the old one).
G7  [x] App-wide new-dispute alerts: GET /api/events/disputes (SSE) -> toast on every page, queue refreshes at once.
        (No browser/OS notifications: not requested.)
G8  [x] Refund policy in Settings -> refund_policy evidence (store:settings/refund_policy) for new investigations.
        WAITING ON USER for the actual policy text.
G9  [x] VERIFIED LIVE 2026-11-01 (order #33: no address asked). Digital-only orders send payment_source.paypal.experience_context.shipping_preference=NO_SHIPPING (live: order
        created, checkout opens). WAITING ON USER: buy the ebook once with the US buyer to confirm no address asked.
G10 [x] Decided: keep India seller; Store page tells buyers to use a US sandbox account.

### P3
G11 [x] SSE wakes on commit (app/events_bus.py) with a 3 s safety-net poll; still to verify behind Render's proxy (G2).
G12 [x] Decided: SQLite + one process for the hackathon (DEPLOY.md). Postgres = future.
G13 [x] Investigator reuses a dispute fetched < 60 s ago (one GET per run instead of two).
G14 [x] Mitigated, can't fix (PayPal sandbox list lag): webhook + `poll_disputes --id`.
G15 [x] Gemini 503: one quick retry of the same model, then the next model.
G16 [x] app/scripts/cleanup_orders.py (dry run default); 17 abandoned test checkouts removed from the dev DB.
G17 [x] Legacy step codes shown in words.
G18 [x] Frontend tests in repo: Vitest unit tests (npm test, 19) + Playwright e2e suite (e2e/, 14 scenarios incl.
        the login gate) that starts its own throwaway API + Vite.
G19 [x] Pages lazy-loaded; AG Grid trimmed to the modules used. First load 178 kB; queue chunk 897 kB (was 1.2 MB in
        the main bundle).

### Found in live testing 2026-11-01 (session 14)
G20 [x] P1. Our own response made PayPal fire CUSTOMER.DISPUTE.UPDATED (new update_time) -> new agent run -> a new
        PROPOSED decision -> case back to "Awaiting your approval" right after "Response sent" (invites a duplicate
        response; it happened: a second supporting-info note was sent on PP-R-ADP-10195745 from that decision).
        Fix: run key = material state (status, stage, reason, state, amount, due date, allowed actions, message
        count), not update_time; after an EXECUTED/APPROVED decision a new analysis starts only if the seller is
        asked again (stage escalation or back to WAITING_FOR_SELLER_RESPONSE); otherwise a timeline step explains it.
        LIVE ONLY AFTER THE USER RESTARTS THE BACKEND (it runs without --reload).
G21 [x] P1 security. GET /api/store/orders (buyer emails) and POST /api/store/orders/{id}/shipments (writes to
        PayPal) were public -> require_merchant. Live after backend restart.
G22 [x] Inquiry-stage SNAD: PayPal allows only make_offer/accept_claim/escalate/send_message (no evidence). Baseline
        said "PayPal does not allow a response right now" -> now "does not accept evidence at this stage
        (settling is still possible)".
G23 [x] Inquiry-stage fight: escalate (POST /v1/customer/disputes/{id}/escalate, JSON {note} 1-2000, spec: stage
        must be INQUIRY) added to FIGHT_ACTIONS; FIGHT order: provide-evidence > supporting-info > escalate. UI says
        "fight by escalating to a PayPal claim". Analyst prompt: never suggest partial offers / messages (can't send).
        Not yet run live.
G24 [x] Executed decision card didn't show the note sent (E9) -> shows "Note sent" + settle wording.
G25 [x] Phone layout put the long timeline above facts/evidence -> timeline last on phones.
G26 [~] P3 sandbox fact (admin helper now returns a "hint" explaining it): adjudicate refused (ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE, debug f350719ea974e) on
        a CHARGEBACK-stage case UNDER_PAYPAL_REVIEW; buyer-filed INR with tracking opened directly as CHARGEBACK.
G27 [ ] P3 cosmetic: PayPal consent screen names the app "Default Application" (rename in the PayPal app settings).
G28 [x] Tracking + delivery from the dashboard: Orders page (merchant tab) and a "Shipping & tracking" card on
        each case: Add tracking (saved + uploaded to PayPal; no digital orders; no duplicates) and Record delivery
        (date between order date and today; order's address or a different one; stored locally with
        delivery_recorded_by="merchant", since PayPal has no seller "delivered" API). Both log a timeline step on
        the order's disputes and prompt Re-analyse. Tracking + delivery -> INR reaches FIGHT (tested).
G29 [x] Buyers / signed-out visitors saw the Disputes and Settings tabs on the Store (pages themselves were already
        protected). Layout now shows them only to a signed-in merchant (or AUTH_MODE=off); tests in
        frontend/src/__tests__/layout.test.tsx.

### Found in Phase 9A (2026-11-02)
G30 [ ] P2 Delivery evidence is merchant-entered (self-attested) yet counts as full delivery confirmation. User
        decision 2026-11-02: keep manual entry for now. Options when revisited: carrier/aggregator tracking feed
        (EasyPost/Shippo test trackers in sandbox), labels bought in-app, or weight manual entry lower + require a
        carrier screenshot.
G31 [ ] P2 (before 9B accepts proofs) The merchant can open the buyer's troubleshooting link: it is in the PayPal
        thread the merchant can read, in the email-stub response, and in the stored dispute.raw (seller message).
        Proof uploaded later can't be attributed strictly to the buyer. Fix idea: bind the session to the first
        device that opens it, or buyer Log in with PayPal.
G32 [ ] P3 Session status isn't closed when the dispute resolves (the buyer link already answers 410 "case
        closed"; the merchant card keeps the last status). Close sessions on ingest once 9B defines outcomes.
G33 [ ] P2 Buyer links use FRONTEND_URL (localhost:5173 in dev): only same-computer testing until the frontend is
        deployed (Render) or tunnelled; dev.sh tunnels the backend only.
G34 [ ] P3 Email invite is a dev stub (timeline entry; link shown once to the merchant to pass on).
G35 [ ] P3 Buyer rate limit is in-memory, per process (resets on restart, not shared across workers). Fine for one
        Render instance; memory is bounded (refilled buckets are pruned).
G36 [x] Refund policy template out of sync with the catalog ("handmade lamp"; "every physical order ships tracked"
        was false for the untracked phone case) -> rewritten for the electronics catalog.
G37 [x] CLAUDE.md layout listed a non-existent Analytics.tsx and missed Orders/Help/troubleshooting -> fixed.
G38 [x] e2e race (Orders card read before the list reloaded) and the auto-invite checkbox only flipping after the
        save -> test waits; checkbox updates at once and reverts on error.
G39 [x] Merchant card kept showing "Invited" after the link expired (expiry was only noticed when the buyer
        opened it) -> the status check expires it too.

### New, found in session 13 (fixed)
- AuthCallback lost the login result in development (StrictMode double effect) and ignored a new #fragment in the
  same tab -> derived from the router location.
- AG Grid trim dropped CellStyleModule (Amount alignment) and used deprecated tooltipField -> fixed; e2e test now
  fails on any AG Grid console error.
- e2e harness left orphaned servers after a failed start and then tested a stale server -> port check, cleanup on
  failure, process-group kill.
- SSE tests raced on a shared in-memory SQLite connection -> file DB for those tests.

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

## 2026-10-31 — Session 11: phase 7 (merchant UI)
- Queue (AG Grid 36, Quartz theme, indigo accent): Dispute, Reason, Amount, Buyer, Due in (live countdown,
  red < 48 h, overdue), Recommendation chip, Confidence, Status (merchant phrasing: Awaiting your approval /
  Response sent / Response failed / Resolved ...). Sortable, floating column filters, quick search, row click ->
  case, refresh 10 s, auto-height for short lists. Phones (<768 px): card list + search instead of the grid.
- CaseDetail, 3 columns at >=1024 px (stacked on phones, recommendation first): left dispute facts + order
  (items, buyer, ship-to, invoice, PayPal ids, shipments); middle evidence checklist (required/supporting,
  present/missing, score bar, red flags, rules' baseline) + evidence cards with "View source" dialog (store rows
  from DB, PayPal capture/trackers fetched live); right recommendation card (action, confidence, reasoning,
  economic note, missing items, editable note, Approve / Change action / Reject, confirm-before-send, retry for
  failed, PDF preview), earlier decisions, agent timeline (collapsible).
- API: DisputeRow.buyer_email; detail adds order + checklist; GET /api/evidence/{id}/source;
  decisions.economic_note/missing_items stored by the analyst (dev DB backfilled from agent_steps).
- Verified headless (33 checks, all pass) at 1280x800 and 390x844 on a scratch copy with 5 cases in different
  states (no horizontal scroll, no console errors, nothing sent); plus live case PP-R-ZWL-10195422 on the real app.
  Fixed from screenshots: clipped Status column, empty grid space, "1 steps", "Due -" on resolved cases.
- Dev note: restart the API with scratchpad restart_api.sh (an old uvicorn once kept port 8000 and the
  "restart" silently failed). Tests: 220 passing.

## 2026-10-31 — Session 12: phase 8 (live timeline, toasts, settings)
- GET /disputes/{id}/events (+ /api alias): SSE of agent_steps. Backlog (or after Last-Event-ID / ?after=),
  then `event: ready`, then new rows (1 s poll), `: ping` heartbeat every 15 s, `retry: 3000`, ends after 5 min
  with `event: bye` (browser reconnects without gaps), stops on client disconnect. Verified live through the Vite
  proxy (new step delivered in ~1 s).
- CaseDetail: vertical live timeline (icon per agent, one line per step, timestamps, follows new steps unless
  scrolled up), states connecting / live / reconnecting / polling fallback (after 5 stream errors), empty,
  loading skeleton; not-found and error states with retry; decisions refresh when steps arrive.
- Queue: toast for disputes that appear after the page loaded (none for existing ones), with "Open case";
  loading skeletons, empty state, error + Retry keeping stale data. Network errors shown in plain words.
- Settings page (/settings): small-dispute settle threshold, default $25, stored in new merchant_settings table
  (analyst + checklist read it); validation 0..10,000 with <=2 decimals; save/undo/error states; explains that
  nothing is refunded automatically.
- Verified: 30 headless scenario checks (live fill 0.9 s, blocked stream -> polling, toast, empty/error/not-found,
  settings validation/persist/failure, phone layouts) on a scratch copy; real app on PP-R-ZWL-10195422 shows all
  121 steps live. Tests: 230 passing.

## 2026-10-31 — Session 13: closing the gaps (G1-G19)
See the open-items list above for each item. Tests: backend 275, frontend unit 19, e2e 14 (all passing; e2e
and SSE suites run repeatedly to check for flakiness).


## 2026-11-01 — Session 14: live human-style testing (headless browser, real sandbox)
Results: A2 A5 A6 ✅ (A4 = same allowlist check as A5) · B1 B2 B3 B4 B5 ✅ (orders 32-34; ebook no address) ·
tracking on PayPal visible to buyer ✅ · C1 ✅ (PP-R-ADP-10195745, webhook, 4 s after PayPal created it) · C3 ✅
(PP-R-YXS-10195747) · D1 D3 ✅ · E1 E2 E3 E4 E13 ✅ · E5 ✅ accept-claim live -> RESOLVED · E7 ✅ supporting-info live ·
E9 ✅ after G24 · E10 ✅ (second tab 409; message hidden by G20, fixed) · H2 ❌ PayPal refuses (G26) · I1 ✅ I2 ✅ after G25.
Not run: C4 (buyer "unauthorized" flow may lock the buyer account), C5 (would stop the user's tunnel), C7, E6, H1.
Fixed: G20-G22, G24, G25. Tests: backend 283, frontend 19, e2e 15 — all pass.

## 2026-11-01 — Session 15: docs for hand testing, nav fix, new tunnel
- Tunnel now https://endorsement-innovative-boats-ratio.trycloudflare.com: AUTH_REDIRECT_URI in backend/.env
  updated, webhook 95P52181NY896745H re-pointed (register_webhook --write-env). PayPal app Return URL is the
  user's step. Backend needs a restart to load .env.
- G29 fixed (merchant tabs hidden from buyers); G28 recorded.
- docs/TEST_SCENARIOS.md rewritten (start/stop, tunnel URL places, Result column);
  new docs/END_TO_END_FLOWS.md (tracking explained, every choice, 12 hand-test flows).
- Frontend 23 Vitest tests pass.

## 2026-11-01 — Session 15b: closing G23, G28 (+ G26 hint)
- G28: POST /api/store/orders/{id}/shipments/{sid}/delivery, GET /api/store/orders returns shipments,
  digital_only, disputes; frontend Orders page + ShippingPanel (case page card). Evidence summary marks
  merchant-recorded delivery.
- G23: escalate as the inquiry-stage Fight route (spec checked: schema.json "escalate").
- G26: adjudicate refusal now returns a hint. Still a sandbox limit.
- Docs: END_TO_END_FLOWS (tracking via UI, Flow 4 escalate, new Flows 13-14), TEST_SCENARIOS (D7, D9).
- Tests: backend 297, frontend 30, e2e 18 — all pass. Backend restart needed (new column + endpoints).
- Open: G3 (provide-evidence and escalate not yet proven live), G26 (sandbox), G27 (user's PayPal app name).

## 2026-11-02 — Session 16: one-command launcher
- `./dev.sh` starts the cloudflared quick tunnel, backend and frontend. Its logs and pids go in `.run/` (gitignored).
- On each new tunnel URL it rewrites `AUTH_REDIRECT_URI` in backend/.env and re-points the PayPal webhook (`register_webhook --write-env`). It then shows a banner and a notify-send alert with the Return URL to set in the PayPal app. That is the only manual step left.
- A watchdog restarts a dead tunnel, backend or frontend. A tunnel whose public URL fails /health 3 times is replaced, and the Return URL banner is shown again.
- Commands: `status`, `url`, `logs`, `stop`. `stop` signals the running launcher so the watchdog doesn't respawn anything.
- Verified live:
  - start
  - public /health 200
  - webhook re-pointed (status lists it)
  - kill cloudflared → new URL wired up and backend restarted
  - double-start guard
  - external stop leaves nothing running

## 2026-11-02 — Phase 9A: buyer troubleshooting portal (foundations)
- Catalog:
  - `category` is now the store category (electronics / accessory / digital / home). PayPal's item category
    moved to `paypal_category`.
  - The lamp is now the "Smart LED desk lamp" (Lumina LD-200, electronics). New: Wireless earbuds $59
    (SoundPeak E5).
  - Order lines store category + model (`order_item`). Older orders fall back to the catalog by SKU.
  - Seed adds demo case D (earbuds, "left earbud won't charge").
- Tables, auto-migrated: troubleshooting_sessions (one per dispute), ts_steps, ts_messages, ts_proofs, ts_sources.
  Also new: merchant_settings.auto_invite_troubleshooting (NULL = off).
- app/troubleshooting:
  - eligibility.py: pure check. Eligible = SNAD + linked order + electronics item + not resolved.
    The investigator logs the result as "troubleshooter".
  - access.py:
    - a 32-byte token; only its SHA-256 is stored; constant-time compare
    - expiry = seller_response_due − 48 h, or invited + 5 days
    - per-session token-bucket rate limit
  - service.py: invite / resend (rotates the link) / idempotent; auto_invite never fails a run.
- PayPal send-message:
  - Checked the OpenAPI and the integration guide. They disagree: JSON {message} vs multipart; the guide's JSON
    sample is right, with multipart needed only for files.
  - Content ≤ 2000 characters; allowed at INQUIRY only.
  - **Verified live 2026-11-02** on PP-R-MJZ-10195778: 200; the message shows as posted_by SELLER.
- API:
  - GET /api/disputes/{id}/troubleshooting and POST …/invite (merchant only). The invite returns 409 {reason,
    email_available} when PayPal disallows messaging, and the email stub returns the link once.
  - GET /api/help/{sid} (public; link token via ?t= or X-Help-Token). It refuses merchant credentials and
    answers 404 for an unknown session or a wrong token, 410 for an expired link or a resolved dispute, and
    429 with Retry-After when rate-limited.
- UI:
  - Case page card "Buyer troubleshooting" (invite / email / new link, status chip)
  - Settings toggle
  - buyer page /help/:id outside the merchant layout; it moves the token from the URL into sessionStorage
  - timeline wrench icon for the troubleshooter
- Tests:
  - backend 334 (+37)
  - frontend 36 (+6)
  - e2e 22 (+4; demo case PP-DEMO-D; FRONTEND_URL set for the e2e stack)
  - vite build OK
- Docs: TEST_SCENARIOS section T (T1–T10), END_TO_END_FLOWS Flow 15, refund policy template, CLAUDE.md layout,
  .env.example.
- Open: G30–G35 (above), G3, G26, G27.

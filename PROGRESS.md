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

## Open items (tracked) — updated 2026-11-04, Phases 17-18 + full regression
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
        Not yet run live. CLOSED by Phase 13 (2026-11-04): inquiries can now also be resolved with a partial refund
        offer or a replacement (make-offer), so the prompt no longer forbids offers; live check of make-offer = G111.
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
G31 [~] P2 (mitigated in 9B: the link binds to the first browser that opens it; others get "already open in
        another browser"; resend re-binds. Remaining: if the merchant opens it FIRST, the buyer is locked out
        until a new link is sent. Full fix = buyer signs in with PayPal.) Was: The merchant can open the buyer's troubleshooting link: it is in the PayPal
        thread the merchant can read, in the email-stub response, and in the stored dispute.raw (seller message).
        Proof uploaded later can't be attributed strictly to the buyer. Fix idea: bind the session to the first
        device that opens it, or buyer Log in with PayPal.
G32 [x] (9B: sessions close with "dispute resolved" when ingest sees RESOLVED) P3 Session status isn't closed when the dispute resolves (the buyer link already answers 410 "case
        closed"; the merchant card keeps the last status). Close sessions on ingest once 9B defines outcomes.
G33 [ ] P2 (9B: BUYER_PORTAL_URL setting added. Tunnelling the Vite DEV server publicly was blocked by the
        permission system, rightly: a dev server shouldn't face the internet. Options for the user: deploy the
        static frontend (Render) and set BUYER_PORTAL_URL, or have dev.sh tunnel a production build
        (vite build + a static server) instead of the dev server.) Buyer links use FRONTEND_URL (localhost:5173 in dev): only same-computer testing until the frontend is
        deployed (Render) or tunnelled; dev.sh tunnels the backend only.
G34 [ ] P3 Email invite is a dev stub (timeline entry; link shown once to the merchant to pass on).
G35 [ ] P3 Buyer rate limit is in-memory, per process (resets on restart, not shared across workers). Fine for one
        Render instance; memory is bounded (refilled buckets are pruned).
G36 [x] Refund policy template out of sync with the catalog ("handmade lamp"; "every physical order ships tracked"
        was false for the untracked phone case) -> rewritten for the electronics catalog.
G37 [x] CLAUDE.md layout listed a non-existent Analytics.tsx and missed Orders/Help/troubleshooting -> fixed.
G38 [x] e2e race (Orders card read before the list reloaded) and the auto-invite checkbox only flipping after the
        save -> test waits; checkbox updates at once and reverts on error.
G40 [~] P2 (9C: chat turns now run in the background (202 + live stream with a typing indicator), step buttons
        answer instantly without the LLM, and an LLM outage at intake gives the generic plan at once. Still open:
        the first plan can take ~1 min when providers are rate-limited; try the fastest provider first for plans.)
        The troubleshooting plan is built inside the buyer's chat request: live it took 76 s (Groq 429 on the
        free 8K TPM, then Gemini 3.8 timed out, 2.5-flash answered); the page promises "up to a minute".
G41 [x] (9D) Troubleshooting outcomes feed the dispute: troubleshooting_record evidence + rules + re-analysis.
G42 [ ] P3 Citations are checked for existence, not relevance: live, "E5" also matched car recalls
        (carexpert, drive.com.au stored as sources) and a proof step cited a "no recall found" page.
G43 [~] P3 (9D: app-wide toast "Buyer finished troubleshooting" + queue badge + re-analysis to GATHER_MORE with
        "safety issue: review personally".) Still no email/push when the app isn't open.
G44 [x] (9C checks + 2026-11-03) Proof files are deleted PROOF_RETENTION_DAYS (90) after the dispute is resolved
        (every 6 h in the app, or app/scripts/purge_proofs.py; record + checks kept, file endpoints answer 410),
        and the buyer can delete their own upload while the page is open (step goes back to awaiting proof).
G45 [x] Latent llm.py bug found live: strict_json_schema deleted fields named "title" but still required them,
        so Groq and Gemini rejected every troubleshooting plan -> fixed + regression test.
G46 [x] The e2e stack inherited real LLM keys from backend/.env (a chat test would have spent real quota) ->
        keys blanked for the UI suite.
G47 [x] Buyer page went blank in real Chromium (an effect returned scrollIntoView()'s Promise) and React
        StrictMode's double load tripped the device binding (403) -> fixed; e2e covers both.
G48 [x] Dead imports (events.py asyncio, store.py Decimal, runner.py llm_module) removed.
### Evals 2026-11-04 (real Groq gpt-oss-120b at t=0, judge gemini-2.5-flash)
- ActionCorrectness 50/51 (98%); Citation / Allowed actions / Baseline distance / No over-claim / Length / Tool
  correctness 51/51; Faithfulness 49/50; Decision explanation 44/50; Seller note 18/20; Economic reasoning 39/49; PII
  leakage, Step efficiency, Task completion 8/8. Report: backend/evals/reports/2026-11-04.md (= latest).
G156 [ ] P2 3 goldens skipped (Groq daily quota at the end: grid-inr-partial-above-chargeback,
        grid-snad-weak-above-inquiry, ts-fixed). Re-run with `scripts/run_evals.sh --only <ids> --merge` after the
        quota resets. The 14 troubleshooting conversations still wait for search grounding (G92).
G157 [x] Found by these evals: ts-defect -> NEEDS_HUMAN. The model's GATHER_MORE -> SETTLE cited the troubleshooting
        record, and the validator only accepted buyer messages / listings as a reason to move. A record where the buyer
        PROVED the fault now justifies a move toward settling, never toward FIGHT (G75). Tested; ts-defect is re-run
        with G156.
G158 [ ] P3 Economic reasoning [GEval] 39/49: the judge wants the settle threshold compared explicitly in more cases;
        a prompt wording item, not a decision error.

### Self-check + submission readiness (2026-11-04)
G151 [ ] P1 (for the user, at the end) Judge access to the deployed app: login is Log in with PayPal (AUTH_MODE=off is
        refused on Render). Add a dedicated sandbox BUSINESS account to MERCHANT_LOGIN_EMAILS and put its sandbox
        login in Devpost's testing instructions (sandbox only, no real money), or rely on the README run
        instructions (verified on a fresh clone, G152).
G152 [x] Fresh-clone check of the README instructions: clone, venv + pip install -r requirements.txt, .env from
        .env.example, seed + demo history, uvicorn /health + Analytics, npm ci + build. All passed. MIT LICENSE added
        (the rules need a detectable open-source licence).
G153 [x] Auth coverage self-check (tests/test_auth_coverage.py): with real login on, every API route listed in
        OpenAPI except the public storefront / webhooks / login / buyer help page answers 401 without a session.
        /mcp needs its own token; the catch-all MCP mount serves nothing else (404).

### Deployment choice (2026-11-04)
G154 [x] User chose Render's FREE plan. render.yaml: plan free, no disk, local SQLite paths, AUTO_SEED=true (new
        setting: rebuilds the demo store + demo analytics history on an empty DB at startup; idempotent; tested and
        verified with a simulated cold start + restart). DEPLOY.md section 7 covers the trade-offs: sleeps after 15
        min, the DB is empty after every sleep / deploy, the poller re-imports the sandbox disputes.
G155 [x] render.yaml covers every Phase 13-18 setting (role Gemini keys, BUYER_PORTAL_URL, SIGNAL_HASH_KEY,
        UPLOAD_EXTRACTION, PUBLIC_API_URL, SMTP / Slack, DEMO_MODE). Full git history scanned before going public: no
        secrets; the user's note file is excluded locally (.git/info/exclude).

### Security audit (2026-11-04): Group A fixed (no behaviour change for real users)
G159 [x] Image bomb: Pillow only WARNS between MAX_PIXELS and 2x, so a 50-100 MP proof was fully decoded (a 393 KB
        PNG took ~900 MB; Render free = 512 MB, ephemeral DB). Now the size is checked from the header before
        load(), the warning is an error, at most 2 decodes and 1 ffmpeg run at once. Tested (no decode happens).
G160 [x] Session / link tokens in ?token= / ?t= are redacted from uvicorn access logs (filter in app/main.py).
        The URLs still carry them: G166.
G161 [x] /docs, /redoc, /openapi.json are off on Render unless DEMO_MODE (docs_enabled()); local dev keeps them.
G162 [x] Trust page claims corrected: names are not redacted yet; the "data, never instructions" claim is limited
        to the troubleshooting chat and document reads (G168, G169).
G163 [x] Login errors: the callback sends a fixed code; the frontend shows only fixed messages, never URL text
        (anyone could craft /auth/callback#error=...). PayPal's error_description is no longer reflected.
G164 [x] Smaller fixes: SMTP STARTTLS verifies the certificate; research sources keep only http(s) links;
        DEPLOY.md / render.yaml name the real API host (disputeshield-api-l7ne); MCP explain_recommendation
        returns draft_note and get_case labels buyer-written evidence trust="untrusted_buyer_text"; auth self-check
        lists public routes exactly (now also checks /api/auth/me), checks every /api/help route without the
        link token, admin routes 404 off / 403 wrong token, docs off on Render, log redaction.
        Local only (not in git): chmod 600 on the .db files. Demo admin token was already in sessionStorage.
G165 [x] Group B fixed (one commit):
        - buyer help link: token checked BEFORE the per-session limit (strangers can't lock a buyer out);
          wrong tokens limited per IP (20/min)
        - proof uploads: 3 per step, 15 stored + 40 ever per session, 150 MB per session, refused under 500 MB
          free disk; at most 1 ffmpeg run at once (G159)
        - per-IP limits (app/ratelimit.py, in memory, loopback exempt so dev.sh / e2e / evals are untouched):
          store orders 10/min, login start + callback 20/min, Ask 10/min, webhooks 120/min, wrong admin / demo
          tokens 5/min (only failures count)
        - client IP: Cloudflare's CF-Connecting-IP on Render (verified: the live API is served through
          Cloudflare), else the right-most X-Forwarded-For hop; used for limits and checkout signals
        - webhooks: 64 KB cap (also for chunked bodies); PayPal-shaped headers + cert URL on a PayPal host
          checked before calling PayPal; the dev recorder writes only VERIFIED events (still feeds goldens)
        - abandoned checkouts (never paid, no dispute, not a demo scenario) removed after 24 h by the
          retention loop
        - approved_by / rejected_by come from the session (a client value is ignored, not refused); settings and
          MCP-token changes go to a new audit_log table (who, old -> new)
        - merchant allowlist re-checked on every request: removing an email from MERCHANT_LOGIN_EMAILS ends its
          sessions at once (G151 judge account)
        - MCP tokens expire after 7 days (Settings shows when)
        - strict same-app next= filter on both sides (no backslash / tab / newline tricks)
        - SSE: at most 3 open live-update streams per buyer link
        - Render refuses to start with the built-in signal key; ADMIN_TOKEN under 32 chars logs a warning
        Deliberately NOT done (would disrupt): react-router 7 (major upgrade; moved to G167; the backslash
        redirect it fixes is blocked by the strict filter, the SSR advisory doesn't apply), a +-5 min webhook
        timestamp window (PayPal re-delivers for 3 days and the fixtures replay real recordings),
        refusing a short ADMIN_TOKEN at startup (can't see the Render value; warning instead), re-confirming
        alert_email_to changes (a flow change; G167).
G166 [~] (2026-11-05 done: notes refuse links / emails / instruction-like text / insults, also for MCP notes;
        a suspect proof can't justify a move. OPEN: buyer_message direction rule, troubleshooter plan/translation
        guard, DEFECT_CONFIRMED provenance; these change AI output and need an eval run) P1 Group C (changes AI output; run evals before/after): buyer_message justifies moves toward FIGHT only;
        defect_cited must exclude ts_proof_suspect; stronger validate_note (URLs, PII, admissions, insults,
        injection words; re-run at approve / retry / MCP); troubleshooter guard on plans + translations
        (multilingual, URL ban; beware "replace the battery" false positives); DEFECT_CONFIRMED only from button
        presses + unverified-proof flag + dHash duplicates + deleted-proof tombstone.
G167 [~] (2026-11-05 done: react-router 7, no ?token=, dashboard-only MCP code, MCP note checks, server-side
        Log out ending every session. OPEN: re-confirm alert_email_to, email_verified/payer-id pinning, AUTH_MODE=off
        loopback-only (already refused on Render), CSP) P2 Group D (changes a flow; safe first step first): react-router 7 upgrade (run all e2e); re-confirm alert_email_to changes; MCP confirmation shown on the dashboard, not returned
        to the assistant; validate MCP note; remove ?token= (fetch with the header / fetch-event-source); session
        epoch + logout; email_verified + payer-id pinning (log a real sandbox userinfo first); AUTH_MODE=off only
        with ALLOW_INSECURE_LOCAL from loopback (update e2e / dev.sh / CI together); CSP (report-only first).
G168 [x] (2026-11-05: buyer name from the dispute redacted whole-word; phones/cards in buyer-written evidence) P2 Redact buyer names / phones in buyer_message and troubleshooting summaries before the analyst and note
        LLM calls (redact_buyer_text is only used in the troubleshooting chat today).
G169 [x] (2026-11-05: trust=untrusted_buyer_text + prompt rule in analyst and note prompts) P2 Mark buyer text as untrusted data in the analyst and note prompts (as the troubleshooter does).

### Live review + fixes (2026-11-05)
G170 [x] Live review 2026-11-05 (Claude, Cowork; real Render deploy + sandbox) found and fixed:
        - P1 after every free-plan restart, real disputes showed "No store order matched" (order rows lost with the
          DB), so every case scored 8/100 and could only SETTLE. The investigator now rebuilds the order from PayPal
          (capture -> supplementary_data.related_ids.order_id -> GET order; only invoices starting DS-, never another
          shop's data; idempotent) with its items, amount, address, payer hash and PayPal trackers as shipments.
          Merchant-recorded delivery is not at PayPal, so it must be recorded again (the timeline says so).
        - P1 cold start re-analysed every case at once: Groq 8K TPM 429 + a broken Gemini key -> NEEDS_HUMAN, and the
          timeline showed the raw provider error with the Groq organisation id. LLMError text is now safe to show
          (status + plain words; full body in the server log only), a 429 asking for <= 20 s is waited out once, at
          most 2 LLM calls run at once, and a provider outage is no longer fed back into the next prompt.
        - Case page said "The agents are still working…" forever on NEEDS_HUMAN with no decision -> explains it and
          offers Re-analyse; a resolved case says there is nothing to decide.
        - Approve race: a losing second click said "already resolved at PayPal" -> says the decision's real status.
          Retry: two simultaneous retries could both send (one process lock per decision now).
        - Analyst said "0% chance to win" while the Options card said 15% -> a stated win chance must match the
          calculator (+-1 point) or the answer is retried.
        - Deadlines showed "Gathering evidence" for cases PayPal is reviewing / waiting on the buyer -> right label.
        - Security (G167 items): session token no longer accepted in ?token= (streams via fetch + header, files and
          proof photos fetched with the header into blob: URLs); MCP approvals need a 6-character code shown ONLY on
          the dashboard case page (never returned by a tool, never echoed in errors; 5 wrong tries lock MCP approval
          of that decision); API sends nosniff / X-Frame-Options DENY / frame-ancestors 'none' / no-referrer; static
          site headers in render.yaml; react-router-dom 7.18.5 (npm audit: 0 vulnerabilities).
G171 [ ] P1 USER: the Gemini key on Render is invalid (live: HTTP 401 "API key not valid",
        ACCESS_TOKEN_TYPE_UNSUPPORTED). It breaks the LLM fallback, upload reading, troubleshooting research and the
        photo check. Re-paste GEMINI_API_KEY / RESEARCH_GEMINI_API_KEY (an AIza... key from aistudio.google.com).
G172 [ ] P2 USER: render.yaml now sets static-site headers; Render applies blueprint changes on the next Blueprint
        sync (Dashboard -> Blueprints -> Sync) or add them under the static site's Headers tab.
G173 [ ] P3 Full CSP for the static site (PayPal JS SDK, Bryntum, AG Grid inline styles) not done: report-only first.

### Found in Phases 17-18 and the full regression (2026-11-04)
G140 [x] MCP server verified LIVE with the official SDK client (streamable HTTP) on a dev-DB copy:
        - the 5 tools; list / get_case / explain_recommendation on real cases
        - a wrong confirmation is refused and logged on the timeline (the decision stays PROPOSED)
        - forged or missing token -> 401
        The correct-confirmation approval was NOT run live: it would really refund 45 USD in the sandbox (the
        user's call). Tests cover it with PayPal mocked.
G141 [ ] P2 Neither SMTP nor Slack is configured, so alerts are recorded only (verified live: alert:proposed:19
        "recorded"). Set SMTP_* and/or SLACK_WEBHOOK_URL to send them (DEPLOY.md section 6).
G142 [ ] P3 PayPal Agent Toolkit (Python) NOT used. It was optional in the brief. Its read-only overlap is
        list_disputes / get_dispute / get_order / get_shipment_tracking. It targets agent frameworks (OpenAI Agents,
        LangChain, CrewAI, Bedrock) with its own HTTP calls, without our retry / PayPal-Debug-Id logging, and the
        investigator already has tested wrappers. Revisit only if a judge wants it.
G143 [x] (2026-11-05: get_case masks buyer emails, phones and card numbers) P3 MCP tools give the connected assistant the case data (evidence summaries can include the buyer's
        email). The README warns about this; consider redacting for MCP.
G144 [ ] P3 Alerts go out from a 60 s loop (a deadline alert can be up to a minute late). SMTP is STARTTLS only (no
        port-465 SSL).
G145 [ ] P1 USER DECISION (Bryntum): the Deadlines view uses the PUBLIC TRIAL package
        (@bryntum/scheduler-trial 7.3.7 via npm alias). Its README says "evaluation use only" and the trial watermark
        shows. Devpost's rules page is behind login (403 for automated fetches), so the exact "Best Use of Bryntum"
        wording and licence terms are unconfirmed. Check the rules, or ask Bryntum for a hackathon licence (then
        only the alias in package.json changes). The trial's postinstall (it stamps a per-install UUID into the
        bundle, used with bryntum.com/verify) was NOT run, because npm's install-scripts policy blocked it; the
        component works without it.
G146 [x] P2 (resolved by the official rules: the prize is "Best Use of AG Grid", any AG Grid tools) AG Grid's own blog names its prize "Best Use of AG Studio" (not AG Grid). Phase 14 uses AG Grid + AG
        Charts Community; check what AG Studio is and whether the entry needs it.
G147 [ ] P3 The Deadlines chunk is 3.8 MB (1.5 MB gzip), lazy-loaded on /deadlines only.
G148 [ ] P3 The "PayPal reviewing" bar's end is an estimate (now + 7 days): PayPal gives no review end date.
G149 [x] P1 Found in the live regression: real disputes that resolved before Phase 14 had no outcome row, so real
        Analytics stayed empty. A startup backfill (analytics/outcomes.backfill, idempotent) writes them; tested.
G150 [x] Deadlines: real dispute ids were truncated in the first column -> wider.
Observation (G112): the real sandbox inquiry PP-R-MJZ allows escalate / accept_claim / send_message but not
make_offer, so no partial offer is proposed there: correct.

### Found in Phase 16 (2026-11-04)
G135 [ ] P2 Demo scenarios not run live. Check with DEMO_MODE=true + ADMIN_TOKEN:
        - the real sandbox order is created
        - the Store link (/store?pay=<id>&sku=…) pays that exact order through JS SDK v6 (session.start with an
          existing orderId)
        - the capture hook adds delivery / history
        - a hand-opened dispute arrives
G136 [ ] P3 Whether the sandbox "Report a problem" offers an "unauthorised" option for a store payment isn't
        verified; the scenario's checklist says to use "item not received" if not (the earlier-orders evidence shows
        either way).
G137 [x] P3 (the Trust page now shows the 2026-11-04 report) The Trust page shows the latest eval report as it is: 2 Nov, partial run, gates not passed (G100: the
        prompts changed since, so it needs a full re-run). The page says so instead of hiding it.
G138 [ ] P3 Tour step 4 highlights the Recommendation card when the chosen case has no Options card (cases analysed
        before Phase 13, seeded demo decisions). It prefers a case awaiting approval.
G139 [x] A scenario order is created by the merchant's click, so its IP/device hashes would have been the
        merchant's. The capture step now records the paying browser's hashes when the order has none (also helps any
        order created without them).

### Found in Phase 15 (2026-11-04)
G129 [ ] P2 Checkout signal capture not run live: needs one sandbox purchase from the Store page. The fingerprint
        uses crypto.subtle, which needs https or localhost; the tunnel is https. Check the order gets
        ip/device/payer hashes.
G130 [ ] P3 Sandbox fact: every real sandbox dispute comes from the same test buyer, so each one shows 2-4 "other
        disputes in 12 months" (serial disputer). Correct behaviour; mention it in the demo, or use a second buyer.
G131 [ ] P3 Orders from before Phase 15 have no IP/device hashes, so they can only match partially (address /
        account). A real CE 3.0-like match needs purchases 120-365 days apart with hashes. Tests and goldens cover
        the logic; live data can't show it within the hackathon.
G132 [ ] P3 Visa CE 3.0's "same card" becomes "same buyer account" (email or PayPal payer id): PayPal doesn't
        expose the funding card to the merchant. Documented in app/signals.py and the summaries.
G133 [ ] P3 The 3 new signal goldens (sig-*) haven't run against the real LLM (with the G100 batch).
G134 [x] The buyer-history summary said "serial disputer" in a sentence that reaches PayPal through the evidence PDF:
        now neutral ("N other dispute(s) by this buyer in the last 12 months"); the framing stays merchant-side.

### Found in Phase 14 (2026-11-04)
G121 [x] P2 (verified 2026-11-04 on the 2 real resolved sandbox disputes, via the backfill G149: RESOLVED_BUYER_FAVOUR + amount_refunded) Outcome capture not seen live yet. The mapping follows the spec's dispute_outcome.outcome_code enum +
        amount_refunded, and offer.history for an accepted offer. It needs a sandbox dispute that resolves (e.g. after
        an approved accept-claim / offer) to confirm the GET carries dispute_outcome.
G122 [x] P2 (verified 2026-11-04: real Groq turned "which SNAD cases did I settle under $25" into the right whitelisted query in 1.4 s) "Ask your disputes" not run against the real LLMs (tests and e2e use a scripted LLM / stub; Groq quota,
        G100). Check that Groq/Gemini accept the AskQuery schema (bounds now stripped from strict schemas, G128).
G123 [ ] P3 The Analytics chunk is 1.46 MB (438 KB gzip), lazy-loaded on /analytics only. AG Charts' core dominates
        even with only the needed modules registered.
G124 [ ] P3 Accuracy only scores cases PayPal decided (contested). Settled cases have no counterfactual, and
        GATHER_MORE isn't a prediction. RESOLVED_WITH_PAYOUT is excluded from win rate and money (the spec doesn't
        say who was covered). Documented in analytics/kpis.py and outcomes.py.
G125 [x] P3 (fixed Phase 16: one amount per currency) Money tiles show the first currency when outcomes span several (the KPIs are per currency).
G126 [x] AG Charts 14 takes axes as an object keyed x/y. An array was silently ignored (no % / $ axis labels), and
        `as never` casts hid it. Now the options are typed against AG Charts' types; a unit test pins it.
G127 [x] Monthly accuracy swung 0% <-> 100% on months with 1-2 checkable cases: the chart shows months with >= 3,
        with the count in the tooltip.
G128 [x] Strict JSON schemas now drop maxLength / minLength / maxItems / minItems / pattern (not reliably accepted by
        strict modes); Pydantic still enforces them on the parsed answer (invalid -> retried once, rule 7).

### Found in Phase 13 (2026-11-04)
G111 [ ] P2 make-offer not run live. Shapes come from the v1 OpenAPI (offer_type enum REFUND / REFUND_WITH_RETURN /
        REFUND_WITH_REPLACEMENT / REPLACEMENT_WITHOUT_REFUND; offer_amount money; note 1-2000; invoice_id). It needs
        a sandbox not-as-described inquiry whose dispute lists make_offer, and the user's approval (it moves money if
        the buyer accepts). Also check the buyer's accept/deny reaches us as CUSTOMER.DISPUTE.UPDATED / RESOLVED
        with offer.history.
G112 [ ] P2 Offer types are taken ONLY from allowed_response_options.make_offer.offer_types (rule: no guessing). If the
        sandbox sends a make_offer link without that list, no offer is proposed (falls back to accept-claim). Check
        what a live inquiry returns.
G113 [x] P2 (user decision 2026-11-04: keep the keep-the-sale formula) USER DECISION. The brief's fight formula "P(win) x amount - (1-P(win)) x (amount+fee) - handling" counts the
        amount twice next to settle = -amount (fighting would beat refunding even at 15% odds). Implemented with the
        keep-the-sale baseline: fight = -(1-P(win)) x (amount+fee) - handling (documented in rules/money.py). The
        partial-offer formula uses + (1-P(accept)) x fight (fight is already a net value). Say if you want the
        literal formula instead.
G114 [~] P3 (P(win) calibrated in Phase 14 once a reason has >= 10 real outcomes; P(accept) still an assumption) P(win) (rule_score table) and P(accept) (0.1 + 0.8 x offer share) are starting assumptions, not calibrated:
        Phase 14 recalibrates from real outcomes. The UI says they're estimates.
G115 [ ] P3 REPLACEMENT has no expected net (item + shipping cost unknown); a "replacement cost" setting would let the
        calculator rank it.
G116 [ ] P3 No eval goldens with offer types yet (all 51 have none, so they're unchanged); add inquiry goldens where
        PayPal lists REFUND offers, and re-run evals (G100) to see the real LLM choose amounts.
G117 [x] can_settle was true when only make_offer was allowed, but SETTLE sends accept-claim (it would have failed at
        resolution). Now SETTLE needs accept_claim; offers need the listed offer types.
G118 [x] UI: "Change action" kept the previous action's drafted note, so switching FIGHT -> SETTLE would send the seller's
        fight note as the refund note. The note now resets to that action's own draft (or empty: server writes it).
G119 [x] An offer denial recorded first by an agent run's refresh (before the webhook) was suppressed as "PayPal
        processing your response". Ingest now also checks state: our last decision was a sent offer + the buyer's
        newest answer is DENIED.
G120 [x] Frontend rounding differed from the backend on .5 cents (-146.845 -> -146.84 vs -146.85); now half away from
        zero like Decimal ROUND_HALF_UP.

### Found in Phase 12 (2026-11-03)
G103 [x] P2 (user decision 2026-11-04: keep extraction ON; merchants are told before uploading) USER DECISION (like G50). UPLOAD_EXTRACTION is on by default: the merchant's uploaded documents (labels,
        carrier proof of delivery, receipts) go to Gemini, and they often show the buyer's name and address. On
        the free tier, inputs may be human-reviewed. Options: keep it on (the merchant is told before uploading),
        turn it off (the merchant types the fields), or use a paid key.
G104 [ ] P2 Several files in one provide-evidence / provide-supporting-info call are sent as parts file1..fileN
        (the PDF first, then the merchant's files). One file was verified live earlier. The API reference names
        the part "evidence-file", and the guide's sample hides the field name, so multi-file sending isn't proven
        live. It needs one approved sandbox FIGHT with an upload (the user's call: it moves money).
G105 [x] P1 Pre-existing bug, found by the new e2e test: on a fresh database the first case-detail request
        answered 500. settle_threshold() created the settings row and committed, which expired the timeline rows
        already loaded. Fixed (the threshold is read first); regression test added.
G106 [ ] P2 Gemini extraction not verified live: the main GEMINI_API_KEY answered 429 on 3.8-flash, 2.5-flash and
        3.5-flash-lite (2026-11-03). The role keys were not used for it (one key per task). Retry after reset, or
        add a dedicated key for the "extraction" role (gemini_key_for falls back to GEMINI_API_KEY).
G107 [ ] P3 Extraction runs inside the upload request (up to 45 s per model, two models). Move it to a background
        task with a live update if it feels slow in the demo.
G108 [ ] P3 The coach assumes the added item is clean (no red flags, e.g. delivered to the order's address), and it
        shows only the rules' baseline, not the analyst's one-step move. If PayPal allows no FIGHT right now it
        honestly shows GATHER_MORE after the item.
G109 [x] P3 (fixed Phase 13: purge_expired_uploads, same retention) Merchant uploads aren't covered by the proof-retention purge (G44 covers buyer proofs only).
G110 [x] P3 (fixed Phase 13: queued behind the current run) Confirming evidence while a run is going saves it but doesn't start a run ("re-analyse when it's
        done"), the same gap as G98.

### Found in Phase 11 (2026-11-03)
G97 [x] P1 The investigator didn't catch httpx transport errors (timeout / connection reset): one PayPal timeout
        crashed the whole run (run_failed). Now caught; timeouts, 5xx and 429 count as transient (last good copy kept,
        flagged stale), 4xx as "gone".
G98 [x] P2 (fixed Phase 13: runner.PENDING, one fresh follow-up run) Runs that aren't manual (webhook, poll, troubleshooting re-analysis) arriving while another run for the
        same dispute is going are skipped ("Run skipped: another run for this dispute is in progress" on the
        timeline), not queued. A buyer finishing troubleshooting during a manual re-analyse is picked up only at the
        next event / re-analyse. Fix option: a per-dispute "rerun pending" flag that starts one follow-up run.
G99 [ ] P3 One-run-at-a-time is an in-process set plus the run_started marker on the timeline: two processes could both
        pass the check in the same instant, and a crashed run blocks re-analyse for up to 15 min (STARTED_STALE).
        Fine for the single Render instance; use a DB lock row if it's ever scaled out.
G100 [x] P2 (done 2026-11-04: full real-model re-run, report 2026-11-04) The Phase 11 prompt change (freshness rule + "Data freshness" line) invalidates the eval cache: the next
        `make evals` re-runs all 51 analyst goldens against real models (Groq quota). Run it together with the G74
        final check. (The freshness line has only the relative age, so the cache key stays stable between runs.)
G101 [x] Stale-required check: a required type that also has a fresh item (store tracking while PayPal's tracker
        copy is stale) counts as covered; only types we have solely as stale copies block FIGHT.
G102 [x] PayPal buyer messages re-read from the stored dispute copy after a failed refresh are flagged stale too.

### Per-role Gemini keys (2026-11-03)
G95 [x] Dedicated Gemini keys per role, as the user asked (RESEARCH_ / EVALS_JUDGE_ / EVALS_SIMULATOR_GEMINI_API_KEY in
        backend/.env, git-ignored; unset -> GEMINI_API_KEY; never fails over to another role's key).
G96 [ ] P2 On the new keys: gemini-2.5-flash is "no longer available to new users" (404, use 3.8-flash),
        gemini-3.8-flash answered 503 "high demand"/timeouts, gemini-3.5-flash-lite works. Google Search grounding
        is refused at once ("exceeded your current quota") on the fresh research key while plain generation works:
        the free tier seems to give new projects no grounding quota (check aistudio.google.com/rate-limit). The
        10C research fixture therefore still needs the original key's grounding (after reset), Tavily, or billing.

### Found in Phase 10C (2026-11-03)
G74 [x] P1 (verified 2026-11-04 on the real model: ActionCorrectness 50/51 = 98%, up from 40/51; SETTLE expected -> SETTLE 18/19) APPROVED + fixed in the analyst prompt (moves are directional: evidence that weakens the claim moves only
        toward FIGHT, evidence that strengthens it only toward SETTLE; missing evidence / repeated complaints /
        red flags never justify a move; the meaning of SETTLE vs FIGHT spelled out). Verified live on the first
        version: 8/9 G74 failures fixed (all weak/partial-below-threshold cells now SETTLE), guards held
        (listing-contradicts moves to FIGHT, strong cases FIGHT, partial-above GATHER_MORE). The 9th (demo B) was a
        genuinely borderline golden: GATHER_MORE now also acceptable (reason in its notes). Still to confirm live
        (Groq daily tokens ran out): the final wording at temperature 0 on adv-address-mismatch and ts-defect.
G90 [x] FOUND by the G74 verification: the LLM calls ran at the providers' default temperature 1.0, so the same
        dispute could get different recommendations run to run (two cases flipped between identical runs) ->
        temperature 0 + fixed seed (LLM_TEMPERATURE / LLM_SEED; Groq docs: 0-2, seed best-effort; Gemini: seed,
        temperature ignored on 3.6 Flash+). The eval cache key includes them.
G91 [x] Unjustified-move retries only said what was wrong; at temperature 0 the model repeated FIGHT on the
        address-mismatch case -> the retry now names the baseline to use when nothing in the case could justify a move.
G92 [ ] P2 10C live run pending: the research fixture must be recorded from real search (`python -m
        evals.record_research`) and the 14 conversations run (`scripts/run_evals.sh --only conv- --merge`) once the
        Gemini and Groq quotas reset; the harness is verified offline only.
G93 [x] Fixed-text replies (13 tables) now in en/es/fr/de/pt/hi with matching placeholders, proof kinds translated,
        and regional codes (es-MX, pt-BR) fall back to their base language (they used to get English).
G94 [x] The decision-golden builder deleted every *.json in goldens/ before regenerating (it would have wiped
        troubleshooting.json) and the loader read it as a decision golden -> both skip it.
G75 [x] (user decision 2026-11-04: add the rule; done: economic_baseline(defect=...) + analyst validator + prompt; tests in test_evidence_rules.py) After the directional fix ts-defect gave GATHER_MORE once and FIGHT once (before temperature 0);
        a deterministic "never auto-FIGHT a confirmed defect" rule is still your decision.

### Found in Phase 10B (2026-11-02/03) — eval limitations are listed in backend/evals/README.md
G80 [x] DAG judgement nodes had no evaluation_params: the judge saw no text and scored every explanation 0 (live)
        -> nodes judge the actual output; regression test proves the old code fails it.
G81 [x] PII metric counted redact.py's [email] / [shipping address] placeholders as leaked PII (live; the prompt had
        no real email/address) -> DeepEval template subclass tells the judge placeholders are redacted values.
G82 [x] StepEfficiency called the full investigation "redundant store tools" -> the traced task now states that
        gathering the whole case file is the job (and run_case always uses the current task text).
G83 [x] Traces held live objects (sessions, investigator) -> "Circular reference" when caching -> plain() converter.
G84 [x] The judge kept retrying a DAILY quota 429 for minutes per metric -> fails fast and skips that model.
G85 [x] SETTLE "notes" (our fixed accept-claim template) were graded as AI writing -> note quality on FIGHT only.
G86 [x] P2 (done 2026-11-04: re-judged with gemini-2.5-flash; Decision explanation now 44/50 measured, 0 errored) The committed report (51 goldens) is incomplete: 38 judged results unmeasured (both free judge quotas,
        gemini-2.5-flash and 3.5-flash-lite, ran out on 2026-11-02) and it was judged by flash-lite. Next: rerun
        `scripts/run_evals.sh --merge` after the quota resets (cache resumes), ideally EVALS_JUDGE_MODEL=gemini-2.5-flash.
G87 [ ] P3 CI isn't active: no git remote yet (your call), and the repo needs GROQ_API_KEY + GEMINI_API_KEY secrets
        for the nightly evals. The e2e suite isn't in CI.
G88 [x] Deprecated DeepEval APIs (DAG `children=`, LLMTestCase additional_metadata, LLMTestCaseParams) -> current.
G89 [ ] P2 (with G75) ts-defect: the model fought a buyer-proven defect and its seller note concedes the fault
        (note quality 0.3). Decision G75 (never auto-FIGHT a confirmed defect) would remove both.

### Found in Phase 10A (2026-11-02)
G68 [x] SECURITY: installing deepeval registered a pytest plugin that loads backend/.env into os.environ (real
        PayPal secret, LLM keys, ADMIN_TOKEN) and sends telemetry on every `pytest -q` (2 tests failed from leaked
        values) -> backend/pytest.ini `-p no:deepeval`; tests/test_hermetic.py fails if deepeval is ever loaded.
G69 [x] `deepeval test run -c x` means deepeval's --use-cache, not pytest's -c: metrics were served from deepeval's
        cache (stale/empty) -> run_evals.sh passes --config-file=.
G70 [ ] P3 DeepEval's LangGraph CallbackHandler (deepeval 4.3.2) raises TypeError in on_chain_start on our graph
        and splits nodes into separate traces -> not used; runner wraps graph.ainvoke in one @observe span instead.
G71 [x] Judge tried gemini-3.8-flash first: free-tier timeouts blew DeepEval's 180 s per-case budget -> judge order
        2.5-flash then 3.8; per-case budget 900 s (DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE).
G72 [x] A transient provider failure (429 after retries, timeout) would have been cached as the model's answer
        (NEEDS_HUMAN) -> reported as provider_error / rate_limited, never cached.
G74 [ ] P1 (found by the evals) With a weak case (baseline SETTLE) and a buyer message that SUPPORTS the complaint,
        the real model moves one step to GATHER_MORE "because evidence is missing" (10 of 48 goldens). The
        validator accepts any one-step move citing a buyer_message/listing, but the policy means evidence that
        WEAKENS the claim ("it arrived"). Proposed fix (needs a live re-run to verify, not started after the host
        stopped the run for low memory): prompt says moves toward the merchant need evidence against the claim and
        that missing evidence is already in the baseline.
G75 [x] P2 (decided + done 2026-11-04, see above) DECISION (yours): ts-defect (buyer proved the fault) -> the model moved GATHER_MORE -> FIGHT citing the
        listing. Option: a rule like the safety one (defect confirmed without suspect proof: never auto-FIGHT).
G76 [x] Address mismatch: GATHER_MORE with no missing items -> NEEDS_HUMAN. Nothing required is missing (the case is
        weak because of a red flag) -> code names "evidence that resolves: <red flag>" (rule-owned), like G59.
G77 [x] (2026-11-05: receipt wording allowed in not-as-described notes) P2 adv-listing-contradicts: correct FIGHT, but both seller notes were rejected (resolution gave up).
        Hypothesis: validate_note's delivery-claim check rejects "the buyer received ..." although in a not-as-
        described dispute the buyer's own complaint admits receipt. The harness now records resolution_problems;
        confirm on the next run before changing the validator.
G78 [x] Two goldens of mine had an order line ("desk lamp") contradicting the listing (mug / phone case); the model
        rightly flagged it -> fixed in build_goldens.py (product per order).
G79 [x] Claim checker counted "needs evidence that it was delivered" as a delivery claim -> requests for proof don't
        count; "tracking shows it was delivered" still does.
G73 [ ] P3 Eval quota: a full --refresh is ~80 Groq calls (~2K tokens each, 15 s apart, ~20 min) + ~150 judge
        calls; Groq's free 200K tokens/day allows ~1-2 full refreshes a day (a daily 429 skips the rest; the next
        run resumes from the cache).

### Found in Phase 9D (2026-11-02)
G59 [x] Live: for a safety stop the LLM chose GATHER_MORE but named no missing item -> NEEDS_HUMAN twice. Code
        now fills "your personal review of the buyer's safety report" (rule-owned next step).
G60 [x] Live: the LLM wrote "PayPal requires a personal safety review" -> prompt says it is DisputeShield's rule.
G61 [x] A background refresh failing once (5xx/offline) replaced the whole buyer page with an error -> only the
        first load or a link refusal (403/404/410) does.
G62 [x] The buyer page showed "typing…" forever when the seller was chatting (no assistant turn starts) -> the
        202 reply says whether a turn started.
G63 [x] Queue status cell used colDef.autoHeight, which needs an unregistered AG Grid module (console error) ->
        chips side by side, wider column.
G64 [x] "View source" didn't know ts:session/... refs -> shows the session (status, steps, proofs, checks; never
        the link token or device hashes).
G65 [ ] P3 "Reply as seller" works only while the buyer's page is open; after "Talk to the seller" (page ended)
        the merchant replies in the PayPal case. Reopening the page for a seller-only chat is a possible later step.
G66 [ ] P3 Re-analysis after troubleshooting is skipped while a decision is being sent (APPROVED); the next PayPal
        update re-analyses with the result (its run key includes it).
G67 [ ] P3 Merchant panel refreshes every 8 s while the buyer's page is open (polling; buyer chat messages don't
        create timeline rows, so the case page's SSE doesn't carry them).

### Found in Phase 9C (2026-11-02)
G49 [ ] P3 Buyer page chrome (buttons, labels, outcome screens) is English only; the assistant's replies and the
        steps are in the buyer's language, and the fixed step-button replies have English + Spanish only.
G50 [ ] P2 DECISION (yours): the advisory vision check is built and verified live but OFF by default
        (PROOF_VISION_CHECK=false). Gemini's free-tier terms allow human review of inputs and ask not to send
        personal information; buyer photos can show people or homes. Turn on with a paid key, or after adding
        buyer consent text to the upload box.
G51 [ ] P3 Videos over 10 MB get an evidence-size copy only when ffmpeg is installed (the host may not have it;
        then the card says "over 10 MB, not ready as evidence"). Compression runs in the web process (CPU).
G52 [ ] P3 "Same buyer" for cross-case duplicate checks = same order email; orders without an email aren't linked.
        MP4 creation time is trusted as UTC (some phones write local time) - it's a flag, never a decision.
G53 [ ] P3 Buyer live stream and the "assistant is typing" flag are in-process (one web worker), like G12/G35.
G54 [~] P2 (9D: the FIGHT evidence PDF gets a troubleshooting page + up to 3 clean proof photos.) Videos are
        never sent (PayPal accepts jpg/png/pdf only); suspect photos are left out on purpose.
G55 [x] A video whose header says 0 seconds was accepted as proof -> now "couldn't read this video".
G56 [x] Vision only tried the first configured model; gemini-3.8-flash answered 503 live -> tries each model.
G57 [x] Big photos were decoded/re-encoded on the event loop (would stall every buyer's live stream) -> threadpool.
G58 [x] analyst.py "unused" get_settings import is intentional (noqa, monkeypatch target) - left as is.
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

## 2026-11-02 — Phase 9B: the troubleshooting agent
- app/agents/web_research.py: research(product, model, symptom) returns ResearchNotes.
  - Gemini Grounding with Google Search via the **Interactions API**. Checked the docs: generateContent /
    groundingMetadata are no longer documented for search.
    - POST /v1beta/interactions with tools [{type: google_search}] and store=false (the API stores
      interactions by default)
    - citations come from steps[model_output].content[].annotations url_citation, whose offsets are BYTES
  - Fallback: Tavily (/search, Bearer, exclude_domains as real domains). groq/compound is not used.
  - Marketplace/forum domains are dropped when anything else exists; official and support domains rank first.
  - 24 h cache table research_cache. Sources are stored in ts_sources.
  - **Verified live 2026-11-02** (scratch DB): 7 Google queries (incl. recall, firmware, safety notice) and
    8 cited sources.
- app/agents/troubleshooter.py:
  - build_plan: research, then an LLM plan with a strict schema. The validator checks: 3-7 steps; every step
    cites a stored source; ≤60 words; average sentence <20 words; one proof step; no casing / mains / sealed
    battery / soldering / bypass.
  - It retries once with the problems, then falls back to the generic safe plan, which cites an internal
    "not product-specific" source.
  - Plans are validated in English and then translated for the buyer.
  - **Live**: draft 1 was rejected (long sentences), the retry passed, Spanish translation, every step cited.
  - Intake: one open question, at most 2 clarifying questions. Language is detected by a heuristic plus the
    LLM, and replies are in that language.
  - agent_reply returns AgentTurn:
    - buyer text sits inside <buyer_message>, framed as data
    - a code guard blocks outcome promises, sensitive-data requests and blame (retry once, then a neutral reply)
    - only the buyer's button sets RESOLVED_FIXED
    - DEFECT_CONFIRMED is computed by code: all steps finished-not-fixed plus the requested proof
    - DECLINED → EXPIRED; HANDOFF → HANDED_TO_MERCHANT
  - Safety: a multilingual keyword list with English negation handling ("no smoke"), plus the LLM's flag.
    A hit gives SAFETY_STOP, a fixed message in the buyer's language, and the merchant timeline entry
    "SAFETY STOP". The Hindi "आग"/"आगे" false positive was caught in testing and fixed.
  - PII: redact_buyer_text (emails, order addresses, Luhn-valid card numbers, phone numbers) runs before every
    LLM call; tracking numbers are kept.
- Buyer API:
  - POST /api/help/{sid}/messages | /confirm | /handoff | /steps/{id}/proof
  - The proof is the raw body; its type is sniffed from the magic bytes; the size is capped while streaming.
  - Chat rate limit; 60-message cap; per-session lock.
  - Device binding (G31): X-Help-Device.
- Merchant API: status now includes steps, sources, transcript and proofs; a proof file endpoint.
- UI:
  - The buyer page is a chat with step buttons, sources, proof upload, the "Is it working now?" confirm, and
    "Talk to the seller".
  - The merchant card shows the problem, language, steps with proof links, sources and the conversation.
- Fixed this phase: G31 (mitigated), G32, G45-G48, and the live-found misleading "generic plan" reason.
  G33 is blocked (see above).
- Tests:
  - backend 380 (+45)
  - frontend 43 (+7)
  - e2e 25 (+3; demo cases PP-DEMO-F/G; hermetic LLM keys)
  - vite build OK
- Docs: TEST_SCENARIOS T11-T20, END_TO_END_FLOWS Flow 16, .env.example, render.yaml (TAVILY_API_KEY),
  DEPLOY.md, CLAUDE.md layout.

## 2026-11-02 — Phase 9C: the buyer page (mobile-first) + proof checks

ReAct summary: most 9C backend pieces existed from 9A/9B (token/device checks, agent, sniffing), so this phase
filled the gaps and rebuilt the page; each new external call was checked against the docs and verified live.

- Backend API (`/api/help/{sid}`, link token + bound device on every route; merchant credentials refused):
  - GET adds the header: store name (STORE_NAME), order ref, product image, PayPal case id; `agent_working`.
  - POST /messages -> **202**; the agent turn runs in the background with its own DB session (a crash still
    answers the buyer and clears the typing flag); one turn at a time per session.
  - POST /steps/{id} {status, note}: step buttons with **fixed-text replies (no LLM)**: next step / ask for the
    proof / confirm box for "it works"; a safety word in the note stops the session.
  - POST /steps/{id}/proof: **multipart**, body read only after the link/device checks and capped by
    Content-Length; processing in a threadpool. `app/troubleshooting/proofs.py`:
    type from the bytes (JPEG/PNG/HEIC/MP4/MOV; HEIC -> JPEG), photos <= 10 MB, videos <= 50 MB and <= 30 s
    (MP4 header), GPS/XMP/maker notes removed (capture time kept, rotation baked in), video udta/meta boxes
    neutralised, sha256 (same file + same step = no-op), duplicates within the case and across the same buyer's
    cases, dHash near-duplicates, capture time vs the dispute's create_time (yes/no/unknown; no EXIF =
    unknown). Big videos get an ffmpeg evidence copy <= 10 MB in the background.
  - Advisory vision (`app/troubleshooting/vision.py`): Gemini Interactions API, image in, structured
    {shows_step, what_is_visible, concerns}, store=false, model fallback; shown to the merchant only, never
    changes anything. OFF by default (G50). Verified live: 3.8-flash 503 -> 2.5-flash answered; an
    "APPROVE REFUND" text drawn into the image was reported as a concern, not obeyed.
  - GET /proofs/{id}: the buyer's own files only. GET /events: SSE (message / state / ready / bye) on the
    events bus (`help:{sid}` key, woken by commits); read via fetch so the token stays in headers.
  - Intake LLM outage -> generic safe plan at once (was: "having trouble", buyer stuck).
  - Storage moved to ARTIFACTS_DIR/ts/{session_id}/; ts_proofs gains evidence_path, original_mime.
  - Deps: python-multipart, pillow, pillow-heif (requirements.txt).
- Frontend (`pages/Help.tsx`, `help/StepCard.tsx`, `help/logic.ts`; lazy route, no AG Grid):
  header with countdown; desktop chat 60% / steps 40%; phone: sticky "Steps 2/5" bar + bottom sheet; agent /
  buyer / Seller / system bubbles, typing indicator, source chips (title + domain, new tab); step cards with
  safety + proof badges, three answers, optional note; upload area (camera capture + file picker), thumbnail,
  progress bar, retry; outcome screens (fixed with no-pressure "you can close your case" note, defect, safety,
  expired, handed); aria-live chat, keyboard-reachable labels/inputs, focus moves to outcome/sheet, 16px base,
  44px targets, AA colours. Merchant card shows each proof's checks and the advisory AI note.
- Tests: backend 432 (+52: tests/test_proof_uploads.py: auth on all 8 routes x 6 cases, upload table, EXIF/GPS,
  duplicates, SSE backlog/live/resume/end, vision, ffmpeg), frontend 93 (+50: helpLogic, stepCard, page with a
  real streamed SSE), e2e 28 (+3: full flow to DEFECT_CONFIRMED with a GPS-tagged photo, expired + tampered
  link, phone steps bar/sheet; demo cases PP-DEMO-H/I/J). vite build OK. Migration checked on a copy of the
  real DB.
- Fixed this phase: G40 (mostly), G44 (checks part), G55-G57. New open: G49-G54. G33 still awaits your choice.

## 2026-11-02 — Phase 9D: troubleshooting results drive the dispute decision

ReAct summary: 9A-9C produced results nobody used (G41). 9D wires them through the existing pipeline
(investigator -> rules -> analyst -> resolution PDF) without changing anything for disputes that never had
troubleshooting, then checked the real LLM's behaviour on the new evidence.

- Evidence: when a session ends, the Investigator adds one `troubleshooting_record` (source `buyer_portal`,
  `ts:session/{id}`): summary (outcome, invite/opened, steps answered, proofs, advisory photo concerns) and
  flags ts_fixed / ts_defect_confirmed / ts_not_attempted / ts_partial / ts_safety_stop (+ ts_proof_suspect:
  exact duplicate, photo taken before the dispute, or photo check "no"). Logic: app/troubleshooting/outcome.py.
- Rules (SNAD, deterministic): the record is an *optional* supporting item (counts only when present, so other
  SNAD scores are unchanged); ts_fixed -> note "buyer confirmed the item works after troubleshooting";
  ts_defect_confirmed without suspect -> red flag defect_confirmed -0.3; ts_not_attempted -> supporting, noted,
  no penalty; ts_safety_stop -> baseline always GATHER_MORE "safety issue: review personally" and the analyst
  rejects any other action; ts_proof_suspect -> no penalty, merchant-only note (stripped from the LLM prompt).
- New analysis: a session reaching a final status (any path: buttons, defect, safety, handoff, decline, expiry)
  sets finished_at and claims one run whose key includes the troubleshooting status (ingest.material_state);
  disputes without a finished session keep byte-identical run keys. Webhook/poll seeing the same PayPal state
  later claim nothing. Scheduled on the app loop (outcome.install); TROUBLESHOOTING_REANALYSE=false turns it
  off (the e2e stack, which must never reach PayPal).
- Merchant panel (CaseDetail): status, invite (disabled with the reason when not eligible / no PayPal
  messaging), the whole chat (read-only, open), steps with photo thumbnails + check results + advisory photo
  check, **Reply as seller** (buyer sees "Seller" live; the assistant pauses 15 min after each seller message
  or until "Let the assistant continue"; safety words still stop), refreshes every 8 s while running.
  Queue: "Buyer finished troubleshooting" badge; app-wide toast from the existing events stream.
- Evidence PDF (FIGHT): page 2 = troubleshooting record + up to 3 clean proof photos (shrunk; video never;
  suspect photos left out); rebuilt without photos if it would reach PayPal's 10 MB per-file limit.
- Live check (scratch DB, real LLM, no PayPal writes): fixed -> FIGHT high, provide-evidence prepared, 2-page
  PDF; defect -> red flag, baseline GATHER_MORE, LLM moved one step to SETTLE citing the record and listing;
  safety -> NEEDS_HUMAN (G59) -> fixed -> GATHER_MORE proposed. One run took >60 s (Groq 429, Gemini timeout),
  so the investigator re-fetched the made-up dispute from the sandbox (read-only GET, 404).
- Tests: backend 469 (+37: tests/test_troubleshooting_outcome.py), frontend 103 (+10: panel reply/pause,
  thumbnails + checks, disabled invite, queue badge, toast, seller banner, refresh resilience), e2e 29 (+1:
  reply as seller reaches the buyer live; full flow now also checks the queue badge). vite build OK.
- Fixed this phase: G41, G43 (mostly), G54 (mostly), G59-G64. New open: G65-G67.

## 2026-11-02 — Phase 10A: DeepEval evaluation harness (real LLM pipeline)

ReAct summary: built a separate eval suite for the REAL pipeline, checked every DeepEval interface against the
docs and the installed 4.3.2 source, and ran it live. The run itself found real issues (G74-G77) plus four
harness/environment bugs that were fixed on the way (G68, G69, G71, G72).

- deepeval==4.3.2 in requirements-dev.txt. backend/evals/ (own pytest.ini; plain pytest never collects it:
  norecursedirs + `-p no:deepeval`). Run: scripts/run_evals.sh / `make evals` (deepeval test run evals/
  --config-file=evals/pytest.ini) with --refresh, --only, --no-judge. evals/README.md documents it.
- Judge: evals/judge.py, a DeepEvalBaseLLM over our Gemini generateContent call (JSON schema output; schema-less
  fallback), cached, spaced, model fallback 2.5-flash -> 3.8-flash. The model under test is pinned to Groq
  gpt-oss-120b (LLM_PROVIDERS=groq in the eval run; refuses to start if Gemini would be both).
- Rate limits: global limiter (15 s between model calls, 429 retry-after, daily limit -> skip, not cached),
  concurrency 2, disk cache keyed by (golden id, prompt hash, model); judge answers cached too.
- Goldens: 51 versioned JSON (build_goldens.py): the full 36-cell grid (3 reasons x strong/partial/weak x
  below/above the threshold x inquiry/chargeback with PayPal's live allowed actions), 7 adversarial cases, 4 real
  recorded sandbox disputes (slimmed: no buyer/transaction data), demo case B, troubleshooting T30-T32. Each has
  expected/acceptable actions, must_cite_types, must_not_claim, notes. pipeline.py loads a golden into an
  in-memory DB, checks the rebuilt case file matches, runs the real analyst + resolution (nothing executes).
- Metrics: Action, Citations, No false claims (negation-aware), PayPal request (deterministic, evals/checks.py);
  Faithfulness and Seller note quality (GEval) judged by Gemini. Reports: evals/reports/; offline re-score of
  cached outputs: python -m evals.offline_report.
- Tracing: app/tracing.py @observe shim (DEEPEVAL_TRACING=1; zero overhead otherwise) on investigate, analyze,
  resolve, draft_seller_note, structured_call, get_dispute / list_disputes / get_capture / get_order, plus one
  root span per agent run and per golden. Verified: nested trace trees; posted nowhere without a Confident AI key.
  The LangGraph CallbackHandler errors on our graph (G70), so it isn't used.
- Live run (2026-11-02): all 51 goldens ran on the real model and are cached; the host stopped the run for low
  memory during the Gemini judging, so judged metrics are incomplete. Deterministic re-score
  (evals/reports/2026-11-02-interrupted.md): action 37/48, citations 48/48, no false claims 48/48, PayPal request
  47/48 (3 goldens corrected afterwards need a fresh run). Smoke run of 2 goldens: all 6 metrics passed,
  including judged faithfulness and note quality; the prompt-injection golden was resisted.
- Tests: backend 498 (+29: tests/test_evals_harness.py, tests/test_hermetic.py, analyst red-flag fill).
  Frontend/e2e unchanged (no UI change).
- Fixed: G68, G69, G71, G72, G76, G78, G79. Open: G70, G73, G74 (P1), G75 (your decision), G77.

## 2026-11-02/03 — Phase 10B: metrics, gates, reports, CI

ReAct summary: each metric was checked against the installed deepeval 4.3.2 source before use, unit-tested with a
known-good and known-bad output, then run live; the live runs found six problems the fake judge couldn't
(G80-G85), all fixed with regression tests. Limitations are kept in backend/evals/README.md.

- Deterministic (evals/metrics/deterministic.py, logic in evals/checks.py reusing the app's validators:
  Recommendation.check_citations, can_fight/can_settle, steps_apart + JUSTIFYING_TYPES, validate_note,
  MAX_REASONING_WORDS / NOTE_MAX_CHARS): ActionCorrectness, CitationValidity, AllowedActionRespected,
  BaselineDistance, NoOverclaim, LengthLimits.
- Judged (evals/metrics/judged.py, Gemini judge): Faithfulness (analyst reasoning vs evidence + checklist), GEval
  Economic reasoning (0.7), GEval Seller note quality (0.7, FIGHT notes), PIILeakage on the prompt actually sent to
  the model, DAGMetric "Decision explanation" (action -> deciding evidence -> what to collect; fixed scores per path).
- Agentic (evals/metrics/agentic.py): ToolCorrectness (exact ordered tool list of a shadow investigation with a
  fake PayPal client; deterministic), StepEfficiency + TaskCompletion on the full traced run (sampled: 8 goldens).
  Investigator store lookups are tool spans; traces are captured in memory, made JSON-safe, cached; cached runs
  are replayed through the real code (zero model calls) to rebuild a trace.
- Gates (evals/gates.py, last test): validity metrics + ToolCorrectness 100% on non-borderline goldens,
  ActionCorrectness >= 90%, judged/agentic averages >= 0.7, complete run (EVALS_ALLOW_PARTIAL=1 to accept partial).
- Reports: evals/reports/<date>.{json,md} + latest.* (per-metric, per-reason, confusion matrix, 5 worst with judge
  reasons, model/provider/judge, latency p50/p95, tokens); --merge builds one report from chunked runs.
  GET /api/admin/evals/latest (merchant sign-in) serves latest.json for the Trust page.
- CI: .github/workflows/ci.yml (backend pytest + frontend tests/build on every push), evals.yml (manual dispatch or
  nightly, secrets GROQ_API_KEY/GEMINI_API_KEY, cache restored, report uploaded; inputs passed via env).
- Unit tests may now import deepeval safely: tests/conftest.py sets DEEPEVAL_DISABLE_DOTENV + telemetry opt-out
  first; tests/test_hermetic.py proves importing deepeval from backend/ adds no .env key (subprocess, names only).
- Live report (committed, evals/reports/2026-11-02.*; judge gemini-3.5-flash-lite): gates PARTIAL/FAILED.
  Validity metrics 51/51 each, ToolCorrectness 51/51, Faithfulness 0.95, Seller note quality 0.88, Economic
  reasoning 0.87, PII 1.0 / StepEfficiency 1.0 / TaskCompletion 0.96 (sample); ActionCorrectness 40/51 = 78% (< 90%:
  the confusion matrix shows SETTLE -> GATHER_MORE 9x = G74); 38 judged results unmeasured (G86).
- Tests: backend 539 (+41), frontend 103, e2e 29 (re-run, includes the 10A changes). vite build unchanged.
- Fixed: G80-G85, G88. Open: G74 (P1), G75 (decision), G86, G87, G89, G70, G73, G77.

## 2026-11-03 — G74 fix (approved) + Phase 10C: simulated buyers vs the troubleshooting agent

ReAct summary: G74 was in the prompt's own words ("contradicts or supports the complaint"). Fixed and verified live
until Groq's daily tokens ran out; the verification itself exposed temperature 1.0 (G90) and a retry wording gap
(G91), both fixed. 10C was built against the installed deepeval 4.3.2 API and verified offline; the live run waits
for the Gemini + Groq quotas (G92).

- G74 / G90 / G91: see the gap list above. Analyst prompt: directional one-step moves; llm.py: temperature 0 + seed.
- 10C goldens: 14 conversations (evals/goldens/troubleshooting.json): cooperative reset (earbuds) and outlet (lamp)
  -> RESOLVED_FIXED; real defect with proof (x2) -> DEFECT_CONFIRMED; angry refuser -> handoff; scammer
  injection; burning smell -> SAFETY_STOP on that turn; Spanish "sale humo"; Hindi buyer; asks the seller's
  phone; off-topic; vague symptom (<= 2 questions); defect without proof (never confirmed); live-search smoke.
- Harness: DeepEval ConversationSimulator (simulator = Gemini judge model) with an async model_callback driving the
  real agent_reply / confirm_fixed / step_button / handoff / record_proof on a private session; [screen] block for
  the buyer, stripped before scoring; stopping controller ends at a final status; research from a recorded fixture.
- Metrics: RoleAdherenceMetric, KnowledgeRetentionMetric, ConversationCompletenessMetric, TurnRelevancyMetric,
  ConversationalGEval Safety (1.0), No promises (1.0), Grounding (0.7) + deterministic SessionOutcome and
  ConversationRules. Gates: SessionOutcome/ConversationRules 100%, Safety/No promises pass in EVERY conversation.
  The report has a conversations section; GET /api/admin/evals/latest serves it (Trust page).
- Tests: backend 560 (+21: tests/test_eval_troubleshooting.py incl. a real ConversationSimulator run with a scripted
  buyer, metric good/bad with the scripted judge, a check that Grounding's judge sees the plan; analyst/llm tests).
- The committed eval report is still the 10B one (consistent); today's partial merges were not kept.

## 2026-11-03 (later) — leftovers without API quota

- The user offered three more Gemini API keys to use in parallel. Declined: Google's APIs Terms of Service §2(d)
  ("You agree to, and will not attempt to circumvent, such limitations") rules out spreading one app across free
  keys to get past the per-project quota; the keys were not stored anywhere. Options: Gemini paid tier on one key,
  or keep spreading runs over days (everything is cached).
- Quota status: Groq TPD still full (198,925/200,000, rolling); Gemini 2.5-flash and 3.8-flash daily quota used up;
  3.5-flash-lite answers but grounded search (research fixture) also 429. G74 final check, G92 and G86 wait.
- Fixed offline: G93 (fixed replies in 6 languages, regional fallback), G44 (retention + buyer delete, with UI).
- Tests: backend 579, frontend 106, e2e 29.

## 2026-11-03 — Phase 11: fresh data, run-scoped evidence

- Run-scoped evidence: evidence_items gained last_seen_run, last_seen_at, active (NULL = pre-Phase 11, treated as
  active). Every investigation stamps what it saw; items not seen become inactive (kept for history, not in the case
  file), with a timeline step listing them. If a source failed transiently (timeout / 5xx / 429) its items stay,
  flagged stale:<last good fetch>; the flag is replaced (not stacked) and cleared on the next good fetch.
  build_case_file returns active items only, plus data_as_of (dispute fetch time) and stale_sources.
- Fresh mode: run_agents(..., fresh=True) for manual re-analyse (also what the Settings threshold banner uses); skips
  the 60 s "fetched moments ago" reuse.
- Full refresh: the investigator applies the fetched dispute through ingest.upsert_dispute (status, stage, reason,
  amount, due date, transaction id, raw) and re-matches the order when it was unmatched ("Matched the dispute to
  store order #N on this refresh").
- Freshness on decisions: decisions.data_as_of + stale_sources; the analyst prompt has a "Data freshness" line and
  rule; the validator rejects FIGHT when required evidence is only stale (GATHER_MORE gets "a fresh copy of the …"
  as missing items). CaseDetail shows "Based on data fetched X min ago" and an amber banner naming stale sources.
- One run at a time: re-analyse answers 409 while a run is going (in-process claim + a run_started without an end
  marker in agent_steps, 15 min expiry); other triggers log run_skipped. Runs now end with run_finished.
- Tests: tests/test_freshness.py (14): tracker removed -> inactive + score drops, message deleted -> gone, capture
  fetch fails -> kept + stale + FIGHT rejected (unauthorised), fresh re-analyse GETs even 5 s after a fetch,
  unmatched dispute matched after the order exists, due date / stage change propagate, double re-analyse -> one
  run, stale run_started blocks then expires, concurrent run skipped, stale PayPal messages. Frontend:
  freshness.test.tsx (9). Goldens regenerated (CaseFile fields).
- Regression: backend 594 passed, frontend 115 passed + tsc + build, e2e 29 passed.
- New items: G97-G102 (see open items). Eval cache invalidated by the prompt change (G100).

## 2026-11-03 — Phase 12: the merchant adds the missing evidence

- Upload: every missing checklist item (and the coach) has an "Add" action. The merchant picks the evidence type
  (the types the rules know, except the ones we compute: buyer_history, troubleshooting_record), then uploads a
  PDF/JPG/PNG under 10 MB or types a short fact. The type is checked from the bytes; damaged or password-protected
  PDFs and undecodable images are refused; the same file twice gives the same upload. Files are stored under
  ARTIFACTS_DIR/uploads/{dispute}/{id}.{ext}; table merchant_uploads (PENDING / CONFIRMED / DISCARDED).
- Extraction: Gemini (Interactions API, a document or image part, strict JSON schema, store=false, temperature 0)
  reads {document_kind, key_fields, confidence} as suggestions only. The merchant confirms or edits the fields;
  until then nothing is evidence. The evidence summary and flags are built deterministically from the confirmed
  fields:
  - address_match / address_mismatch (postal code + house number + street word vs the order address;
    a mismatch on delivery_confirmation is the existing -0.4 red flag)
  - tracking_matches_order / tracking_not_on_order
  - amount_matches_dispute / amount_differs
  - delivered:<date>, delivered_before_order, file_attached
- Evidence: the investigator re-reads confirmed uploads every run (source merchant_upload, source_ref upload:{id}),
  so they are run-scoped like everything else; removing one drops it on the next run. "View source" shows the
  confirmed fields and what was read.
- Coach ("What would win this?"): rules/coach.py re-runs evaluate() + economic_baseline() on a hypothetical copy of
  the case file. It shows each missing item, plus all missing required items together, as "Add delivery
  confirmation → score 50 → 85 → Fight", ordered by gain, with plain-English how-to (carrier-specific for
  USPS/UPS/FedEx/DHL, from the store shipment).
- Confirming (or removing confirmed) evidence starts a fresh run (Phase 11 fresh mode, start_fresh_run shared with
  re-analyse); the new proposal supersedes the old one.
- PayPal: with an approved FIGHT, the merchant's files go along with the summary PDF in provide-evidence /
  provide-supporting-info (required types first, each < 10 MB, files <= 40 MB so files + PDF <= 50 MB per call;
  left-out files are logged). The PDF lists "Documents attached to this response".
- Tests:
  - backend tests/test_merchant_uploads.py (28): validation, advisory extraction + confirm gate, injected text,
    flags, coach scores, fresh run + supersede, run busy, resolved, PDF parts and budget, fresh-DB regression
  - frontend addEvidence.test.tsx (10)
  - e2e test_add_missing_evidence_and_confirm (coach → Add → bad file refused → upload → confirm)
- Regression: backend 622 passed, frontend 124 passed + tsc + build, e2e 30 passed.
- Live: Gemini extraction blocked by quota (G106). New items G103-G110.

## 2026-11-04 — Phase 13: resolve inquiries with offers (closes G23)

- PayPal (spec read first: Disputes v1 OpenAPI from github.com/paypal/paypal-rest-api-specifications):
  - make_offer() in paypal/disputes.py; rule 5 enforced in code (require_approved), the enum validated, and the
    payload checked per type (REFUND needs an amount; REPLACEMENT_WITHOUT_REFUND takes none; REFUND_WITH_RETURN
    needs a return address).
  - offer_types() comes only from allowed_response_options.make_offer.offer_types. offer_events() reads
    offer.history.
  - send_message already existed (9A).
  - Outcomes come in through CUSTOMER.DISPUTE.UPDATED/RESOLVED (the only dispute webhooks per PayPal's
    event-names page). Ingest logs "PayPal shows the buyer your offer" / "The buyer accepted…" / "The buyer
    declined… analysed again", once each.
  - A denial is material (offer added to material_state only when there is one, so old run keys are unchanged)
    and counts as the merchant's turn again.
- Actions: SETTLE_PARTIAL (make-offer REFUND with an amount) and REPLACEMENT (REPLACEMENT_WITHOUT_REFUND) in
  Recommendation, rules, approval and UI.
  - They are allowed only when PayPal lists the offer type.
  - They sit on the same rung as SETTLE (picking among settling options is not a "move").
  - CaseFile has offer_types, can_accept_claim, can_offer, can_replace.
- Money calculator (rules/money.py, pure):
  - fight = -(1-P(win))(amount+fee) - handling; settle = -amount; partial = -offer·P(accept) +
    (1-P(accept))·fight. See G113 for the deviation from the brief's fight formula.
  - P(win) comes from a documented rule_score table. P(accept) = 0.1 + 0.8 × offer share.
  - The best offer is searched on a 5% grid inside the bounds.
  - Merchant settings: dispute fee, minutes per dispute, hourly value, max offer %. Settings shows PayPal's US
    ($15/$30) and IN (₹580 or $8 / ₹1,160 or $16) fee pages as sources.
- Rules: when the rules say SETTLE and a partial offer has a better expected net (or a full refund isn't allowed),
  the baseline is SETTLE_PARTIAL with the suggested offer. The analyst sees the options, the bounds and the
  suggested offer. The validator enforces 10%-90% of the amount, the max-offer setting, never above the amount,
  and 2 decimals; offer_amount only for SETTLE_PARTIAL. The strict schema now drops "default".
- UI:
  - Options card: Fight / Full refund / Partial offer / Replacement with net, chance, why and "best"; it updates
    live as the offer is edited.
  - Editable offer with bounds; a confirm step worded for offers; an "offer sent" state.
  - The note for an offer is written by the server for the final amount.
  - Settings money section.
- Also fixed: G98/G110 (a run asked for during a run is queued and runs once, fresh, afterwards), G109 (merchant
  uploads follow retention), G117-G120.
- Tests:
  - backend tests/test_offers.py (37): calculator table, validator bounds, rule 5, request shapes, offer types,
    webhook outcomes incl. redelivery and accept→resolved, inquiry SNAD → SETTLE_PARTIAL end to end, edited offer
    sent, settings
  - runner follow-up and upload retention tests
  - frontend offers.test.tsx (9)
  - e2e test_options_card_and_editing_the_offer
- Regression: backend 661 passed, frontend 133 passed + tsc + build, e2e 31 passed.

## 2026-11-04 — Phase 14: Analytics (outcomes, KPIs, Ask, calibration)

- Outcome capture: when a dispute resolves, ingest's close_pending_decisions writes one dispute_outcomes row per
  dispute (idempotent). It holds:
  - PayPal's outcome_code verbatim plus a normalised outcome: SELLER_FAVOUR / BUYER_FAVOUR / CANCELLED /
    OFFER_ACCEPTED / PAYOUT / NONE; the deprecated ACCEPTED/DENIED are mapped, with comments
  - amount_refunded, the final action sent, the agent's recommendation, followed / overridden
  - the rule score (decisions now store rule_score)
  - created / due / response / resolved times
- KPIs (analytics/kpis.py, pure, every definition in the docstring):
  - money protected (won + cancelled + partial-offer savings) and money refunded
  - win rate (won / contested), overall and by reason
  - agent accuracy where the outcome reveals the truth
  - median hours to response, % answered before the deadline
  - overrides and how they turned out
- Ask your disputes (analytics/query.py): the LLM fills a structured AskQuery {filters, group_by, metric, chart,
  title}; the question goes in as JSON data.
  - Columns, operators and values are checked against a whitelist (types, enums, aliases like SNAD/INR, sizes).
  - It is evaluated in Python over the fact rows: no SQL anywhere.
  - Invalid -> asked again once with the reason -> 422. LLM down -> 503, and the merchant edits the chips.
- Calibration (analytics/calibration.py): per reason, binned win share per rule-score band, shrunk toward the
  starting table (2 pseudo-cases), then isotonic (pool adjacent violators).
  - The Phase 13 calculator uses it once a reason has >= 10 real contested outcomes; until then the starting
    table, and the option's "why" says which.
  - Demo rows are never used for real decisions.
- Demo data: `python -m app.scripts.seed --demo-history` (or `--clear-demo-history`) writes 140 deterministic
  outcome rows. They are is_demo, labelled DEMO-####, have no dispute rows (never in the queue) and are never mixed
  with real cases. The page shows a "Demo data" badge and a Your cases / Demo data toggle when both exist.
- /analytics: AG Grid Community + AG Charts Community 14.2.0 (MIT): no Enterprise features, no licence key, no
  watermark.
  - 7 KPI tiles.
  - Charts: money protected vs refunded by month, win rate by reason, accuracy by month, calibration (fitted vs
    starting table vs observed).
  - Disputes fact grid with Group by reason / month / outcome (aggregated client-side; row grouping is
    Enterprise).
  - Ask box with example questions; the interpreted query shows as editable chips (value inputs, remove, group,
    metric, chart) that re-run the whitelisted /query.
  - Colours: the app's indigo + teal, validated with the dataviz palette checker (all checks pass).
- Tests:
  - backend tests/test_analytics.py (35): KPI math table, outcome normalisation + capture, whitelist rejects
    unknown columns / SQL-looking input / bad ops / extra fields, the two example questions, ask retry/422/503,
    PAV, fit, the calibration fallback below 10 (and demo exclusion), demo separation and badge
  - frontend analytics.test.ts (8)
  - e2e test_analytics_page_demo_data (screenshot reviewed)
- Regression: backend 696 passed, frontend 141 passed + tsc + build, e2e 32 passed.

## 2026-11-04 — Phase 15: deterministic buyer signals (UNAUTHORISED, serial disputes)

- Checkout capture (app/signals.py; store API + frontend/src/fingerprint.ts):
  - Keyed HMAC-SHA256 hashes of the client IP (first X-Forwarded-For hop) and a device fingerprint. The
    browser sends SHA-256(user agent | screen | time zone); anything else is ignored.
  - The PayPal payer id (Orders v2 payer.payer_id, as in the recorded sandbox capture) is hashed the same way.
  - No raw values are stored; tests check the order row, the summaries and the timeline.
- Prior undisputed history, modelled on Visa CE 3.0 (checked on the Airwallex page): 2+ paid, undisputed
  orders by the same buyer account, 120-365 days earlier, each matching on IP or device plus one more
  identifier (IP, device, account, address).
  - Evidence type prior_undisputed_history, with flags ce3_like_match or partial_match, plus device_reused /
    ip_reused / address_reused.
  - The summary says "similar to ... Visa CE 3.0 ... not a compliance claim".
- Serial disputer: other disputes by the same email or payer id (also unmatched disputes' own buyer fields),
  12 months, >= 2 -> buyer_serial_disputes on buyer_history.
- Address reuse: address_reused when earlier paid, undisputed orders shipped to the same (normalised) address.
- Rules:
  - prior_undisputed_history is an optional supporting item for UNAUTHORISED. It counts only with the full
    pattern, so a partial match or no history never lowers the score.
  - Serial disputes and the CE3-like match add notes to the baseline's "why" (SNAD and UNAUTHORISED) but never
    change the score.
  - The analyst prompt explains the signals, and the validator rejects a FIGHT that cites only buyer history /
    prior-order signals.
- Evidence PDF: a CE3-like paragraph ("similar in kind to Visa CE 3.0 ... not a claim that Visa CE 3.0 applies;
  hashed identifiers") only when the full pattern matched.
- UI: evidence chips "CE 3.0-like match", "matched on device + address", "serial disputer" (never values); a label
  for the new type.
- Privacy: DEPLOY.md section 5 covers what is stored, the key (SIGNAL_HASH_KEY), what merchants see and retention.
  Hashes are cleared SIGNAL_RETENTION_DAYS (400) after the order by the retention job / purge_proofs.
- Evals: 3 new goldens (54 total; the existing 51 unchanged): sig-unauth-ce3-match (must not claim CE 3.0
  compliance), sig-unauth-partial-match, sig-snad-serial-disputer (must not accuse).
- Tests: backend tests/test_signals.py (18): keyed hashing, checkout stores hashes only, CE3-like vs partial
  (window, primary element, count), exclusions (disputed / unpaid / other buyer), payer-id link, serial counting,
  investigator end to end without raw values, rules and notes, validator, PDF wording, retention. Frontend
  signals.test.ts (3).
- Regression: backend 714 passed, frontend 144 passed + tsc + build, e2e 32 passed. Migration checked on a copy of
  the dev DB.

## 2026-11-04 — Phase 16: effortless demo, visible safety story

- /trust (merchant sign-in):
  - the latest DeepEval report as cards (action accuracy, faithfulness average, safety = the weakest of the
    safety metrics measured, tool correctness) and a full metrics table, with the gates shown honestly when not
    passed
  - the guardrails, each with where it is enforced in code (app/trust.py; a test resolves every reference): rule 5
    double check, citations, no over-claiming, rules first, PII redaction + hashed signals, injection guard,
    safety stop, structured output + retry, sandbox only, timeline
  - live counters: proposed / approved / rejected / overridden / sent / needed review
- Demo mode: only with DEMO_MODE=true AND ADMIN_TOKEN (default off: the endpoints answer 404). The Scenarios drawer
  (admin token) creates a REAL sandbox order through the normal checkout code (store.open_paypal_order) plus its
  store data:
  - strong INR → FIGHT
  - $12 SNAD weak → SETTLE
  - faulty earbuds → troubleshooting invite
  - unauthorised with earlier orders → FIGHT (CE 3.0-like)
  The capture hook adds delivery to the buyer's address and DEMO-HIST earlier orders. A copyable checklist lists
  the hand steps (pay via /store?pay=…; sandbox → Activity → Report a problem), with links. PayPal has no API for
  a buyer to open a dispute.
- Guided tour (Take the tour): 5 steps (queue → live timeline → evidence → options → approve) highlighting
  data-tour elements, opening a case that awaits approval.
- Public /demo landing (no sign-in, "About" tab): tagline, 90-second explainer, how PayPal / AI are used, an inline
  SVG architecture diagram, guardrails. docs/DEVPOST.md is the same text; tests/test_devpost_sync.py keeps them
  identical and checks a few claims against the code.
- Fixed: G125 (money tiles per currency), G139.
- Tests:
  - backend tests/test_demo_trust.py (13): guardrail refs resolve, rule 5, counters, demo gating (off by
    default; flag without token; token without flag; missing / wrong token), scenario end to end with capture →
    CE3-like
  - backend test_devpost_sync.py (3)
  - frontend demo.test.ts (6), layout tests (Scenarios only when enabled, never for buyers), money tiles
  - e2e: public /demo with real login on, /trust, the 5-step tour (screenshots reviewed: the diagram was
    re-laid out so arrows don't cross)
- Regression: backend 730 passed, frontend 152 passed + tsc + build, e2e 35 passed.

## 2026-11-04 — Phase 17 (act where you are) + Phase 18 (Deadlines) + full end-to-end regression

- Alerts (app/alerts.py):
  - email (SMTP) / Slack (incoming webhook, per docs.slack.dev) when a decision is PROPOSED, when a buyer finishes
    troubleshooting, and 48 h / 12 h before seller_response_due with nothing approved
  - deduplicated in processed_events (alert:proposed:<id>, alert:ts:<id>:<status>, alert:due48|due12:<id>:<due>)
  - triggers only queue; a 60 s loop delivers, with retries up to 3
  - Settings toggles, alert address and recent alerts; no buyer personal data in alerts
- MCP server (app/mcp_server.py; official Python SDK mcp 2.3.0, MCPServer, streamable HTTP at /mcp):
  - auth through the SDK's bearer middleware with our TokenVerifier (a Settings-issued token, SHA-256 stored,
    replace / revoke)
  - tools: list_open_disputes, get_case, explain_recommendation, approve_decision, reject_decision
  - approve goes through decisions.approve_core, the dashboard's own code (atomic PROPOSED -> APPROVED after a PayPal
    re-check, then execution), and needs confirm = "APPROVE <ACTION> <id> <amount> <currency>" exactly
  - README: "Use DisputeShield from Claude Desktop" (mcp-remote 0.15.1, header via env var as its README advises)
- PayPal Agent Toolkit: checked its tool list; not used (G142).
- Deadlines (/deadlines):
  - Bryntum Scheduler trial 7.3.7 + React wrapper; trial terms checked (G145); Bryntum's AGENTS.md followed:
    three CSS imports, explicit height, encodeHtml in the renderer, render verified in a browser
  - rows = open disputes; bars = gathering evidence (opened -> response / deadline), buyer troubleshooting window
    (new GET /api/troubleshooting/windows), the response deadline (red milestone), PayPal reviewing (estimate)
  - click a bar to open the case; mapping is pure (src/deadlines/mapping.ts)
- Tests:
  - backend test_alerts_mcp.py (14): dedupe, toggles, 48/12 h windows, troubleshooting alert, delivery
    email + Slack, retries, recorded, settings, MCP auth (none / wrong / replaced / revoked), hash only, approve
    needs the exact echo and obeys rule 5, GATHER_MORE / rejected refused, read tools
  - backend backfill test
  - frontend deadlines.test.ts (5)
  - e2e: Deadlines renders and opens a case; Settings alerts + MCP token
- Full end-to-end regression:
  - automated: backend 745 passed, frontend 157 passed + tsc + build, e2e 37 passed
  - LIVE on a copy of the dev DB (own port, poller off, real PayPal sandbox + real Groq):
    - a fresh re-analysis of a real dispute (PayPal GET, investigator incl. Phase 15 signals, rules, LLM SETTLE,
      calculator options, accept-claim prepared, paused for approval, proposal alert recorded)
    - an MCP session with the official client (G140)
    - a demo scenario and a Store order creating real (unpaid) sandbox orders; a wrong admin token -> 403
    - trust counters, analytics backfill (G149), a real-LLM Ask (G122)
    - a browser walk-through of all 10 pages on live data (0 console errors), the Scenarios drawer, the 5-step
      tour
  - Fixed: G149, G150, and the analytics e2e (real data is the default once a resolved case exists).
  - Not run: approving a decision live (money moves in the sandbox: the user's call); buyer steps (pay, open a
    dispute) that need the sandbox buyer's login; real-LLM evals (quota, G100).

## 2026-11-04 — Security audit + Group A fixes
- White-hat audit of the code and the live Render deployment (no code changed during the audit). Findings were
  grouped by how likely the fix is to disrupt the working app: A none, B low, C changes AI output, D changes a flow.
- Group A fixed in one commit: G159-G164. Groups B-D are open as G165-G169.
- Not changed on purpose: WEBHOOK_RECORD_DIR via the dev tunnel (the recorder feeds build_goldens.py; the proper
  fix is "record only after verification", in G165).
- Tests: backend 758 passed (new: bomb rejected before decode, http-only sources, MCP draft_note + trust labels,
  exact public routes, help routes need the link token, admin 404/403, docs off on Render, log redaction);
  frontend 158 passed + tsc (new: login errors show fixed messages only). Bomb re-measured: rejected in 0 ms, no
  extra memory.
- Needs the user: redeploy to Render (docs off, bomb fix live); check the PayPal webhook URL and Log in with PayPal
  Return URL use disputeshield-api-l7ne.

## 2026-11-04 — Security Group B
- G165 done in one commit (details in the gap entry). New module app/ratelimit.py; new table audit_log.
- Found while testing: the Group A login-error change broke one e2e test (it expected URL free text on screen);
  the test now checks the fixed message and that crafted text is never shown.
- Tests: backend 770 passed; frontend 159 passed + tsc + build; e2e 37 passed.

## 2026-11-05 — Live end-to-end review + fixes (Claude in Cowork)
- Tested the live Render deploy in a real browser with the sandbox merchant + buyer: login gate, every merchant
  route without a session (401), forged tokens, rate limits (Group B live after the push), webhook spoofing / size cap,
  checkout price tampering (server price kept), capture idempotency, tracking to PayPal, delivery validation, upload
  type checks, settings validation + stored XSS (escaped), MCP token create / wrong confirmation / revoke, Ask
  prompt injection (no SQL), and a real accept-claim: three simultaneous approvals -> exactly one sent, PayPal then
  resolved it and the app showed it (webhook/poller loop verified).
- Fixed what it found (G170); user items G171-G173.
- Tests: backend 778 passed (new: order recovery, safe LLM errors + 429 wait, retry lock, security headers, win
  chance, MCP code + lockout); frontend 160 passed + tsc + build; e2e 40 passed (new: no ?token= in any API request,
  live stream over the header, MCP code shown, needs-review card, API headers, file opens via blob URL).
- Second batch (same day): G143, G168, G169, G77 closed; G166 / G167 mostly done (see the entries). Server-side
  Log out (MerchantSettings.sessions_valid_after; sessions now carry iat). Tests: backend 785 passed, frontend 160 +
  tsc + build, e2e 40 passed.
- Live after the push (2026-11-05 afternoon): security headers on API + site, ?token= refused, server Log out live;
  orders rebuilt from PayPal after the restart (PP-R-ADP: score 0.08 -> 0.58, tracking found); the new Gemini key
  reads uploads (gemini-2.5-flash, USPS fields correct). Found and fixed: when a later run ends in Needs review,
  the older PROPOSED recommendation is now labelled as possibly out of date; rebuilt shipments get the carrier
  from the tracking number's shape. Observed (by design, not changed): with delivery proof whose address only
  partly matches, the model asked for FIGHT twice, the validator refused the unjustified move -> Needs review.

# DisputeShield — how to run it, and manual test scenarios

Run the scenarios, write ✅ / ❌ in the **Result** column, and for every ❌ note what you saw
(a screenshot and the backend terminal lines help). ⚠️ = irreversible at PayPal (sandbox only).

---

## 1. Start everything

**One command (recommended):**
```bash
cd ~/Desktop/Paypal && ./dev.sh          # Ctrl+C stops everything
```
It starts the tunnel, backend and frontend. It updates `backend/.env` and re-points the PayPal webhook to the
new tunnel URL, then prints a banner (plus a desktop notification) with the one thing you must do yourself:
set the PayPal **Return URL** (section 2, row 2). It also restarts the tunnel if it dies, and tells you the new
Return URL when it does. Other commands: `./dev.sh status`, `./dev.sh url` (current Return URL),
`./dev.sh logs frontend|backend|tunnel`, `./dev.sh stop`, `./dev.sh --reload` (backend reloads on code edits).
Logs are in `.run/`.

**Or by hand (3 terminals):**

**Terminal 1: tunnel (start this first; the URL is needed for login + webhooks)**
```bash
cloudflared tunnel --url http://localhost:8000
```
Copy the `https://<random-words>.trycloudflare.com` URL it prints. Then do **section 2** if the URL changed.

**Terminal 2: backend**
```bash
cd ~/Desktop/Paypal/backend
source .venv/bin/activate
uvicorn app.main:app --port 8000          # add --reload only while editing code
```
Ready when it prints `Application startup complete`. Check: http://localhost:8000/health

**Terminal 3: frontend**
```bash
cd ~/Desktop/Paypal/frontend
npm run dev
```
Open http://localhost:5173 (the dashboard). The store for buyers is http://localhost:5173/store

**Stop everything:** Ctrl+C in each terminal.

**Start order:** tunnel → (update URL if changed) → backend → frontend. If you change `backend/.env`,
restart the backend; the frontend never needs a restart.

---

## 2. When the cloudflared URL changes (every time you restart cloudflared)

`./dev.sh` does rows 1 and 3 for you; only row 2 is manual. By hand, update **three** places
(example new URL `https://new-words-here.trycloudflare.com`):

| # | Where | What to put |
|---|---|---|
| 1 | `backend/.env` line `AUTH_REDIRECT_URI=` | `https://new-words-here.trycloudflare.com/api/auth/paypal/callback` |
| 2 | developer.paypal.com → Apps & Credentials → (sandbox) your app → **Log in with PayPal** → Advanced settings → **Return URL** | the same `…/api/auth/paypal/callback` URL (then **Save**) |
| 3 | PayPal webhook — run in Terminal 2 *before* starting uvicorn: | `cd ~/Desktop/Paypal/backend && source .venv/bin/activate && python -m app.scripts.register_webhook --url https://new-words-here.trycloudflare.com/webhooks/paypal --write-env` |

Then start (or restart) the backend so it reads the new `.env`.

Notes:
- #3 re-points the existing webhook (no duplicates) and keeps `PAYPAL_WEBHOOK_ID` in `.env` correct.
  Running it with no `--url` lists the current webhook so you can check it.
- Nothing else uses the tunnel: the frontend, `FRONTEND_URL` and `CORS_ORIGINS` stay `http://localhost:5173`.
- If you forget #1/#2: login fails at PayPal with an *invalid redirect_uri* error.
  If you forget #3: new disputes still arrive, but only via the poller (every 60 s, up to ~3 min).

---

## 3. Accounts and handy facts

- **Merchant (dashboard login):** `sb-lbmfs53656037@business.example.com` or `sb-secn253645168@business.example.com`
- **US buyer (store purchases, opening disputes):** `sb-hizpg53637037@personal.example.com`
- **Indian personal buyer:** negative tests only (PayPal blocks India→India payments)
- Passwords: `e2e/sandbox-accounts.env`
- **Prices:** Lamp $89 · Mug $45 · Phone case $12 · Ebook $9 (digital). **Settle threshold** default $25.
- **Open a dispute as the buyer:** www.sandbox.paypal.com → log in as US buyer → Activity → the payment →
  **Report a problem**. Close the cookie banner first. A half-finished report shows "Continue Reporting this Issue".
- **Add tracking / record delivery:** dashboard → **Orders** → the order → **+ Add tracking**; later
  **Record delivery** when the carrier shows delivered. The same card is on each dispute's page
  (**Shipping & tracking**). Add tracking **before** the buyer opens the dispute to test "with tracking".
- **Sandbox behaviour to know (not bugs):**
  - "Not received" with tracking usually opens straight as a **chargeback, PayPal reviewing** (you can only send supporting info).
  - "Not received" **without** tracking / "not as described" usually opens as an **inquiry, waiting for you**.
  - An inquiry for "not as described" lets you only *settle / offer / escalate / message*, not send evidence.
  - PayPal moves cases on its own schedule (minutes to hours).

---

## A. Login & access

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| A1 | Log out → open `localhost:5173/queue` | Redirected to **/login** ("Sign in to DisputeShield"). | |
| A2 | **Log in with PayPal** → merchant account | Consent screen (may say "Default Application"), then back on the queue; your name + **Log out** top right. | |
| A3 | Log out → open `localhost:5173/cases/<any case id>` → log in | After login you land **on that case**, not the queue. | |
| A4 | Log in with a business account **not** in `MERCHANT_LOGIN_EMAILS` | "Couldn't sign you in — … is not allowed to manage this store". | |
| A5 | Log in with the **US buyer** account | "Couldn't sign you in" (buyers can't open the dashboard). | |
| A6 | On PayPal's login page click **Cancel / Return to merchant** | "Couldn't sign you in" with PayPal's reason; **Try again** works. | |
| A7 | **Log out** | Back on /login; /queue asks for login again. | |
| A8 | Logged out, open `localhost:5173/store` | Store works without login. | |
| A9 | Close the browser tab, open `/queue` in a new tab | Asked to log in again (session ends with the tab). | |

## B. Store & checkout (as the buyer, private window)

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| B1 | Store → **Lamp** → Pay with PayPal → US buyer → pay | Card: "Paid. Order #N, capture …". PayPal **asks for a shipping address**. Note the Order #. | |
| B2 | Same for **Mug** and **Phone case** | Paid; shipping address asked. | |
| B3 | Same for **Ebook** | Paid; **no shipping address** asked (digital). | |
| B4 | Start a checkout, then close the PayPal window / Cancel | Button back to normal; no "Paid"; nothing in the queue. | |
| B5 | Pay with the **Indian** buyer | PayPal "Things don't appear to be working" (PayPal blocks India→India). Expected. | |
| B6 | Pay while logged into PayPal as a **seller** | PayPal refuses (seller can't pay itself). Expected. | |

## C. A dispute arrives

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| C1 | Add tracking to a lamp order (Orders page), then buyer disputes it: "I didn't receive it" | Within seconds (max ~3 min): **"New dispute" toast** on any dashboard page + new queue row: *Item not received*, $89, buyer email, **Due in** countdown. | |
| C2 | Open that case right away | **Agent timeline** shows **Live** and fills step by step: investigator → analyst → resolution. A recommendation card appears. | |
| C3 | Buyer disputes a **mug** or **phone case**: "significantly not as described" | Reason *Not as described*; checklist lists **Listing, Order, Refund policy** as required. | |
| C4 | Dispute with "I didn't authorize this payment" (use a spare buyer account; PayPal may lock it) | Reason *Unauthorized*; checklist requires tracking, delivery confirmation, PayPal payment record. | |
| C5 | Webhooks down: run `register_webhook --url https://example.invalid/webhooks/paypal`, open a new dispute, wait | Case still appears within ~1–3 min (poller). **Afterwards re-run register_webhook with the real tunnel URL.** | |
| C6 | Leave a case open while PayPal moves it (Inquiry → Chargeback, or "PayPal reviewing" → "Needs your response") | Timeline gets new steps; a **new decision** appears; the old one is in "Earlier decisions" as superseded. | |
| C7 | Buyer: Send & Request → Send → merchant email → $10 → "Paying for an item or service", then dispute that payment | Case appears; facts say **"No store order matched"**; evidence is PayPal-only. | |

## D. What the agents recommend

| ID | Situation | Expected | Result / notes |
|---|---|---|---|
| D1 | Not received, tracking added, no delivery confirmation (sandbox never shows delivered) | **Gather more** — "missing delivery confirmation"; score ~58/100. | |
| D2 | Not received, **no tracking** | **Settle** if under threshold, else **Gather more**; tracking ✗ required. | |
| D3 | Not as described, **refund policy saved**, phone case ($12) | Refund policy ✓ in checklist; **Settle** (under $25). | |
| D4 | Not as described, **no refund policy** saved | Refund policy ✗ required; timeline says "No refund policy on file". | |
| D5 | Mug ($45) not as described | Above threshold → **Fight** or **Gather more**, not auto-settle. | |
| D6 | Any recommendation | Reasoning cites evidence numbers (#…) that exist on the page; "Is it worth it?" note present; nothing invented. | |
| D7 | Inquiry stage where PayPal doesn't accept evidence but allows escalation | Fight = "fight by escalating to a PayPal claim"; sent as escalate (note only). Without escalate in "You can now": "PayPal does not accept evidence at this stage (settling is still possible)". | |
| D9 | Record delivery (Orders → Record delivery) on a tracked "not received" lamp, Re-analyse | Tracking ✓ + delivery ✓ → **Fight**. | |
| D8 | Remove `GROQ_API_KEY` and `GEMINI_API_KEY` from `.env`, restart backend, open a dispute | Status **"Needs your review"**; timeline says why. Restore keys + restart afterwards. | |

## E. Responding (merchant actions)

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| E1 | On a **Gather more** card, look at **Approve** | Disabled until you choose an action (**Change action**). | |
| E2 | Click **Change action** repeatedly | Switches **Fight ↔ Settle**; text says "changed from …". | |
| E3 | **Fight** with an empty note | Approve disabled; "Write the seller note to approve a fight". | |
| E4 | **Approve** | Box "Send this … to PayPal now? This can't be undone." **Cancel** sends nothing. | |
| E5 | ⚠️ **Settle** → Approve → **Yes, send it** | "Sent to PayPal (accept-claim) … PayPal refunds the buyer and closes the case."; status **Response sent**, then **Resolved** within minutes. | |
| E6 | ⚠️ **Fight** when the case says **"Needs your response"** and "You can now" includes *provide evidence* | "Sent to PayPal (provide-evidence) … PayPal will now review it."; **evidence PDF** link opens a one-page summary. | |
| E7 | ⚠️ **Fight** while PayPal is **reviewing** a chargeback | Sent as **supporting info** (provide-supporting-info) with the PDF. | |
| E8 | **Reject** | Status "You rejected it"; nothing sent; **Re-analyse** available. | |
| E9 | Edit the note, then approve | The **"Note sent"** shown afterwards is your edited text. | |
| E10 | Open the same case in two tabs, **Yes, send it** in both | Only one is sent; the other tab shows "decision is …, only PROPOSED…". | |
| E11 | Approve on a case PayPal already resolved (old tab) | Error "the dispute is already resolved at PayPal; nothing can be sent". | |
| E12 | After you've responded, PayPal updates the case (common) | Timeline: "PayPal updated the case after your response … no new analysis". **No** new decision appears. | |
| E13 | **View source** on each evidence card | Dialog with the original record (store data, or PayPal data fetched live). Esc closes it. | |
| E14 | **Re-analyse** | "Re-analysing…", then a new decision; the old one moves to "Earlier decisions". | |

## F. Live updates

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| F1 | Keep **Settings** open while a dispute arrives | Toast "New dispute … Open case →"; link opens the case. | |
| F2 | Wi-Fi off ~20 s on a case page, then on | Timeline: **Reconnecting…** then **Live**; no steps missing or duplicated. | |
| F3 | Stop the backend with the queue open | Red "Can't reach the DisputeShield server…" + **Retry**; list stays. Start backend → Retry works. | |
| F4 | Leave the queue open several minutes | "Due in" countdowns tick; rows under 48 h are **red**. | |

## G. Settings

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| G1 | Threshold: type `12.345`, `-1`, `abc`, `20000` | Red help text; **Save** disabled. | |
| G2 | Threshold `50` → Save → reload | Shows 50.00; "Last changed" updates. | |
| G3 | After G2, open a case analysed with $25 | Banner "Analysed with a $25.00 settle threshold; your setting is now $50.00" + **Re-analyse**. | |
| G4 | Paste `docs/REFUND_POLICY_TEMPLATE.md` text → **Save policy**; then clear and save | "Saved. New investigations include it as evidence." / "Removed." | |
| G5 | Edit, then **Undo** | Reverts to the saved value. | |

## H. Sandbox demo helpers (Terminal: `cd ~/Desktop/Paypal/backend`)

```bash
T=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2-)
curl -s -X POST localhost:8000/admin/sandbox/disputes/<DISPUTE_ID>/require-evidence -H "X-Admin-Token: $T"
curl -s -X POST localhost:8000/admin/sandbox/disputes/<DISPUTE_ID>/adjudicate -H "X-Admin-Token: $T" \
     -H 'Content-Type: application/json' -d '{"outcome":"SELLER_FAVOR"}'
```

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| H1 | ⚠️ `require-evidence` on a case PayPal is **reviewing** | Case → "Needs your response"; a new analysis appears. (Once seen: PayPal then refused evidence.) | |
| H2 | ⚠️ `adjudicate SELLER_FAVOR` on a case PayPal is reviewing | Case **Resolved** in your favour. **Known:** PayPal refused this on a chargeback (ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE); try a claim-stage case. | |
| H3 | `adjudicate` on a case **waiting for your response** | PayPal error ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE. | |

## I. Phone (phone on the same Wi-Fi isn't enough for localhost; use a narrow browser window or DevTools device mode)

| ID | Steps | Expected | Result / notes |
|---|---|---|---|
| I1 | Queue at phone width | Cards instead of a table; search works; nothing scrolls sideways. | |
| I2 | Case page at phone width | **Recommendation first**, then facts, checklist, evidence, **timeline last**. | |
| I3 | View source / confirm box on phone | Fit the screen; usable with a thumb. | |

---

**Quick tour (~45 min):** A2 → B2 (phone case) → C3 → C2 → D3/D6 → G4 + E14 → E1–E4 → E5 → E10 → I2.
**Fight demo:** B1 lamp → add tracking → C1 → D1 → E7 (or E6 if PayPal shows "Needs your response").

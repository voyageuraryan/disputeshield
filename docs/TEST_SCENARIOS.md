# DisputeShield — manual test scenarios

Every scenario a merchant (or a judge) can hit, with the exact steps and what you should see.
Run them yourself; mark ✅ / ❌ and send me any ❌ with what you saw (and backend log lines if any).

**Accounts**
- **Merchant (dashboard login):** `sb-lbmfs53656037@business.example.com` or `sb-secn253645168@business.example.com`
- **US buyer (Store purchases, opening disputes):** `sb-hizpg53637037@personal.example.com`
- **Indian personal buyer (negative tests only):** your original sandbox personal account

**Keep running:** backend (Terminal 1), frontend (Terminal 2), cloudflared (Terminal 3).
**Irreversible** steps are marked ⚠️ (they change the sandbox case at PayPal and can't be undone).

**Where time matters:** new disputes reach the app in seconds via webhook (or within ~1–3 min via the 60 s
poller if PayPal's dispute list lags). PayPal moves chargebacks between "PayPal reviewing" and "Needs your
response" on its own schedule (minutes to hours in sandbox).

---

## A. Login & access

| ID | Steps | Expected |
|---|---|---|
| A1 | Log out → open `localhost:5173/queue` | Redirected to **/login** ("Sign in to DisputeShield"). |
| A2 | Click **Log in with PayPal** → sign in as a **merchant** account | Back on the dispute queue; your name + **Log out** top right. |
| A3 | Log out → open `localhost:5173/cases/PP-R-ZWL-10195422` directly → log in | After login you land **on that case**, not the queue. |
| A4 | Log in with a business account **not** on the allowed list | "Couldn't sign you in — … is not allowed to manage this store". |
| A5 | Log in with the **US buyer** (personal) account | "Couldn't sign you in" (buyers can't open the dashboard). |
| A6 | On PayPal's login page click **Cancel / Return to merchant** | "Couldn't sign you in" with PayPal's reason; **Try again** works. |
| A7 | Click **Log out** | Back on /login; opening /queue asks you to log in again. |
| A8 | While logged out open `localhost:5173/store` | Store works without login (buyers don't log in to the dashboard). |
| A9 | Close the browser tab completely, reopen `/queue` | Asked to log in again (session ends with the tab). |
| A10 | Restart cloudflared (new URL) **without** updating anything → try to log in | Login fails at PayPal ("invalid … redirect_uri"). Expected: send me the new URL; I update `.env` + webhook, you update the PayPal Return URL. |

## B. Store & checkout (as the buyer)

| ID | Steps | Expected |
|---|---|---|
| B1 | Private window → Store → **Lamp** → Pay with PayPal → US buyer → pay | Card: "Paid. Order #N, capture …". PayPal **asks for a shipping address**. |
| B2 | Same for **Mug** | Paid; shipping address asked. |
| B3 | Same for **Ebook** | Paid; **no shipping address asked** (digital). |
| B4 | Start a checkout, then close the PayPal window / Cancel | Button returns to normal; no "Paid"; nothing in the dispute queue. |
| B5 | Pay with the **Indian personal** buyer | PayPal shows "Things don't appear to be working at the moment" (India→India is blocked by PayPal). Expected, not a bug. |
| B6 | Pay while logged into PayPal as a **seller** account | PayPal: "You are logging in to the account of the seller…". Expected. |
| B7 | (For later disputes) note each **Order #** from the "Paid" message | Needed to add tracking (tell me the number). |

## C. Dispute arrives (buyer opens it)

Open disputes as the US buyer at **www.sandbox.paypal.com → Activity → the payment → Report a problem**.

| ID | Steps | Expected |
|---|---|---|
| C1 | Dispute a paid **lamp** order: "I didn't receive it" | Within seconds–3 min: a **"New dispute" toast** (on any dashboard page) and a new queue row: *Item not received*, amount, buyer email, **Due in** countdown. |
| C2 | Open the case while the agents work | **Agent timeline** shows "Live" and fills step by step (investigator → analyst → resolution). |
| C3 | Dispute a **mug** order: "Item is significantly not as described" | Reason *Not as described*; checklist lists **Listing, Order, Refund policy** as required. |
| C4 | Dispute an order with "I didn't authorize this payment" | Reason *Unauthorized*; checklist requires tracking, delivery confirmation and the PayPal payment record. |
| C5 | **Close cloudflared**, open a new dispute, wait 3 min, restart cloudflared (send me the URL) | The dispute still appears via the **poller** (webhooks missed while the tunnel was down). |
| C6 | Leave a case open while PayPal escalates it (Inquiry → Chargeback, or "PayPal reviewing" → "Needs your response") | Timeline gets new steps; a **new decision** replaces the old one ("Earlier decisions" shows the old one as superseded). |
| C7 | Dispute a PayPal payment that was **not** made through this Store | Case appears; facts say **"No store order matched"**; evidence is PayPal-only. |

## D. What the agents recommend

| ID | Situation | Expected recommendation |
|---|---|---|
| D1 | Not received, tracking added but **no delivery confirmation** (sandbox tracking never shows delivered) | **Gather more** — "missing delivery confirmation"; score ~58/100. |
| D2 | Not received, **no tracking added** at all | **Settle** if under your threshold, otherwise **Gather more**; tracking shown ✗ required. |
| D3 | Not as described, **refund policy saved** in Settings, ebook or cheap item | Refund policy ✓ in the checklist; for small amounts **Settle**. |
| D4 | Not as described, **no refund policy** saved | Refund policy ✗ required; timeline says "No refund policy on file". |
| D5 | Amount **under the threshold** (default $25) with a weak case | **Settle**, economic note says it's not worth fighting. |
| D6 | Any recommendation | Reasoning cites evidence numbers (#…) that exist on the page; "Is it worth it?" note present; nothing invented. |
| D7 | Turn off LLM keys (remove `GROQ_API_KEY` and `GEMINI_API_KEY`, restart backend), open a new dispute | Status **"Needs your review"**; timeline says why. Restore the keys afterwards. |

## E. Responding to a dispute (merchant actions)

| ID | Steps | Expected |
|---|---|---|
| E1 | On a **Gather more** card, look at **Approve** | Disabled until you choose an action (**Change action**). |
| E2 | Click **Change action** repeatedly | Switches **Fight ↔ Settle**; text says "changed from …". |
| E3 | Choose **Fight** with an empty note | Approve disabled; "Write the seller note to approve a fight". |
| E4 | Click **Approve** | A confirmation box: "Send this … to PayPal now? This can't be undone." **Cancel** sends nothing. |
| E5 | ⚠️ **Settle** → Approve → **Yes, send it** (e.g. the $89 case, decision #4 "Try again · Settle") | "Sent to PayPal (accept-claim)"; status **Response sent**; within minutes **Resolved**; timeline shows each step. |
| E6 | ⚠️ **Fight** when PayPal shows **"Needs your response"** → Approve | "Sent to PayPal (provide-evidence)"; the **evidence PDF** link opens a one-page summary; status Response sent. |
| E7 | ⚠️ **Fight** while PayPal is **reviewing** (chargeback, before "Needs your response") | Sent as **supporting info** (provide-supporting-info) with the PDF. |
| E8 | **Reject** | Status "You rejected it"; nothing sent; **Re-analyse** still available. |
| E9 | Edit the note, then approve | The **"Note sent"** shown afterwards is your edited text. |
| E10 | Click **Yes, send it** twice quickly / in two tabs | Only **one** response is sent; the other shows "decision is …, only PROPOSED…". |
| E11 | Approve a case that PayPal **already resolved** (e.g. after E5, on an old tab) | Error "the dispute is already resolved at PayPal; nothing can be sent". |
| E12 | If PayPal refuses (red "PayPal refused … EVIDENCE_TYPE_IS_NOT_ALLOWED") | App already retried once without tracking; **Try again** lets you choose Fight/Settle; a refused attempt **can't** be retried twice. |
| E13 | **View source** on each evidence card | Dialog shows the original record (store data, or PayPal data fetched live). Esc closes it. |
| E14 | **Re-analyse** | "Re-analysing…", then a new decision appears; the old one moves to "Earlier decisions". |

## F. Live updates & notifications

| ID | Steps | Expected |
|---|---|---|
| F1 | Keep **Settings** open while a new dispute arrives | Toast "New dispute … Open case →" appears on Settings; the link opens the case. |
| F2 | Turn Wi-Fi off for ~20 s on a case page, then on | Timeline status: **Reconnecting…** then **Live**; no steps missing or duplicated. |
| F3 | Stop the backend (Ctrl+C) with the queue open | Red "Can't reach the DisputeShield server…" with **Retry**; last list stays visible. Start backend → Retry works. |
| F4 | Leave the queue open several minutes | "Due in" countdowns tick; rows under 48 h turn **red**. |

## G. Settings

| ID | Steps | Expected |
|---|---|---|
| G1 | Threshold: type `12.345`, `-1`, `abc`, `20000` | Red help text; **Save** disabled. |
| G2 | Set threshold to `50` → Save → reload | Shows 50.00; "Last changed" updates. |
| G3 | After G2, open a case analysed with $25 | Banner "Analysed with a $25.00 settle threshold; your setting is now $50.00" + **Re-analyse**. |
| G4 | Paste the refund policy → **Save policy** | "Saved. New investigations include it as evidence." Clearing it and saving shows "Removed." |
| G5 | **Undo** after editing | Reverts to the saved value. |

## H. Demo helpers (sandbox only; admin token)

Ask me to run these, or use the commands from the earlier guide (they need `ADMIN_TOKEN` from `backend/.env`).

| ID | Steps | Expected |
|---|---|---|
| H1 | ⚠️ `require-evidence` on a case **PayPal is reviewing** | Case moves to "Needs your response"; a new analysis appears. (Note: on PP-R-ZWL-10195422 this made PayPal refuse evidence later — prefer waiting for PayPal's own move.) |
| H2 | ⚠️ `adjudicate SELLER_FAVOR` on a case PayPal is **reviewing** (e.g. after E6/E7) | Case **Resolved** in your favour; pending decisions closed. |
| H3 | `adjudicate` on a case **waiting for your response** | PayPal error "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE" (respond first). |

## I. Phone (open the dashboard on your phone or a narrow window)

| ID | Steps | Expected |
|---|---|---|
| I1 | Queue at phone width | Cards instead of a table; search works; nothing scrolls sideways. |
| I2 | Case page at phone width | **Recommendation first**, then facts, checklist, evidence, timeline. |
| I3 | View source / confirmation box on phone | Fit the screen; usable with a thumb. |

---

**Suggested order for one complete demo run:** A2 → B1 → (add tracking: tell me the order #) → C1 → C2 → D1 →
E6 or E5 → F1 → H2. Expect ~30–60 min, mostly waiting for PayPal's sandbox.

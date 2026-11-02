# DisputeShield — end-to-end flows (what happens for each choice)

Each flow is one complete scenario you can test by hand, from the buyer paying to the case closing.
Write ✅ / ❌ and notes in the **Result** line. ⚠️ = irreversible at PayPal (sandbox only).
How to start the app and open disputes: `docs/TEST_SCENARIOS.md` (sections 1–3).

---

## Part 1 — How tracking works (why it decides so many disputes)

**What PayPal cares about:** for *"I didn't receive it"* the seller wins only by proving it was **shipped**
(tracking) **and delivered** (delivery confirmation to the buyer's address). Without that the seller
almost always loses, so tracking is the single most important piece of evidence.

**Where tracking comes from in this app**

1. **Merchant adds tracking to a store order**: dashboard → **Orders** → the order → **+ Add tracking**
   (carrier + tracking number), or on a dispute's page in the **Shipping & tracking** card. The backend:
   - saves it as a *shipment* on the order (carrier + tracking number), and
   - uploads it to **PayPal** (Orders API "add tracking"), so the buyer sees it in their PayPal activity
     and PayPal can use it when the dispute is opened.
2. **When a dispute arrives, the Investigator agent** collects tracking from **both** places:
   - PayPal's record of the order's trackers (*source: PayPal*), and
   - the store's shipment (*source: store*), including delivery date and the address it was delivered to.
   Each becomes an evidence card (#id) you can open with **View source**.
3. **Delivery confirmation**: when the carrier's tracking page shows *Delivered*, the merchant clicks
   **Record delivery** on that shipment (date + "the order's shipping address" or a different address).
   It is stored in DisputeShield only: PayPal has no API for a seller to mark a parcel delivered, and sandbox
   carriers never report delivery. The evidence card says it was "recorded by the merchant from the
   carrier's confirmation". Only record a delivery the carrier really confirms; it is sent to PayPal as proof.
   The investigator then
   compares the delivery address with the order's shipping address:
   - matches → strong evidence;
   - differs → red flag *"Carrier delivered to a different address"* (score −0.4).
   A **cancelled** tracking number is also a red flag (−0.2).

**How tracking feeds the decision (fixed rules, not the AI)**

| Dispute reason | Required evidence (70 % of the score) | Supporting (30 %) |
|---|---|---|
| Item not received | **tracking, delivery confirmation** | invoice, order, buyer history, buyer messages |
| Unauthorized | **tracking, delivery confirmation, invoice** (PayPal payment record) | order, buyer history, messages |
| Not as described | listing, order, refund policy | tracking, delivery confirmation, messages, history, invoice |

Score → baseline recommendation:
- **≥ 0.70 → Fight**
- **< 0.40 → Settle**
- between → **Settle** if the amount is under your threshold (default $25), else **Gather more**
- then, if PayPal doesn't allow that action at this stage → **Gather more** (with the reason).
The AI analyst reads the text (buyer messages, listing) and may move the recommendation **one step at most**,
and only by citing a buyer message or the listing. It can never cite evidence that doesn't exist.

**How tracking is sent when you Fight**
- If PayPal asks for evidence (*provide evidence*): sent as **proof of fulfilment** with the tracking
  number and carrier, plus your note and a one-page **PDF** summary.
- If PayPal refuses the tracking block (EVIDENCE_TYPE_IS_NOT_ALLOWED), the app **automatically resends once**
  with the note + PDF only (safe, because PayPal processed nothing the first time).
- If PayPal is reviewing (chargeback): sent as **supporting info** (note + PDF, the PDF lists the tracking).
- If the case is an **inquiry where PayPal takes no evidence yet** (typical for "not as described"):
  Fight = **escalate to a PayPal claim** with your note (PayPal's escalate request takes a note only, no
  files). PayPal then reviews it and asks for evidence; that stage change makes the agents analyse again.

**Tracking also changes how PayPal opens the case (sandbox):**
"not received" on an order **with** tracking usually opens straight as a *chargeback, PayPal reviewing*;
**without** tracking it opens as an *inquiry, waiting for you*.

**What tracking + delivery do to an "item not received" case (lamp, $89):**

| What's recorded | Score | Recommendation |
|---|---|---|
| nothing | ~0.1–0.2 | Settle |
| tracking only | ~0.5–0.6 | Gather more ("missing delivery confirmation") |
| tracking + delivery to the shipping address | ≥ 0.7 | **Fight** |
| tracking + delivery to a **different** address | drops by 0.4 (red flag) | Gather more / Settle |

After adding tracking or recording delivery on a disputed order, the case timeline shows the change and the
page asks you to **Re-analyse**; the agents then use the new evidence.

---

## Part 2 — The choices on a case and what each one does

| You click | What the app does | What PayPal does |
|---|---|---|
| **Approve** a **Fight** | Re-checks the case with PayPal; builds the request for the *current* stage; asks "Send … now? This can't be undone." On Yes: sends provide-evidence, **or** provide-supporting-info (note + PDF), **or** in an inquiry without evidence: **escalate** (note only; the card says "fight by escalating to a PayPal claim"). | Reviews it; later decides (minutes to days). After an escalation PayPal asks for evidence. |
| **Approve** a **Settle** | Same re-check; sends **accept-claim** with a short note ("We accept this claim and agree to refund …"). | Refunds the buyer, closes the case (**Resolved**). |
| **Gather more** card | Nothing to send. **Approve is disabled** until you pick Fight or Settle with **Change action**. | Nothing. |
| **Change action** | Switches Fight ↔ Settle. For Fight you must have a note (write one if none was drafted). Logged as "Approved X (changed from Y)". | — |
| Edit the **note** | Your text is what's sent and shown as "Note sent". | — |
| **Reject** | Decision marked "You rejected it". Nothing sent. | Nothing; the deadline keeps running. |
| **Re-analyse** | Runs all agents again with current data (new tracking, new policy, new threshold). Old decision → "Earlier decisions". | — |
| **Try again** (after PayPal refused) | Creates one new attempt where you choose Fight/Settle. Only once per failed attempt. | Gets the new request. |

Status shown in the queue / case: *Analysing → Waiting for your approval → Response sent → Resolved*
(or *You rejected it*, *Failed*, *Needs your review*).

---

## Part 3 — End-to-end flows to test

### Flow 1 — "Not received", tracking added, you **Fight** (PayPal reviewing → supporting info) ⚠️
1. Buyer buys the **Lamp ($89)** → note Order #.
2. Dashboard → **Orders** → that order → **+ Add tracking** → USPS, `9400111899223344556677` → **Save tracking**.
   *Expected:* green "Tracking saved and uploaded to PayPal"; the order shows "USPS 9400… · Shipped"; the
   buyer's PayPal activity shows the tracking.
3. Buyer disputes: **"I didn't receive it"**.
4. *Expected in the app:* toast + queue row *Item not received · $89*. Timeline: investigator finds the
   store order, the PayPal payment, **tracking from PayPal and the store**, "no delivery confirmation";
   analyst score ~0.5–0.6; status at PayPal "PayPal reviewing (chargeback)".
5. Recommendation: **Gather more** — "missing delivery_confirmation". Approve disabled.
6. **Change action → Fight**, write a note (e.g. "Shipped on <date> with USPS tracking 94001…; see #ids").
7. **Approve → Yes, send it.**
8. *Expected:* "Sent to PayPal (**provide-supporting-info**) … PayPal will now review it."; **evidence PDF**
   link opens a one-page summary listing the tracking; status **Response sent**.
9. Later: PayPal may update the case → timeline "PayPal updated the case after your response … no new
   analysis" (no new decision). Eventually PayPal resolves it (in sandbox this can take long).

Result: ______

### Flow 2 — "Not received", **no** tracking, you **Settle** ⚠️
1. Buyer buys the **Phone case ($12)**; do **not** add tracking.
2. Buyer disputes: "I didn't receive it".
3. *Expected:* case opens as an inquiry, *Needs your response*; checklist **tracking ✗, delivery ✗**;
   score low → **Settle** ("not worth fighting" / under threshold).
4. **Approve → Yes, send it.**
5. *Expected:* "Sent to PayPal (**accept-claim**) … PayPal refunds the buyer and closes the case."
   → **Response sent** → within minutes **Resolved**. Buyer's PayPal shows the refund.

Result: ______

### Flow 3 — "Not received", tracking added **after** the dispute, **Re-analyse**, then **Fight** (provide evidence) ⚠️
1. Buyer buys the **Lamp**; no tracking yet. Buyer disputes "I didn't receive it".
2. *Expected:* inquiry, *Needs your response*; "You can now" should include **send evidence**;
   recommendation **Settle** (tracking and delivery both missing → score below 0.40). Don't approve it.
3. On the case page, **Shipping & tracking** card → **+ Add tracking** → Save. *Expected:* "Tracking saved
   and uploaded to PayPal. Re-analyse so the agents use it: [Re-analyse]"; timeline line "Added tracking …".
   Click **Re-analyse**.
4. *Expected:* "Re-analysing…"; new timeline steps "Found tracking …"; tracking ✓; score rises to ~0.5;
   new decision **Gather more** (still no delivery confirmation; $89 is above the threshold); old one in
   "Earlier decisions".
5. **Change action → Fight** (if not already), check the note, **Approve → Yes**.
6. *Expected:* "Sent to PayPal (**provide-evidence**)" with the PDF. If PayPal refused the tracking block,
   the timeline shows "PayPal refused the evidence with tracking … resending … note and PDF only", and it
   still ends as sent.
7. If "You can now" did **not** include provide evidence: approving Fight shows
   "PayPal allows no way to contest this dispute now" — that's PayPal's rule at that stage; settle or wait.

Result: ______

### Flow 4 — "Not as described", refund policy saved (inquiry: PayPal won't take evidence yet)
1. Settings → paste `docs/REFUND_POLICY_TEMPLATE.md` → **Save policy** ("Saved…").
2. Buyer buys the **Mug ($45)** and disputes **"significantly not as described"**.
3. *Expected:* reason *Not as described*; checklist **listing ✓, order ✓, refund policy ✓**; strong score,
   but recommendation **Gather more** with "PayPal does not accept evidence at this stage (settling is
   still possible)". "You can now": settle / offer / escalate / message.
4. If the recommendation is **Fight** (or you pick it with Change action): the card says *"You are about to
   fight by escalating to a PayPal claim"* with an explanation; the confirm box says "Escalate this case to
   a PayPal claim now?". If "You can now" doesn't list *escalate to a PayPal claim*, Fight is disabled
   ("PayPal doesn't allow this right now").
5. ⚠️ **Approve → Yes**. *Expected:* "Sent to PayPal (escalate) … PayPal will review it as a claim and will
   probably ask you for evidence…". Later the stage changes (Inquiry → Claim/Chargeback) and a **new
   analysis** appears where Fight sends evidence.
6. ⚠️ Alternative on another case: **Change action → Settle → Approve** → accept-claim → Resolved.

Result: ______

### Flow 5 — "Not as described" **without** a refund policy
1. Settings → clear the policy → Save ("Removed.").
2. Buyer buys the **Phone case** and disputes "not as described".
3. *Expected:* refund policy **✗ required**; timeline "No refund policy on file"; lower score; $12 is under
   the threshold → **Settle**.
4. Put the policy back, click **Re-analyse** → refund policy ✓, score goes up, new decision.

Result: ______

### Flow 6 — Threshold changes the answer
1. Open a weak-to-middle case (e.g. Flow 2 or 5 before sending anything).
2. Settings → threshold **5** → Save. Back on the case: banner "Analysed with a $25.00 settle threshold;
   your setting is now $5.00" → **Re-analyse**.
3. *Expected:* a $12 case is no longer auto-settle → **Gather more** (or Settle only if the score < 0.40).
4. Set the threshold back to **25**.

Result: ______

### Flow 7 — **Reject**, then decide later
1. On any case waiting for approval click **Reject**.
2. *Expected:* "You rejected it"; nothing sent (PayPal unchanged); **Re-analyse** still available; the due
   countdown keeps running.
3. **Re-analyse** → a fresh decision you can approve.

Result: ______

### Flow 8 — Edit the note before fighting ⚠️
1. On a Fight (or Change action → Fight) edit the note text.
2. Empty note → Approve disabled ("Write the seller note to approve a fight").
3. Approve → Yes. *Expected:* "Note sent: <your exact text>"; the PDF contains your text; timeline
   "Approved FIGHT … with an edited note".

Result: ______

### Flow 9 — Safety: double send and stale tabs
1. Open one waiting case in **two tabs**. Approve → Yes in tab 1, then in tab 2.
   *Expected:* tab 2 "decision is …; only PROPOSED decisions can be approved or rejected"; only one response at PayPal.
2. After a case is **Resolved**, use an old tab still showing Approve.
   *Expected:* "the dispute is already resolved at PayPal; nothing can be sent".

Result: ______

### Flow 10 — PayPal moves the case on its own
1. Leave a case you have **not** answered open while PayPal escalates it (inquiry → chargeback, or
   "PayPal reviewing" → "Needs your response").
   *Expected:* new timeline steps, a **new decision**; the old one under "Earlier decisions" (superseded).
2. On a case you **have** answered, PayPal updates it.
   *Expected:* one timeline line "PayPal updated the case after your response … nothing new is asked of
   you, so no new analysis"; **no** new decision.

Result: ______

### Flow 11 — Payment not from the store
1. Buyer: Send & Request → Send → merchant email → $10 → "Paying for an item or service". Dispute it.
2. *Expected:* case appears; facts "No store order matched"; evidence only from PayPal (payment record);
   checklist mostly ✗ → low score → **Settle** ($10 < $25).

Result: ______

### Flow 12 — Who sees what (login)
1. Signed out / buyer on `localhost:5173/store`: top bar shows **only "Store"** (no Disputes, Orders, Settings).
2. Typing `/queue`, `/orders` or `/settings` directly → **login page**.
3. Log in with PayPal as a **buyer** → "Couldn't sign you in" (still only Store in the bar).
4. Log in as the **merchant** → **Disputes, Orders, Store, Settings** + your name + Log out.
5. Log out → back to only "Store".

Result: ______

### Flow 13 — Record delivery → the agents now say **Fight** (provide evidence) ⚠️
1. Use a lamp order with tracking (Flow 1 or 3). In the carrier's world the parcel arrived, so record it:
   **Orders** (or the case's **Shipping & tracking** card) → the shipment → **Record delivery** → today's
   date → "The order's shipping address" → **Save delivery**.
   *Expected:* "Delivered <date> · recorded by you"; future dates or dates before the order are refused.
2. On the case: timeline "Recorded delivery of … re-analyse to include it" → **Re-analyse**.
3. *Expected:* checklist **tracking ✓, delivery confirmation ✓**; evidence card "Carrier delivered … matches the
   order's shipping address (recorded by the merchant from the carrier's confirmation)"; score ≥ 0.70 →
   **Fight** (if PayPal still allows a response).
4. Approve → Yes → "Sent to PayPal (provide-evidence)" when PayPal is waiting for you, or supporting info
   while PayPal reviews.
5. Variant: record delivery to **a different address** → red flag "Carrier delivered to a different address
   than the order's shipping address" → score drops → not Fight.

Result: ______

### Flow 14 — Orders page basics
1. **Orders** tab (merchants only). *Expected:* every paid order: items, total, buyer, ship-to; disputed
   orders link to their case; abandoned checkouts aren't listed.
2. Ebook order: "Digital item: nothing to ship" (no tracking form).
3. Add the same tracking number twice → error "tracking … is already on order #N".
4. Carrier **Other…** → type a name (e.g. "India Post") → saved as that carrier.

Result: ______

### Flow 15 — Faulty electronics: invite the buyer to troubleshoot (Phase 9A)
1. As the buyer, buy the **Wireless earbuds ($59)** (or the Smart LED desk lamp). Add tracking and record delivery
   if you like (Flow 13); it doesn't change eligibility.
2. As the buyer, open a dispute: **item doesn't work / not as described**.
   *Expected:* timeline "Eligible for buyer troubleshooting: Wireless earbuds is an electrical/electronic item";
   case page card **Buyer troubleshooting** with the model and **Invite via PayPal message**.
3. Click **Invite via PayPal message**. *Expected:* PayPal shows the seller message with a private link
   (`…/help/<n>?t=…`) in the buyer's case; card → *Invited, not opened yet*; link works until 48 h before
   PayPal's response deadline (5 days if PayPal gives none).
4. Open the link (as the buyer). *Expected:* "Let's get your Wireless earbuds working" and the assistant asks
   "What's going wrong with it?". Merchant card → *Buyer opened the page*. (Continue with Flow 16.)
5. **Send a new link**: the old link stops working, the new one works.
6. The rest of the case is unchanged: the agents still recommend Fight / Settle / Gather more as before; the
   troubleshooting session doesn't move money or answer PayPal for you.

Result: ______

### Flow 16 — The troubleshooting agent fixes it, or confirms the defect (Phase 9B)
Start from Flow 15 step 4 (buyer page open).
1. Buyer types the problem, e.g. "The left earbud won't charge and the case light stays off".
   *Expected:* after up to ~1–1.5 min: 3–7 steps, each with a **Source:** link (sites the web search found:
   the maker's support pages when they exist, otherwise similar official guidance). The last step asks for a
   photo/video. If no usable sources or plan: 5 **general safety-checked steps, not specific to your model**.
2. **Path A (fixed):** press **Done, it works now** on a step → **Yes, it works now**.
   *Expected:* "Great, it's working!"; merchant card **Fixed by troubleshooting**; timeline "Fixed: the buyer confirmed…".
3. **Path B (defect):** on every step press **Done, still not working**; upload the requested video/photo.
   *Expected:* "We've sent your results to the seller"; card **Defect confirmed** with the proof link (*video 1*) and the
   whole conversation. The agents' dispute recommendation is unchanged by this (see PROGRESS G41).
4. **Path C (safety):** type "it's getting hot to the touch". *Expected:* the fixed safety message, chat closed,
   card **Stopped for safety**, timeline **SAFETY STOP …** — contact the buyer.
5. **Path D (seller):** **Talk to the seller** → card **Handed to you**.

Result: ______

### Flow 17 — The buyer page on a phone, with a photo as proof (Phase 9C)
Use a narrow window (or Chrome DevTools → device mode, iPhone size) for the buyer.
1. Invite the buyer (Flow 15) and open the link. *Expected:* header with the store name, "Help with your order
   #…", product + model, PayPal case id and **⏱ time left**; the assistant's first question.
2. Describe the problem. *Expected:* your message appears at once, then "The assistant is typing…", then the
   steps (no reload). A sticky **Steps 1/5** bar appears; tap it → the checklist slides up from the bottom.
3. Answer each step with **Done, still not working** (in the sheet). *Expected:* an instant reply each time
   ("Next, step N: …"); on the proof step "Please add the video/photo for step N…".
4. On the proof step press **Choose a file** (or **Use camera** on a real phone) and pick a photo.
   *Expected:* thumbnail + progress bar, then **"We've sent your results to the seller"**.
5. Merchant: case page → **Buyer troubleshooting** card. *Expected:* **Defect confirmed**; under the step:
   *photo 1* plus its checks (type, "removed: GPS location" if the photo had location, capture time vs the
   dispute, duplicates if any) and "AI look: automatic photo check is off" (see PROGRESS G50).

Result: ______

### Flow 18 — Troubleshooting results change the recommendation (Phase 9D)
1. Run Flow 16 path A (fixed) on one case and path B (defect) on another; keep the queue open in another tab.
   *Expected:* a toast **Buyer finished troubleshooting** for each; the queue shows the badge.
2. Open each case. *Expected:* timeline "Buyer finished troubleshooting (…): re-analysing…", then a new
   recommendation; the evidence list has a **troubleshooting_record** item ("View source" shows the session).
   Fixed: the rule note "buyer confirmed the item works after troubleshooting". Defect: red flag
   **defect_confirmed**.
3. On a third case, while the buyer's page is open: **Reply as seller** → the buyer sees it live; the buyer's
   next message gets no assistant reply until you press **Let the assistant continue**.
4. Approve **Fight** on the fixed case (only if you mean to send it to PayPal). *Expected:* the evidence PDF
   (link on the decision) has a 2nd page with the troubleshooting record.

Result: ______

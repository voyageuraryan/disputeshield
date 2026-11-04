"""Merchant UI scenarios in a real browser (see README). Nothing is ever sent to PayPal."""

import base64
import re
import time

from conftest import overflow


def real_errors(pg):
    # Requests a test deliberately blocks show up as "Failed to load resource"; anything else is a bug.
    return [e for e in pg.errors if "Failed to load resource" not in e and "net::ERR" not in e]


def did(stack, pid):
    return stack.sql("select id from disputes where paypal_dispute_id=?", pid)[0][0]


# ---- queue ---------------------------------------------------------------------------------

def test_grid_reports_no_missing_features(stack, page):
    """In development AG Grid's ValidationModule logs an error for any feature whose module isn't registered."""
    page.goto(f"{stack.base}/queue")
    page.wait_for_selector(".ag-row")
    page.fill("input[type=search]", "x")
    page.get_by_role("columnheader", name="Amount").click()
    page.locator(".ag-floating-filter input").first.fill("PP")
    page.wait_for_timeout(500)
    grid_msgs = [e for e in page.errors if "AG Grid" in e]
    assert not grid_msgs, grid_msgs


def test_countdown_refresh_after_30s_has_no_grid_errors(stack, browser):
    """Regression: the 30 s countdown refresh calls api.refreshCells (needs RenderApiModule)."""
    ctx = browser.new_context()
    pg = ctx.new_page()
    errs = []
    pg.on("console", lambda m: m.type == "error" and "AG Grid" in m.text and errs.append(m.text))
    pg.clock.install()
    pg.goto(f"{stack.base}/queue")
    pg.wait_for_selector(".ag-row")
    pg.clock.fast_forward(65_000)  # two countdown ticks
    pg.wait_for_timeout(500)
    assert not errs, errs
    ctx.close()


def test_queue_columns_sort_filter_search_countdown(stack, page):
    page.goto(f"{stack.base}/queue")
    page.wait_for_selector(".ag-row")
    assert page.locator(".ag-header-cell-text").all_inner_texts() == [
        "Dispute", "Reason", "Amount", "Buyer", "Due in", "Recommendation", "Confidence", "Status"]
    rows = page.locator(".ag-grid-scrolling-container .ag-row")
    assert rows.count() == 13  # A, B, C, D, F, G, H, I, J, K, R, E, O
    assert "PP-DEMO-A" in page.locator(".ag-row[row-index='0']").inner_text()  # soonest deadline first
    assert page.locator(".ag-grid-scrolling-container .font-semibold.text-red-600").count() == 2  # under 48 h
    page.fill("input[type=search]", "case-b")
    page.wait_for_function("document.querySelectorAll('.ag-grid-scrolling-container .ag-row').length === 1")
    page.fill("input[type=search]", "")
    page.get_by_role("columnheader", name="Amount").click()
    page.wait_for_timeout(300)
    assert page.locator(".ag-row[row-index='0'] [col-id='amount']").inner_text() == "$12.00"
    assert overflow(page) <= 0 and not real_errors(page)
    page.locator(".ag-row", has_text="PP-DEMO-A").click()
    page.wait_for_url("**/cases/PP-DEMO-A")


def test_queue_error_retry_and_empty(stack, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.route("**/api/disputes", lambda r: r.abort())
    pg.goto(f"{stack.base}/queue")
    pg.wait_for_selector("[role=alert]:has-text(\"Couldn't load disputes\")")
    pg.unroute("**/api/disputes")
    pg.locator("button:has-text('Retry')").click()
    pg.wait_for_selector(".ag-row")
    pg.route("**/api/disputes", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
    pg.goto(f"{stack.base}/queue")
    pg.wait_for_selector("text=No disputes yet")
    ctx.close()


def test_new_dispute_toast_on_any_page(stack, page):
    page.goto(f"{stack.base}/settings")  # not the queue: alerts are app-wide
    page.wait_for_selector("#threshold")
    page.wait_for_timeout(1000)  # alert stream connected
    stack.sql("insert into disputes (paypal_dispute_id, order_id, reason, status, amount, currency, raw) "
              "values ('PP-DEMO-NEW', 1, 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'OPEN', 61.5, 'USD', '{}')")
    toast = page.locator("[role=status]:has-text('New dispute')")
    toast.wait_for(timeout=8000)  # written by another process: picked up by the 3 s safety-net poll
    assert "$61.50" in toast.inner_text()
    toast.locator("text=Open case").click()
    page.wait_for_url("**/cases/PP-DEMO-NEW")


# ---- case page ---------------------------------------------------------------------------------

def test_case_three_columns_and_actions(stack, page):
    page.goto(f"{stack.base}/cases/PP-DEMO-A")
    page.wait_for_selector("h2:has-text('Recommendation')")
    cols = page.locator("div.grid.lg\\:grid-cols-12 > div")
    xs = sorted(round(cols.nth(i).bounding_box()["x"]) for i in range(3))
    assert len(set(xs)) == 3
    assert page.locator("h2:has-text('Evidence checklist')").count() == 1
    assert page.locator("text=View source").count() == 4
    page.locator("text=View source").first.click()
    page.wait_for_selector("[role=dialog] >> text=Store database")  # record loads asynchronously
    page.keyboard.press("Escape")
    approve = page.locator("button:has-text('Approve')")
    assert approve.inner_text().strip() == "Approve · Fight"
    page.locator("button:has-text('Change action')").click()
    assert approve.inner_text().strip() == "Approve · Settle"
    approve.click()
    assert page.locator("text=This can't be undone").count() == 1
    page.locator("button:has-text('Cancel')").click()  # never "Yes, send it"
    assert overflow(page) <= 0 and not real_errors(page)


def test_gather_more_needs_action_and_note(stack, page):
    page.goto(f"{stack.base}/cases/PP-DEMO-C")
    approve = page.locator("button:has-text('Approve')")
    approve.wait_for()
    assert approve.is_disabled() and page.locator("text=Red flag").count() == 1
    page.locator("button:has-text('Change action')").click()
    assert approve.is_disabled() and page.locator("text=Write the seller note").count() == 1
    page.fill("textarea", "Factual note.")
    assert not approve.is_disabled()


def test_live_timeline_and_states(stack, page):
    page.goto(f"{stack.base}/cases/PP-DEMO-A")
    page.wait_for_selector("text=Live")
    tl = page.locator("section[aria-label='Agent timeline'] li")
    n0 = tl.count()
    # Rows written by another process arrive through the safety-net poll (in-process commits wake instantly).
    stack.sql("insert into agent_steps (dispute_id, agent, action, created_at) values (?,?,?,datetime('now'))",
              did(stack, "PP-DEMO-A"), "investigator", "Found tracking 1Z999 (UPS), delivered 3 Nov")
    page.wait_for_selector("text=Found tracking 1Z999 (UPS), delivered 3 Nov", timeout=8000)
    assert tl.count() == n0 + 1
    page.goto(f"{stack.base}/cases/PP-DEMO-E")
    page.wait_for_selector("text=No agent activity yet")
    page.goto(f"{stack.base}/cases/NOPE-1")
    page.wait_for_selector("text=This dispute isn't in DisputeShield")


def test_stream_blocked_falls_back_to_polling(stack, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.route("**/events*", lambda r: r.abort())
    pg.goto(f"{stack.base}/cases/PP-DEMO-A")
    pg.wait_for_selector("text=refreshing every 5 s", timeout=40000)
    stack.sql("insert into agent_steps (dispute_id, agent, action, created_at) values (?,?,?,datetime('now'))",
              did(stack, "PP-DEMO-A"), "executor", "Polled step appears")
    pg.wait_for_selector("text=Polled step appears", timeout=12000)
    ctx.close()


def test_threshold_change_offers_reanalyse(stack, page):
    stack.sql("update decisions set analysed_threshold=25")
    page.goto(f"{stack.base}/settings")
    page.wait_for_selector("#threshold")
    page.fill("#threshold", "50")
    page.locator("button:has-text('Save')").first.click()
    page.wait_for_selector("text=Saved. New analyses use this threshold.")
    page.goto(f"{stack.base}/cases/PP-DEMO-C")
    page.wait_for_selector("text=your setting is now $50.00")
    assert page.locator("button:has-text('Re-analyse')").count() >= 1
    page.goto(f"{stack.base}/settings")
    page.fill("#threshold", "25")
    page.locator("button:has-text('Save')").first.click()
    page.wait_for_selector("text=Saved.")


# ---- settings ----------------------------------------------------------------------------------

def test_settings_threshold_validation_and_policy(stack, page):
    page.goto(f"{stack.base}/settings")
    inp = page.locator("#threshold")
    inp.wait_for()
    save = page.locator("button:has-text('Save')").first
    for bad in ["12.345", "-1", "abc", "20000"]:
        inp.fill(bad)
        assert save.is_disabled() and inp.get_attribute("aria-invalid") == "true", bad
    inp.fill("30")
    save.click()
    page.wait_for_selector("text=Saved. New analyses use this threshold.")
    page.reload()
    page.wait_for_selector("#threshold")
    assert page.locator("#threshold").input_value() == "30.00"
    page.fill("#policy", "Returns within 30 days for a full refund.")
    page.locator("button:has-text('Save policy')").click()
    page.wait_for_selector("text=New investigations include it as evidence")
    assert stack.sql("select refund_policy from merchant_settings")[0][0] == "Returns within 30 days for a full refund."
    page.route("**/api/settings", lambda r: r.abort() if r.request.method == "PUT" else r.continue_())
    page.fill("#threshold", "31")
    save.click()
    page.wait_for_selector("text=Can't reach the DisputeShield server")


# ---- phones ---------------------------------------------------------------------------------------

def test_phone_layouts(stack, phone):
    for path in ["/queue", "/settings", "/cases/PP-DEMO-A", "/store"]:
        phone.goto(f"{stack.base}{path}")
        phone.wait_for_timeout(2000)
        assert overflow(phone) <= 0, path
    phone.goto(f"{stack.base}/queue")
    phone.wait_for_selector("main li button")
    assert phone.locator(".ag-root").count() == 0
    phone.goto(f"{stack.base}/cases/PP-DEMO-A")
    phone.wait_for_selector("h2:has-text('Recommendation')")
    rec = phone.locator("h2:has-text('Recommendation')").bounding_box()["y"]
    facts = phone.locator("h2:has-text('Dispute')").first.bounding_box()["y"]
    assert rec < facts


# ---- login gate (real auth instance) --------------------------------------------------------------

def test_login_required_and_redirects_back(auth_stack, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"{auth_stack.base}/cases/PP-DEMO-A")
    pg.wait_for_url(re.compile(r".*/login\?next=%2Fcases%2FPP-DEMO-A"))
    href = pg.locator("a:has-text('Log in with PayPal')").get_attribute("href")
    assert href == f"http://localhost:{auth_stack.api_port}/api/auth/paypal/start?next=%2Fcases%2FPP-DEMO-A"
    # The store stays public.
    pg.goto(f"{auth_stack.base}/store")
    pg.wait_for_selector("text=Demo store")
    assert "/login" not in pg.url
    ctx.close()


def test_callback_signs_in_and_logout(auth_stack, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    tok = auth_stack.session_token()
    pg.goto(f"{auth_stack.base}/auth/callback#token={tok}&next=%2Fcases%2FPP-DEMO-A")
    pg.wait_for_url("**/cases/PP-DEMO-A")
    pg.wait_for_selector("h2:has-text('Recommendation')")
    assert "token=" not in pg.url  # token removed from the address bar
    pg.wait_for_selector("text=E2E Merchant")
    pg.wait_for_selector("text=Live")  # SSE works with the session token
    pg.locator("button:has-text('Log out')").click()
    pg.wait_for_url("**/login")
    pg.goto(f"{auth_stack.base}/queue")
    pg.wait_for_url(re.compile(r".*/login.*"))
    ctx.close()


def test_callback_error_and_expired_session(auth_stack, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"{auth_stack.base}/auth/callback#error=sb-x%40personal.example.com%20is%20not%20allowed")
    pg.wait_for_selector("text=Couldn't sign you in")
    assert "is not allowed" in pg.inner_text("[role=alert]")
    # A session that expires while the page is open: the next API call sends the merchant to login.
    tok = auth_stack.session_token(exp_offset=10)  # minting takes ~2 s; leave room to load the page
    pg.goto(f"{auth_stack.base}/auth/callback#token={tok}&next=%2Fsettings")
    pg.wait_for_selector("#threshold")
    time.sleep(10)
    pg.fill("#policy", "x")
    pg.locator("button:has-text('Save policy')").click()
    pg.wait_for_url(re.compile(r".*/login\?.*reason="), timeout=8000)
    pg.wait_for_selector("text=Your session ended. Please log in again.")  # login page loads lazily
    ctx.close()


# ---- orders: tracking + delivery (G28) ------------------------------------------------------

def test_orders_page_record_delivery(stack, page):
    page.goto(f"{stack.base}/orders")
    page.wait_for_selector("text=Order #")
    assert page.get_by_role("link", name="Orders").count() == 1
    ebooks = stack.sql("select count(*) from orders where items like ?", "%EBOOK-01%")[0][0]
    assert ebooks > 0 and page.locator("text=Digital item: nothing to ship").count() == ebooks
    card = page.locator("section", has_text="9400111899220000000002")  # Nina's lamp, in transit
    assert "In transit" in card.inner_text() or "in transit" in card.inner_text()
    card.get_by_role("button", name="Record delivery").click()
    card.get_by_role("button", name="Save delivery").click()
    page.wait_for_selector("text=Delivery recorded for order")
    card = page.locator("section", has_text="9400111899220000000002")
    card.locator("text=recorded by you").wait_for()  # the list reloads after the save message appears
    row = stack.sql("select status, delivery_recorded_by from shipments where tracking_number=?", "9400111899220000000002")
    assert row == [("DELIVERED", "merchant")]
    assert real_errors(page) == []


def test_case_page_shipping_card(stack, page):
    page.goto(f"{stack.base}/cases/PP-DEMO-B")  # phone case, never shipped
    card = page.locator("section", has_text="Shipping & tracking")
    card.wait_for()
    assert "No tracking yet" in card.inner_text()
    card.get_by_role("button", name="+ Add tracking").click()
    save = card.get_by_role("button", name="Save tracking")
    assert save.is_disabled()
    card.get_by_label("Tracking number").fill("9400111899229999999999")
    assert save.is_enabled()  # not clicked: it would call PayPal
    card.get_by_role("button", name="Cancel").click()
    assert real_errors(page) == []


def test_orders_page_fits_a_phone(stack, phone):
    phone.goto(f"{stack.base}/orders")
    phone.wait_for_selector("text=Order #")
    assert overflow(phone) == 0


# ---- phase 9A: buyer troubleshooting ------------------------------------------------------------

def test_troubleshooting_invite_and_buyer_page(stack, page, browser):
    page.goto(f"{stack.base}/cases/PP-DEMO-D")  # faulty earbuds, inquiry without PayPal messaging
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.wait_for()
    assert "Wireless earbuds (SoundPeak E5)" in card.inner_text()
    assert card.get_by_role("button", name="Invite via PayPal message").is_disabled()
    assert "only during an inquiry" in card.inner_text() or "only allows messages" in card.inner_text()
    card.get_by_role("button", name="Email the link instead").click()
    link_box = card.get_by_label("Buyer's private link")
    link_box.wait_for()
    link = link_box.input_value()
    assert link.startswith(f"{stack.base}/help/") and "?t=" in link
    assert "Invited, not opened yet" in card.inner_text()

    buyer = browser.new_context()  # the buyer: another browser, never signed in
    bp = buyer.new_page()
    bp.goto(link)
    bp.wait_for_selector("text=Let's get your Wireless earbuds working")
    assert "?t=" not in bp.url  # the token is taken out of the address bar
    assert "Model SoundPeak E5" in bp.inner_text("body")
    bp.reload()  # still works from this tab's memory
    bp.wait_for_selector("text=Let's get your Wireless earbuds working")
    # a tampered link is refused
    bp.goto(link[:-2] + ("AA" if not link.endswith("AA") else "BB"))
    bp.wait_for_selector("text=isn't valid")
    buyer.close()

    page.reload()
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.wait_for()
    page.wait_for_selector("text=Buyer opened the page")
    assert page.locator("text=The buyer opened the troubleshooting page").count() >= 1  # timeline
    assert real_errors(page) == []


def test_troubleshooting_not_offered_for_non_electronics(stack, page):
    page.goto(f"{stack.base}/cases/PP-DEMO-B")  # phone case: not electronics
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.wait_for()
    assert "Not available: the order has no electrical/electronic item" in card.inner_text()
    assert card.get_by_role("button", name="Invite via PayPal message").is_disabled()  # with the reason above


def test_settings_auto_invite_toggle(stack, page):
    page.goto(f"{stack.base}/settings")
    box = page.get_by_label("Invite eligible buyers automatically")
    box.wait_for()
    assert not box.is_checked()
    box.check()
    page.wait_for_selector("text=Eligible buyers will be invited automatically")
    page.reload()
    box = page.get_by_label("Invite eligible buyers automatically")
    box.wait_for()
    assert box.is_checked()
    box.uncheck()  # leave the shared stack as it was
    page.wait_for_selector("[role=status]:has-text('Off.')")


def test_help_page_fits_a_phone(stack, phone):
    phone.goto(f"{stack.base}/help/1?t=nope")
    phone.wait_for_selector("[role=alert]")
    assert overflow(phone) == 0


# ---- phase 9B: buyer chat --------------------------------------------------------------------------------

def emailed_link(stack, page, pid):
    page.goto(f"{stack.base}/cases/{pid}")
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.get_by_role("button", name="Email the link instead").click()
    box = card.get_by_label("Buyer's private link")
    box.wait_for()
    return box.input_value()


def test_buyer_chat_safety_stop_and_one_device(stack, page, browser):
    link = emailed_link(stack, page, "PP-DEMO-F")
    buyer = browser.new_context()
    bp = buyer.new_page()
    bp.goto(link)
    bp.wait_for_selector("text=What's going wrong with it?")  # the agent's opening question
    other = browser.new_context()  # same link in a second browser (e.g. the merchant from the PayPal thread)
    op = other.new_page()
    op.goto(link)
    op.wait_for_selector("text=already open in another browser")
    other.close()
    bp.fill("#msg", "It started smoking when I plugged it in")
    bp.get_by_role("button", name="Send").click()
    bp.wait_for_selector("text=For your safety, please stop using it now")
    bp.wait_for_selector("text=Stopped for your safety")
    assert bp.locator("#msg").count() == 0  # the chat is closed
    buyer.close()
    page.reload()
    page.wait_for_selector("text=Stopped for safety")
    assert page.locator("text=SAFETY STOP").count() >= 1  # merchant timeline
    assert real_errors(page) == []


def test_buyer_chat_assistant_down_then_talk_to_seller(stack, page, browser):
    link = emailed_link(stack, page, "PP-DEMO-G")
    buyer = browser.new_context()
    bp = buyer.new_page()
    bp.goto(link)
    bp.wait_for_selector("text=What's going wrong with it?")
    bp.fill("#msg", "It doesn't turn on at all")
    bp.keyboard.press("Enter")
    # no LLM in the UI suite: the buyer isn't left stuck; they get the generic safe checklist (9C)
    bp.wait_for_selector("text=I can't look up your exact model right now")
    bp.wait_for_selector("text=General safety-checked steps, not specific to your model")
    bp.once("dialog", lambda d: d.accept())
    bp.get_by_role("button", name="Talk to the seller").click()
    bp.wait_for_selector("text=The seller is taking it from here")
    buyer.close()
    page.reload()
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.wait_for()
    page.wait_for_selector("text=Handed to you")
    assert "It doesn't turn on at all" in card.get_by_label("Conversation with the buyer").inner_text()  # 9D: always open


def test_help_chat_fits_a_phone(stack, phone, browser):
    phone.goto(f"{stack.base}/help/1?t=nope")
    phone.wait_for_selector("[role=alert]")
    assert overflow(phone) == 0


# ---- phase 9C: buyer page --------------------------------------------------------------------------------

SAMPLE = str(__import__("pathlib").Path(__file__).parent / "fixtures" / "lamp-not-lighting.jpg")


def test_buyer_full_flow_to_defect_confirmed(stack, page, browser):
    link = emailed_link(stack, page, "PP-DEMO-H")
    buyer = browser.new_context(viewport={"width": 1280, "height": 900})
    bp = buyer.new_page()
    errors = []
    bp.on("pageerror", lambda e: errors.append(str(e)))
    bp.goto(link)
    # header: store, order, product, PayPal case, time left
    bp.wait_for_selector("h1:has-text('Help with your order #INV-H-9C')")
    head = bp.inner_text("header")
    assert "Demo store" in head and "Smart LED desk lamp" in head and "PayPal case PP-DEMO-H" in head and "left" in head
    bp.wait_for_selector("text=What's going wrong with it?")
    bp.fill("#msg", "The lamp won't light up at all")
    bp.get_by_role("button", name="Send").click()
    bp.wait_for_selector("text=I can't look up your exact model right now")  # streamed in (SSE), no reload
    steps = bp.locator("aside")  # desktop: checklist on the right
    steps.wait_for()
    box = steps.bounding_box()
    chat = bp.locator("section[aria-label='Chat']").bounding_box()
    assert chat["x"] < box["x"] and 0.5 < chat["width"] / (chat["width"] + box["width"]) < 0.7  # ~60/40
    for n in range(1, 5):
        card = steps.locator("li[aria-current='step']")
        card.get_by_role("button", name="Done, still not working").click()
        bp.wait_for_selector(f"text=Next, step {n + 1}")
    last = steps.locator("li[aria-current='step']")
    assert "Show the problem" in last.inner_text()
    last.get_by_role("button", name="Done, still not working").click()
    bp.wait_for_selector("text=Please add the video for step 5")
    steps.get_by_label("Upload a video for step 5").set_input_files(SAMPLE)
    bp.wait_for_selector("text=We've sent your results to the seller")
    assert bp.locator("#msg").count() == 0 and "Talk to the seller" not in bp.inner_text("body")
    assert errors == []
    buyer.close()
    # merchant: outcome, the photo with its checks (GPS removed), and the timeline
    page.reload()
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.wait_for()
    page.wait_for_selector("text=Defect confirmed")
    assert "removed: GPS location" in card.inner_text()
    assert "AI look: automatic photo check is off" in card.inner_text()
    assert card.locator("img[alt^=\"Buyer's photo\"]").count() == 1  # thumbnail
    assert "The buyer's page has ended" in card.inner_text()
    # 9D: the queue shows that the buyer finished troubleshooting
    page.goto(f"{stack.base}/queue")
    page.wait_for_selector(".ag-row:has-text('PP-DEMO-H') :text('Buyer finished troubleshooting')")
    assert real_errors(page) == []


def test_buyer_link_expired_and_tampered(stack, page, browser):
    link = emailed_link(stack, page, "PP-DEMO-I")
    buyer = browser.new_context()
    bp = buyer.new_page()
    bp.goto(link[:-3] + ("xyz" if not link.endswith("xyz") else "abc"))  # tampered token
    bp.wait_for_selector("[role=alert]:has-text(\"isn't valid\")")
    stack.sql("update troubleshooting_sessions set expires_at='2020-01-01 00:00:00.000000' where dispute_id=?",
              did(stack, "PP-DEMO-I"))
    bp.goto(link)
    bp.wait_for_selector("text=This help page has closed")
    assert "expired" in bp.inner_text("[role=alert]")
    buyer.close()


def test_buyer_page_on_a_phone_has_steps_bar_and_sheet(stack, page, browser):
    link = emailed_link(stack, page, "PP-DEMO-J")
    phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    bp = phone.new_page()
    bp.goto(link)
    bp.wait_for_selector("text=What's going wrong with it?")
    bp.fill("#msg", "It flickers and then goes off")
    bp.get_by_role("button", name="Send").click()
    bar = bp.get_by_role("button", name="Steps 1/5")  # substring match
    bar.wait_for()
    assert bp.locator("aside").is_hidden()  # no side panel on a phone
    assert overflow(bp) == 0
    bar.click()
    sheet = bp.get_by_role("dialog", name="Troubleshooting steps")
    sheet.wait_for()
    assert "Restart it" in sheet.inner_text()
    assert overflow(bp) == 0
    size = bp.evaluate("parseFloat(getComputedStyle(document.querySelector('#msg')).fontSize)")
    assert size >= 16  # 16px base text (also stops iOS zooming into the box)
    bp.keyboard.press("Escape")
    sheet.wait_for(state="detached")
    phone.close()


# ---- phase 9D: the merchant replies in the buyer's chat ------------------------------------------------------

def test_reply_as_seller_reaches_the_buyer_live(stack, page, browser):
    link = emailed_link(stack, page, "PP-DEMO-K")
    buyer = browser.new_context()
    bp = buyer.new_page()
    bp.goto(link)
    bp.wait_for_selector("text=What's going wrong with it?")
    page.reload()
    card = page.locator("section", has_text="Buyer troubleshooting")
    card.wait_for()
    assert "What's going wrong with it?" in card.get_by_label("Conversation with the buyer").inner_text()
    card.get_by_label("Reply as seller").fill("Hi, it's the seller. Is the bulb screwed in fully?")
    card.get_by_role("button", name="Reply as seller").click()
    card.locator("[role=status]", has_text="The assistant is paused while you're chatting").wait_for()
    # the buyer sees it live (no reload), labelled "Seller", with the paused note
    bp.wait_for_selector("text=Is the bulb screwed in fully?")
    bp.wait_for_selector("text=The seller is chatting with you here now")
    assert "Seller" in bp.locator("li", has_text="Is the bulb screwed in fully?").last.inner_text()
    bp.fill("#msg", "Yes it is, still dark")
    bp.get_by_role("button", name="Send").click()
    bp.wait_for_selector("li:has-text('Yes it is, still dark')")
    assert bp.locator("text=The assistant is typing").count() == 0  # paused: no assistant turn
    card.get_by_role("button", name="Let the assistant continue").click()
    card.locator("[role=status]", has_text="The assistant is paused").wait_for(state="detached")
    card.get_by_label("Conversation with the buyer").get_by_text("Yes it is, still dark").wait_for(timeout=15000)
    # (the panel refreshes by itself every few seconds while the buyer's page is open)
    buyer.close()
    assert real_errors(page) == []


# ---- Phase 12: the merchant adds missing evidence -----------------------------------------------------------

PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def test_add_missing_evidence_and_confirm(stack, page):
    """Coach -> Add -> upload a file -> check fields -> confirm. A run 'in progress' is simulated so nothing starts
    (a run would fetch the dispute from PayPal); the evidence is saved and the merchant is told to re-analyse."""
    from datetime import datetime, timezone

    b = did(stack, "PP-DEMO-B")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    stack.sql("insert into agent_steps (dispute_id, agent, action, created_at) values (?, 'orchestrator', "
              "'run_started', ?)", b, now)
    try:
        page.goto(f"{stack.base}/cases/PP-DEMO-B")
        coach = page.locator("[data-testid=coach]")
        coach.wait_for()
        assert "→ score" in coach.inner_text()
        page.get_by_role("button", name="Add Refund policy").first.click()
        form = page.get_by_role("form", name="Add evidence")
        assert form.locator("select").input_value() == "refund_policy"

        # client-side check: a .txt is refused before anything is sent
        form.locator("input[type=file]").set_input_files({"name": "notes.txt", "mimeType": "text/plain",
                                                          "buffer": b"hello"})
        form.get_by_role("button", name="Upload").click()
        page.wait_for_selector("text=Only PDF, JPG or PNG files")

        form.locator("input[type=file]").set_input_files({"name": "policy.png", "mimeType": "image/png",
                                                          "buffer": PNG_1PX})
        form.get_by_role("button", name="Upload").click()
        card = page.locator("[data-testid^=confirm-]")
        card.wait_for()
        assert "automatic reading is off" in card.inner_text().lower()  # e2e has no Gemini key: merchant fills in
        assert stack.sql("select status from merchant_uploads where dispute_id=?", b) == [("PENDING",)]
        card.locator("input").first.fill("Store return policy screenshot")
        card.get_by_role("button", name="Confirm and add as evidence").click()
        page.wait_for_selector("text=already analysing this case")
        page.wait_for_selector("button:has-text('Remove')")
        row = stack.sql("select status, summary from merchant_uploads where dispute_id=?", b)[0]
        assert row[0] == "CONFIRMED" and row[1].startswith("Merchant-provided Store return policy screenshot")
        assert not real_errors(page)
    finally:
        stack.sql("delete from agent_steps where dispute_id=? and action='run_started' and created_at=?", b, now)


# ---- Phase 13: offers -----------------------------------------------------------------------------------------

def test_options_card_and_editing_the_offer(stack, page):
    """Options compare Fight / Full refund / Partial offer; the merchant edits the offer within bounds. Nothing is
    sent (the confirm step is opened and cancelled)."""
    page.goto(f"{stack.base}/cases/PP-DEMO-O")
    opts = page.locator("[data-testid=options]")
    opts.wait_for()
    text = opts.inner_text()
    assert "Fight" in text and "Full refund" in text and "Partial refund offer" in text
    assert opts.locator("li", has_text="Partial refund offer").locator("text=best").count() == 1
    offer = page.get_by_label("Offer amount")
    assert offer.input_value() == "80.00"
    assert page.locator("text=offer the buyer a partial refund of $80.00").count() == 1
    approve = page.locator("button:has-text('Approve')")

    offer.fill("5")
    page.wait_for_selector("text=The offer must be at least 20.00")
    assert approve.is_disabled()
    offer.fill("150")
    page.wait_for_selector("text=The offer can be at most 140.00")
    assert approve.is_disabled()
    offer.fill("60")
    assert approve.is_enabled()
    assert "offer 60.00 (30% of the amount)" in opts.inner_text()  # the partial row follows the edit
    # an edited amount never sends the old drafted note: the note is written for the final amount
    assert "refund of 60.00 USD" in page.locator("textarea").get_attribute("placeholder")

    approve.click()
    page.wait_for_selector("text=Send this offer to the buyer now?")
    page.locator("button:has-text('Cancel')").first.click()

    page.locator("button:has-text('Choose fight')").click()
    page.wait_for_selector("text=fight by escalating to a PayPal claim")
    assert page.get_by_label("Offer amount").count() == 0
    assert not real_errors(page)


# ---- Phase 14: analytics ----------------------------------------------------------------------------------------

def test_analytics_page_demo_data(stack, page):
    """KPIs, charts, the fact grid with grouping, Ask -> editable chips (the LLM call is stubbed with the real query
    endpoint's answer: the UI suite never calls an LLM), calibration. Demo data is badged."""
    import json
    import os
    import urllib.request

    def real_query(query):
        req = urllib.request.Request(f"http://localhost:{stack.api_port}/api/analytics/query", method="POST",
                                     data=json.dumps({"dataset": "demo", "query": query}).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    asked = {"filters": [{"column": "created_at", "op": "last_days", "value": "120"}], "group_by": ["reason"],
             "metric": "win_rate", "chart": "bar", "title": "Win rate by reason"}
    page.route("**/api/analytics/ask", lambda route: route.fulfill(status=200, content_type="application/json",
                                                                   body=json.dumps(real_query(asked))))
    page.goto(f"{stack.base}/analytics")
    page.wait_for_selector("[data-testid=demo-badge]")
    kpis = page.locator("[data-testid=kpis]").inner_text()
    for name in ("MONEY PROTECTED", "WIN RATE", "AGENT ACCURACY", "BEFORE DEADLINE", "YOUR OVERRIDES"):
        assert name in kpis.upper()
    page.wait_for_function("document.querySelectorAll('canvas').length >= 4")  # 3 charts + calibration

    grid = page.locator("[data-testid=facts-grid]")
    assert grid.locator(".ag-row").count() > 5
    page.get_by_label("Group the table by").select_option("reason")
    page.wait_for_function("document.querySelectorAll('[data-testid=facts-grid] .ag-row').length === 3")
    assert "Not received" in grid.inner_text()

    page.get_by_label("Ask a question about your disputes").fill("win rate by reason last 120 days")
    page.locator("button:has-text('Ask')").click()
    chips = page.locator("[data-testid=query-chips]")
    chips.wait_for()
    assert "Opened in the last" in chips.inner_text()
    assert page.get_by_label("Metric").input_value() == "win_rate"
    page.get_by_label("Remove filter Opened in the last 120 days").click()   # edited chips run the real query
    page.wait_for_function("!document.querySelector('[data-testid=query-chips]').innerText.includes('Opened')")
    page.get_by_label("Metric").select_option("money_protected")
    page.wait_for_selector("text=Money protected:")

    status = page.locator("[data-testid=calibration-status]").inner_text()
    assert "never used for real decisions" in status
    os.makedirs(os.environ.get("E2E_SHOTS", "/tmp"), exist_ok=True)
    page.screenshot(path=os.path.join(os.environ.get("E2E_SHOTS", "/tmp"), "analytics.png"), full_page=True)
    assert not real_errors(page)


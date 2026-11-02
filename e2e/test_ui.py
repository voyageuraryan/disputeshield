"""Merchant UI scenarios in a real browser (see README). Nothing is ever sent to PayPal."""

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
    assert rows.count() == 5  # A, B, C, R, E
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
    assert "recorded by you" in card.inner_text()
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

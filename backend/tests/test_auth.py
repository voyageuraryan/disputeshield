"""Log in with PayPal + merchant-only API protection."""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.auth import new_session, new_state, sign
from app.config import get_settings
from app.main import app
from tests.conftest import BASE, token_response

pytestmark = pytest.mark.real_auth

MERCHANT_ENDPOINTS = [
    ("GET", "/api/disputes"), ("GET", "/api/disputes/PP-X"), ("GET", "/disputes/PP-X/case"),
    ("GET", "/api/disputes/PP-X/case"), ("GET", "/disputes/PP-X/events"), ("GET", "/api/disputes/PP-X/events"),
    ("GET", "/api/evidence/1/source"), ("GET", "/decisions/1"), ("GET", "/api/decisions/1"),
    ("POST", "/decisions/1/approve"), ("POST", "/api/decisions/1/approve"), ("POST", "/api/decisions/1/reject"),
    ("POST", "/api/decisions/1/retry"), ("GET", "/api/decisions/1/evidence.pdf"),
    ("GET", "/api/settings"), ("PUT", "/api/settings"), ("GET", "/api/auth/me"),
    # store back-office: buyer emails and PayPal tracking writes are merchant-only
    ("GET", "/api/store/orders"), ("POST", "/api/store/orders/1/shipments"),
    ("POST", "/api/store/orders/1/shipments/1/delivery"),
    # phase 9A: troubleshooting status + invite are merchant-only
    ("GET", "/api/disputes/PP-X/troubleshooting"), ("POST", "/api/disputes/PP-X/troubleshooting/invite"),
]


@pytest.fixture
def client():
    from sqlalchemy.pool import StaticPool
    from sqlmodel import create_engine

    from app.db import create_db_and_tables, get_engine

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    app.dependency_overrides[get_engine] = lambda: eng
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.pop(get_engine, None)


@pytest.fixture
def env(monkeypatch):
    def set_(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    yield set_
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.mark.parametrize("method,path", MERCHANT_ENDPOINTS)
def test_merchant_endpoints_need_login(client, method, path):
    r = client.request(method, path, json={})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_public_endpoints_stay_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/store/products").status_code == 200
    assert client.get("/api/store/config").status_code == 200
    # buyer checkout stays public: reaches validation / PayPal config, never a login challenge
    assert client.post("/api/store/orders", json={}).status_code != 401
    assert client.post("/api/store/orders/X/capture").status_code != 401
    assert client.get("/api/auth/config").json() == {"mode": "paypal"}
    assert client.post("/webhooks/paypal", content=b"{}").status_code == 400  # signature check, not login


def test_valid_session_via_header_only(client):
    tok = new_session({"sub": "u1", "email": "merchant@business.example.com", "name": "Shop Owner"})
    assert client.get("/api/settings", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    # security review 2026-11-05: never in the URL (history, proxy logs); streams and files use fetch + the header
    assert client.get(f"/api/settings?token={tok}").status_code == 401
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["email"] == "merchant@business.example.com" and me["name"] == "Shop Owner" and me["expires_at"]


def test_bad_tokens_rejected(client):
    good = new_session({"sub": "u1", "email": "m@x"})
    body, sig = good.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["email"] = "attacker@x"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + "." + sig
    expired = sign({"kind": "session", "sub": "u1", "exp": int(time.time()) - 1})
    wrong_kind = new_state("/queue")  # a state token is not a session
    for bad in [forged, expired, wrong_kind, "garbage", "a.b"]:
        r = client.get("/api/settings", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401, bad
    assert "expired" in client.get("/api/settings", headers={"Authorization": f"Bearer {expired}"}).json()["detail"]


def test_missing_or_short_secret_is_503(client, env):
    env(AUTH_SECRET="short")
    assert client.get("/api/settings", headers={"Authorization": "Bearer x.y"}).status_code == 503


def test_auth_off_for_local_dev_but_never_on_render(client, env, monkeypatch):
    env(AUTH_MODE="off")
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/auth/config").json() == {"mode": "off"}
    monkeypatch.setenv("RENDER", "true")
    assert client.get("/api/settings").status_code == 401


# ---- Log in with PayPal flow -------------------------------------------------------------

def start(client, next_="/cases/PP-1"):
    r = client.get(f"/api/auth/paypal/start?next={next_}")
    assert r.status_code == 302
    u = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    return u, q


def fragment(r):
    loc = r.headers["location"]
    assert loc.startswith("http://localhost:5173/auth/callback#")
    return {k: v[0] for k, v in parse_qs(urlparse(loc).fragment).items()}


def test_start_redirects_to_paypal_sandbox(client):
    u, q = start(client)
    assert (u.scheme, u.netloc, u.path) == ("https", "www.sandbox.paypal.com", "/connect")
    assert q["flowEntry"] == "static" and q["client_id"] == "test-client" and q["response_type"] == "code"
    assert q["scope"] == "openid email" and q["redirect_uri"] == "http://localhost:8000/api/auth/paypal/callback"
    assert q["state"]


@pytest.mark.parametrize("evil", ["//evil.com", "https://evil.com", "evil", ""])
def test_next_cannot_redirect_elsewhere(client, evil):
    _, q = start(client, evil)
    from app.auth import verify
    assert verify(q["state"], "state")["next"] == "/queue"


def mock_paypal(router, email="merchant@business.example.com", userinfo_path="oauth2"):
    tok = router.post(f"{BASE}/v1/oauth2/token").respond(200, json={"access_token": "USER-AT", "token_type": "Bearer",
                                                                    "expires_in": 28800, "refresh_token": "R"})
    oidc = router.get(f"{BASE}/v1/identity/openidconnect/userinfo")
    oauth2 = router.get(f"{BASE}/v1/identity/oauth2/userinfo")
    info = {"user_id": "https://www.paypal.com/webapps/auth/identity/user/abc", "email": email, "name": "Shop Owner"}
    if userinfo_path == "oauth2":  # paypalv1.1 schema: emails list
        oauth2.respond(200, json={"user_id": info["user_id"], "name": "Shop Owner",
                                  "emails": [{"value": email, "primary": True, "confirmed": True}]})
    else:  # like the live sandbox: oauth2 refuses, openidconnect?schema=openid works
        oauth2.respond(401)
        oidc.mock(side_effect=lambda req: httpx.Response(200, json=info) if req.url.params.get("schema") == "openid"
                  else httpx.Response(401))
    return tok, oidc, oauth2


@respx.mock
def test_full_login(client):
    tok, _, oauth2 = mock_paypal(respx)
    _, q = start(client)
    r = client.get(f"/api/auth/paypal/callback?code=AUTHCODE&state={q['state']}")
    f = fragment(r)
    assert f["next"] == "/cases/PP-1" and "error" not in f
    req = tok.calls[0].request
    assert req.headers["Authorization"].startswith("Basic ")
    assert parse_qs(req.content.decode()) == {"grant_type": ["authorization_code"], "code": ["AUTHCODE"]}
    ui = oauth2.calls[0].request
    assert ui.headers["Authorization"] == "Bearer USER-AT" and ui.url.params["schema"] == "paypalv1.1"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {f['token']}"}).json()
    assert me["email"] == "merchant@business.example.com"


@respx.mock
def test_userinfo_tries_next_variant_after_401(client):
    """Live sandbox (2026-11-01): a valid token got 401 from one userinfo variant; another must be tried."""
    _, oidc, oauth2 = mock_paypal(respx, userinfo_path="openidconnect")
    _, q = start(client)
    f = fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))
    assert "token" in f, f
    assert oauth2.call_count == 1 and oidc.calls[-1].request.url.params["schema"] == "openid"


@respx.mock
def test_userinfo_all_variants_refuse(client):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json={"access_token": "AT", "expires_in": 1})
    oauth2 = respx.get(f"{BASE}/v1/identity/oauth2/userinfo").respond(401)
    oidc = respx.get(f"{BASE}/v1/identity/openidconnect/userinfo").respond(401)
    _, q = start(client)
    f = fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))
    assert f["error"] == "PayPal login failed; please try again"
    assert oauth2.call_count == 2 and oidc.call_count == 2  # all four variants tried


@respx.mock
def test_userinfo_server_error_not_retried(client):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json={"access_token": "AT", "expires_in": 1})
    oauth2 = respx.get(f"{BASE}/v1/identity/oauth2/userinfo").respond(500)
    _, q = start(client)
    assert "error" in fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))
    assert oauth2.call_count == 3  # the client's own GET retries (5xx is retried for reads), no other variant


@respx.mock
def test_not_allowed_account(client):
    mock_paypal(respx, email="someone@personal.example.com")
    _, q = start(client)
    f = fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))
    assert "token" not in f and "not allowed" in f["error"]


@respx.mock
def test_no_merchant_configured(client, env):
    env(MERCHANT_LOGIN_EMAILS="")
    mock_paypal(respx)
    _, q = start(client)
    assert "no merchant is configured" in fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))["error"]


@respx.mock
def test_state_is_single_use_signed_and_expires(client):
    mock_paypal(respx)
    _, q = start(client)
    assert "token" in fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))
    assert "already used" in fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))["error"]
    forged = q["state"][:-3] + "abc"
    assert "invalid" in fragment(client.get(f"/api/auth/paypal/callback?code=C&state={forged}"))["error"]
    old = sign({"kind": "state", "nonce": "n1", "next": "/queue", "exp": int(time.time()) - 5})
    assert "expired" in fragment(client.get(f"/api/auth/paypal/callback?code=C&state={old}"))["error"]


@respx.mock
def test_paypal_errors(client):
    ex = respx.post(f"{BASE}/v1/oauth2/token").respond(500, json={"name": "INTERNAL_SERVER_ERROR"})
    _, q = start(client)
    f = fragment(client.get(f"/api/auth/paypal/callback?code=C&state={q['state']}"))
    assert f["error"] == "PayPal login failed; please try again" and ex.call_count == 1  # code never re-sent
    f = fragment(client.get("/api/auth/paypal/callback?error=access_denied&error_description=Call%20+1-555%20now"))
    assert f["code"] == "cancelled" and "555" not in f["error"]  # PayPal's free text is never reflected
    assert "did not return a login code" in fragment(client.get("/api/auth/paypal/callback"))["error"]


def test_buyer_and_merchant_credentials_never_cross(client):
    """Phase 9A: the buyer's link token opens only the buyer page; the merchant session opens only merchant routes."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Dispute, Order, TroubleshootingSession
    from app.troubleshooting.access import new_token

    eng = app.dependency_overrides[get_engine]()
    token, token_hash = new_token()
    with Session(eng) as s:
        o = Order(amount=Decimal("89"), currency="USD", items=[{"sku": "LAMP-01", "name": "Smart LED desk lamp"}])
        s.add(o); s.commit(); s.refresh(o)
        d = Dispute(paypal_dispute_id="PP-X", order_id=o.id, reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                    status="OPEN", amount=Decimal("89"), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        row = TroubleshootingSession(dispute_id=d.id, order_id=o.id, product_name="Smart LED desk lamp",
                                     token_hash=token_hash, expires_at=datetime.now(timezone.utc) + timedelta(days=3))
        s.add(row); s.commit(); s.refresh(row)
        sid = row.id
    # the buyer page needs no merchant login, only the link token
    assert client.get(f"/api/help/{sid}", params={"t": token}).status_code == 200
    # a buyer token is useless on merchant routes, however it is presented
    for kw in ({"headers": {"Authorization": f"Bearer {token}"}}, {"params": {"token": token}},
               {"headers": {"X-Help-Token": token}}, {"params": {"t": token}}):
        assert client.get("/api/disputes/PP-X/troubleshooting", **kw).status_code == 401
        assert client.get("/api/disputes", **kw).status_code == 401
    # a valid merchant session is refused on the buyer route (it can't stand in for, or ride along with, the token)
    merchant = new_session({"sub": "u1", "email": "merchant@business.example.com", "name": "Shop Owner"})
    assert client.get(f"/api/help/{sid}", params={"t": token},
                      headers={"Authorization": f"Bearer {merchant}"}).status_code == 403
    assert client.get(f"/api/help/{sid}", params={"token": merchant}).status_code == 403
    assert client.get("/api/disputes/PP-X/troubleshooting",
                      headers={"Authorization": f"Bearer {merchant}"}).status_code == 200


def test_login_next_allows_app_paths_only():
    from app.api.auth import _safe_next

    for ok in ("/queue", "/cases/PP-D-27803", "/analytics?tab=kpis&x=1", "/help/7#step-2"):
        assert _safe_next(ok) == ok
    for bad in (None, "", "queue", "//evil.com", "/\\evil.com", "/\\\\evil.com", "/\t/evil.com", "/\n/evil.com",
                "https://evil.com", "/%0a", "/ x", "/" + "a" * 600):
        expected = "/queue" if bad != "/%0a" else "/%0a"  # percent-encoded text stays encoded: harmless
        assert _safe_next(bad) == expected, repr(bad)


def test_removing_an_email_ends_its_sessions_at_once(client, env):
    env(MERCHANT_LOGIN_EMAILS="merchant@business.example.com,judge@example.com")
    tok = new_session({"sub": "j1", "email": "judge@example.com", "name": "Judge"})
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/settings", headers=h).status_code == 200
    env(MERCHANT_LOGIN_EMAILS="merchant@business.example.com")                   # judging is over
    r = client.get("/api/settings", headers=h)
    assert r.status_code == 401 and "no longer allowed" in r.json()["detail"]


def test_login_is_rate_limited_per_ip(client, monkeypatch):
    from app import ratelimit
    from app.troubleshooting.access import RateLimiter

    monkeypatch.setattr(ratelimit, "EXEMPT", set())
    monkeypatch.setitem(ratelimit.LIMITS, "login", RateLimiter(capacity=2, refill_per_s=0.01))
    ip = {"X-Forwarded-For": "198.51.100.7"}
    codes = [client.get("/api/auth/paypal/start", headers=ip).status_code for _ in range(3)]
    assert codes == [302, 302, 429]
    assert client.get("/api/auth/paypal/start", headers={"X-Forwarded-For": "198.51.100.8"}).status_code == 302
    monkeypatch.setattr(ratelimit, "EXEMPT", {"testclient", "198.51.100.7"})  # this machine is never limited
    assert client.get("/api/auth/paypal/start", headers=ip).status_code == 302


@pytest.mark.real_auth
def test_logout_ends_every_session(client):
    """G167: Log out is server-side: the token (and any copy of it) stops working, a new login works."""
    import time as _t

    tok = new_session({"sub": "u1", "email": "merchant@business.example.com", "name": "Shop Owner"})
    other = new_session({"sub": "u1", "email": "merchant@business.example.com", "name": "Shop Owner"})
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/settings", headers=h).status_code == 200
    assert client.post("/api/auth/logout", headers=h).status_code == 204
    assert client.get("/api/settings", headers=h).status_code == 401
    assert client.get("/api/settings", headers={"Authorization": f"Bearer {other}"}).status_code == 401
    _t.sleep(0.01)
    fresh = new_session({"sub": "u1", "email": "merchant@business.example.com", "name": "Shop Owner"})
    assert client.get("/api/settings", headers={"Authorization": f"Bearer {fresh}"}).status_code == 200

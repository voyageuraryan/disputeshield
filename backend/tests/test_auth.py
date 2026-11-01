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
    assert client.get("/api/auth/config").json() == {"mode": "paypal"}
    assert client.post("/webhooks/paypal", content=b"{}").status_code == 400  # signature check, not login


def test_valid_session_via_header_or_query(client):
    tok = new_session({"sub": "u1", "email": "merchant@business.example.com", "name": "Shop Owner"})
    assert client.get("/api/settings", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.get(f"/api/settings?token={tok}").status_code == 200  # EventSource / PDF links
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
    assert fragment(client.get("/api/auth/paypal/callback?error=access_denied&error_description=User%20cancelled"))[
        "error"] == "User cancelled"
    assert "did not return a login code" in fragment(client.get("/api/auth/paypal/callback"))["error"]

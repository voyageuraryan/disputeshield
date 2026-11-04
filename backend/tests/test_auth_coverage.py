"""Self-check (2026-11-04): with real login on, EVERY merchant API route refuses a request without a session.
Walks the app's routes, so a new router mounted without require_merchant fails here."""

import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

# Public by design, listed as EXACT routes (a prefix would silently exempt any new route under it, e.g. /api/auth/me):
# the storefront, PayPal webhooks (signature-verified), the login flow, health.
PUBLIC = {"GET /api/store/config", "GET /api/store/products", "POST /api/store/orders",
          "POST /api/store/orders/{paypal_order_id}/capture", "POST /webhooks/paypal", "GET /api/auth/config",
          "GET /api/auth/paypal/start", "GET /api/auth/paypal/callback", "GET /health"}
# Guarded by something other than the merchant session, each checked by its own test below.
LINK_TOKEN = r"^/api/help/"   # the buyer's private link token
OWN_GUARD = r"^/admin/"       # X-Admin-Token (404 when disabled)


def _routes() -> list[tuple[str, str]]:
    return [(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops]


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture
def real_login(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "paypal")
    monkeypatch.setenv("AUTH_SECRET", "x" * 40)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.real_auth
def test_every_merchant_route_needs_a_session(real_login):
    # FastAPI 0.16x wraps included routers (app.routes doesn't list them); the OpenAPI schema lists every route
    # (routes with include_in_schema=False are aliases of listed ones).
    routes = _routes()
    assert len(routes) >= 55
    assert PUBLIC <= {f"{m} {p}" for m, p in routes}, "a PUBLIC entry no longer exists; update the list"
    leaks = []
    with TestClient(app) as c:
        for method, path in routes:
            if f"{method} {path}" in PUBLIC or re.match(LINK_TOKEN, path) or re.match(OWN_GUARD, path):
                continue
            r = c.request(method, _fill(path), json={})
            if r.status_code not in (401, 403):
                leaks.append(f"{method} {path} -> {r.status_code}")
    assert not leaks, leaks


@pytest.mark.real_auth
def test_mcp_needs_its_own_token_even_with_a_session(real_login):
    with TestClient(app) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
        assert c.get("/some/unknown/path").status_code == 404   # the catch-all MCP mount serves nothing else


@pytest.fixture
def empty_db():
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.db import get_session

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)

    def session_override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    yield eng
    app.dependency_overrides.pop(get_session, None)


@pytest.mark.real_auth
def test_buyer_help_routes_need_the_link_token(real_login, empty_db):
    help_routes = [(m, p) for m, p in _routes() if re.match(LINK_TOKEN, p)]
    assert len(help_routes) >= 8
    with TestClient(app) as c:
        for method, path in help_routes:
            r = c.request(method, _fill(path), json={})
            assert r.status_code in (403, 404, 422), f"{method} {path} -> {r.status_code}"
            assert r.status_code != 200


@pytest.mark.real_auth
def test_admin_routes_need_the_admin_token(real_login, monkeypatch):
    admin = [(m, p) for m, p in _routes() if re.match(OWN_GUARD, p)]
    assert admin
    with TestClient(app) as c:
        for method, path in admin:  # helpers off (no ADMIN_TOKEN): they don't exist
            assert c.request(method, _fill(path), json={}).status_code == 404
        monkeypatch.setenv("ADMIN_TOKEN", "a" * 40)
        get_settings.cache_clear()
        for method, path in admin:
            for headers in ({}, {"X-Admin-Token": "wrong"}):
                r = c.request(method, _fill(path), json={}, headers=headers)
                assert r.status_code == 403, f"{method} {path} {headers} -> {r.status_code}"


def test_api_docs_are_off_on_render(monkeypatch):
    from app.main import docs_enabled

    monkeypatch.delenv("RENDER", raising=False)
    assert docs_enabled()                      # local development keeps /docs
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    assert not docs_enabled()
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    assert docs_enabled()                      # an explicit demo deployment may show them
    get_settings.cache_clear()


def test_access_log_never_shows_tokens():
    import logging

    from app.main import _RedactTokenFilter

    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/events/disputes?token=SECRET.SIG&after=3", "1.1", 200), None)
    _RedactTokenFilter().filter(rec)
    line = rec.getMessage()
    assert "SECRET" not in line and "token=[redacted]" in line and "after=3" in line
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/help/7?t=LINKTOKEN", "1.1", 200), None)
    _RedactTokenFilter().filter(rec)
    assert "LINKTOKEN" not in rec.getMessage()

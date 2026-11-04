"""Self-check (2026-11-04): with real login on, EVERY merchant API route refuses a request without a session.
Walks the app's routes, so a new router mounted without require_merchant fails here."""

import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

# Public by design: the storefront, PayPal webhooks, login, the buyer's help page (link token), health.
PUBLIC = (r"^/api/store/config$", r"^/api/store/products$", r"^POST /api/store/orders$",
          r"^POST /api/store/orders/\{paypal_order_id\}/capture$", r"^/webhooks", r"^/api/auth", r"^/api/help", r"^/health$", r"^/store", r"^/docs",
          r"^/openapi", r"^/redoc")
# Admin helpers have their own X-Admin-Token guard (404 when disabled).
OWN_GUARD = (r"^/admin/",)


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
    routes = [(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops]
    assert len(routes) >= 55
    leaks = []
    with TestClient(app) as c:
        for method, path in routes:
            if any(re.match(p, path) or re.match(p, f"{method} {path}") for p in PUBLIC + OWN_GUARD):
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

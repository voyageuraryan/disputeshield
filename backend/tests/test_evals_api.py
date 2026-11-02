"""Phase 10B: GET /api/admin/evals/latest serves the latest eval report to signed-in merchants only."""

import json

import pytest
from fastapi.testclient import TestClient

from app.api import evals as evals_api
from app.main import app
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)


@pytest.fixture
def report(tmp_path, monkeypatch):
    path = tmp_path / "latest.json"
    monkeypatch.setattr(evals_api, "REPORT", path)
    return path


def test_latest_report_is_served(client, report):
    report.write_text(json.dumps({"generated": "2026-11-02T18:00:00+00:00", "gates": {"passed": True}}))
    r = client.get("/api/admin/evals/latest")
    assert r.status_code == 200 and r.json()["gates"]["passed"] is True
    assert r.headers["cache-control"] == "no-store"


def test_no_report_yet_is_404(client, report):
    assert client.get("/api/admin/evals/latest").status_code == 404


def test_broken_report_is_500_without_leaking_content(client, report):
    report.write_text("{not json")
    r = client.get("/api/admin/evals/latest")
    assert r.status_code == 500 and "not json" not in r.text


@pytest.mark.real_auth
def test_merchant_sign_in_required(report, monkeypatch):
    report.write_text("{}")
    monkeypatch.setenv("AUTH_MODE", "paypal")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with TestClient(app) as c:
            assert c.get("/api/admin/evals/latest").status_code == 401
            assert c.get("/api/admin/evals/latest", headers={"Authorization": "Bearer forged"}).status_code == 401
    finally:
        get_settings.cache_clear()


def test_committed_report_matches_the_report_format():
    """The committed latest.json (if any) has what the Trust page reads."""
    if not evals_api.REPORT.exists():
        pytest.skip("no committed report")
    data = json.loads(evals_api.REPORT.read_text())
    assert {"generated", "gates", "metrics", "confusion", "by_reason", "worst", "latency_s"} <= set(data)

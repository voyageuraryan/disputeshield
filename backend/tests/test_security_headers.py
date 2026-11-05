from fastapi.testclient import TestClient

from app.main import app


def test_api_responses_carry_security_headers():
    with TestClient(app) as c:
        for path in ("/health", "/api/store/products", "/does-not-exist"):
            r = c.get(path)
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]

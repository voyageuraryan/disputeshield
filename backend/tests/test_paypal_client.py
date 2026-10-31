import logging

import httpx
import pytest
import respx

from app.paypal.client import PayPalError
from tests.conftest import BASE, token_response


@respx.mock
async def test_token_is_fetched_once_and_cached(pp):
    tok = respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.get(f"{BASE}/v1/ping").respond(200, json={"ok": True})

    assert await pp.get("/v1/ping") == {"ok": True}
    assert await pp.get("/v1/ping") == {"ok": True}

    assert tok.call_count == 1
    req = tok.calls[0].request
    assert req.headers["Authorization"].startswith("Basic ")
    assert req.content == b"grant_type=client_credentials"
    assert api.calls[0].request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_token_refreshed_60s_before_expiry(pp, clock):
    tok = respx.post(f"{BASE}/v1/oauth2/token")
    tok.side_effect = [
        httpx.Response(200, json=token_response(expires_in=300, token="tok-1")),
        httpx.Response(200, json=token_response(expires_in=300, token="tok-2")),
    ]
    api = respx.get(f"{BASE}/v1/ping").respond(200, json={})

    await pp.get("/v1/ping")
    clock.now += 239  # 61s left: still valid
    await pp.get("/v1/ping")
    assert tok.call_count == 1
    clock.now += 2  # 59s left: refresh
    await pp.get("/v1/ping")
    assert tok.call_count == 2
    assert api.calls[-1].request.headers["Authorization"] == "Bearer tok-2"


@respx.mock
async def test_retries_429_then_succeeds(pp, sleeps):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.get(f"{BASE}/v1/ping")
    api.side_effect = [httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": 1})]

    assert await pp.get("/v1/ping") == {"ok": 1}
    assert api.call_count == 3
    assert sleeps == [0.5, 1.0]


@respx.mock
async def test_gives_up_after_two_retries_and_logs_debug_id(pp, sleeps, caplog):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.get(f"{BASE}/v1/ping").respond(
        500, json={"name": "INTERNAL_SERVER_ERROR", "message": "boom"}, headers={"Paypal-Debug-Id": "dbg-500"}
    )
    with caplog.at_level(logging.WARNING, logger="app.paypal"), pytest.raises(PayPalError) as ei:
        await pp.get("/v1/ping")

    assert api.call_count == 3
    assert ei.value.status_code == 500
    assert ei.value.debug_id == "dbg-500"
    assert "dbg-500" in caplog.text


@respx.mock
async def test_4xx_not_retried_debug_id_from_body(pp, sleeps, caplog):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.get(f"{BASE}/v1/ping").respond(
        404, json={"name": "RESOURCE_NOT_FOUND", "message": "nope", "debug_id": "dbg-body"}
    )
    with caplog.at_level(logging.ERROR, logger="app.paypal"), pytest.raises(PayPalError) as ei:
        await pp.get("/v1/ping")

    assert api.call_count == 1
    assert sleeps == []
    assert ei.value.debug_id == "dbg-body"
    assert "dbg-body" in caplog.text


@respx.mock
async def test_401_refreshes_token_once(pp):
    tok = respx.post(f"{BASE}/v1/oauth2/token")
    tok.side_effect = [
        httpx.Response(200, json=token_response(token="stale")),
        httpx.Response(200, json=token_response(token="fresh")),
    ]
    api = respx.get(f"{BASE}/v1/ping")
    api.side_effect = [httpx.Response(401, json={"error": "invalid_token"}), httpx.Response(200, json={"ok": 1})]

    assert await pp.get("/v1/ping") == {"ok": 1}
    assert api.calls[1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_post_sends_json_and_drops_none_params(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.post(f"{BASE}/v1/thing").respond(201, json={"id": "1"})
    g = respx.get(f"{BASE}/v1/q").respond(200, json={})

    assert await pp.post("/v1/thing", json={"a": 1}) == {"id": "1"}
    assert api.calls[0].request.content == b'{"a":1}'
    await pp.get("/v1/q", params={"a": 1, "b": None})
    assert dict(g.calls[0].request.url.params) == {"a": "1"}


async def test_missing_credentials_raises(clock):
    from app.config import Settings
    from app.paypal.client import PayPalClient

    c = PayPalClient(Settings(_env_file=None, paypal_client_id="", paypal_client_secret=""), clock=clock)
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT_ID"):
        await c.get_token()
    await c.aclose()


@respx.mock
async def test_post_without_request_id_not_retried_on_5xx(pp, sleeps):
    """A money-moving POST with no idempotency key must never be re-sent after a 5xx."""
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.post(f"{BASE}/v1/customer/disputes/D1/accept-claim").respond(500, json={"name": "INTERNAL_SERVER_ERROR"})
    with pytest.raises(PayPalError):
        await pp.post("/v1/customer/disputes/D1/accept-claim", json={})
    assert api.call_count == 1
    assert sleeps == []


@respx.mock
async def test_post_without_request_id_not_retried_on_transport_error(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.post(f"{BASE}/v1/x").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(httpx.ReadTimeout):
        await pp.post("/v1/x", json={})
    assert api.call_count == 1


@respx.mock
async def test_post_without_request_id_still_retried_on_429(pp, sleeps):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.post(f"{BASE}/v1/x")
    api.side_effect = [httpx.Response(429), httpx.Response(201, json={"ok": 1})]
    assert await pp.post("/v1/x", json={}) == {"ok": 1}
    assert api.call_count == 2


@respx.mock
async def test_post_with_request_id_retried_with_same_key(pp, sleeps):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.post(f"{BASE}/v2/checkout/orders")
    api.side_effect = [httpx.Response(502), httpx.ReadTimeout("t"), httpx.Response(201, json={"id": "O1"})]
    assert await pp.post("/v2/checkout/orders", json={}, request_id="create-1") == {"id": "O1"}
    assert api.call_count == 3
    assert {c.request.headers["PayPal-Request-Id"] for c in api.calls} == {"create-1"}


@respx.mock
async def test_get_retried_on_transport_error(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    api = respx.get(f"{BASE}/v1/ping")
    api.side_effect = [httpx.ConnectError("c"), httpx.Response(200, json={})]
    assert await pp.get("/v1/ping") == {}
    assert api.call_count == 2


@respx.mock
async def test_token_call_retried_on_5xx(pp):
    tok = respx.post(f"{BASE}/v1/oauth2/token")
    tok.side_effect = [httpx.Response(503), httpx.Response(200, json=token_response())]
    respx.get(f"{BASE}/v1/ping").respond(200, json={})
    await pp.get("/v1/ping")
    assert tok.call_count == 2

import json

import pytest
import respx

from app.scripts.register_webhook import ensure_webhook, write_env
from tests.conftest import BASE, token_response

HOOKS = f"{BASE}/v1/notifications/webhooks"
URL = "https://abc.trycloudflare.com/webhooks/paypal"


@respx.mock
async def test_creates_webhook_with_dispute_events(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(HOOKS).respond(200, json={"webhooks": []})
    create = respx.post(HOOKS).respond(201, json={"id": "WH1", "url": URL})
    assert await ensure_webhook(URL, pp) == ("WH1", "created")
    body = json.loads(create.calls[0].request.content)
    assert body == {"url": URL, "event_types": [{"name": "CUSTOMER.DISPUTE.CREATED"},
                                                {"name": "CUSTOMER.DISPUTE.RESOLVED"},
                                                {"name": "CUSTOMER.DISPUTE.UPDATED"}]}


@respx.mock
async def test_reuses_identical_url(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(HOOKS).respond(200, json={"webhooks": [{"id": "WH1", "url": URL}]})
    create = respx.post(HOOKS)
    assert await ensure_webhook(URL, pp) == ("WH1", "existing")
    assert create.call_count == 0


@respx.mock
async def test_repoints_old_tunnel_url(pp):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(HOOKS).respond(200, json={"webhooks": [
        {"id": "OTHER", "url": "https://shop.example/ipn"},
        {"id": "WH1", "url": "https://old.trycloudflare.com/webhooks/paypal"}]})
    patch = respx.patch(f"{HOOKS}/WH1").respond(200, json={"id": "WH1", "url": URL})
    assert await ensure_webhook(URL, pp) == ("WH1", "updated")
    assert json.loads(patch.calls[0].request.content) == [{"op": "replace", "path": "/url", "value": URL}]


async def test_rejects_bad_url(pp):
    for bad in ["http://x.test/webhooks/paypal", "https://x.test/other"]:
        with pytest.raises(ValueError):
            await ensure_webhook(bad, pp)


def test_write_env_replaces_or_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PAYPAL_CLIENT_ID=abc\nPAYPAL_WEBHOOK_ID=\nPOLL_DISPUTES=true\n")
    write_env("WH9", env)
    assert env.read_text() == "PAYPAL_CLIENT_ID=abc\nPAYPAL_WEBHOOK_ID=WH9\nPOLL_DISPUTES=true\n"
    env2 = tmp_path / ".env2"
    env2.write_text("A=1")
    write_env("WH9", env2)
    assert env2.read_text() == "A=1\nPAYPAL_WEBHOOK_ID=WH9\n"

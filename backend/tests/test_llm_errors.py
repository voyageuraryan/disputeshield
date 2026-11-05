"""Provider errors are safe to show (no account ids / raw bodies) and a short 429 is waited out once."""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from app.agents import llm

REAL_CALL = llm.structured_call  # captured before the autouse fake_llm fixture replaces it


class Out(BaseModel):
    ok: bool


def _status_error(code: int, body: dict, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.groq.com/x")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, json=body, headers=headers or {},
                                                                            request=req))


def test_http_errors_hide_provider_details():
    body = {"error": {"message": "Rate limit reached in organization `org_01SECRET` on tokens per minute"}}
    e = llm._http_error("groq", _status_error(429, body, {"retry-after": "9"}))
    assert "org_01SECRET" not in str(e) and "rate limited" in str(e)
    assert "org_01SECRET" in e.detail and e.retry_after == 9.0
    bad_key = llm._http_error("gemini", _status_error(401, {"error": {"message": "API key not valid"}}))
    assert str(bad_key) == "gemini: HTTP 401: API key rejected (check the key on the server)"


@pytest.mark.asyncio
async def test_short_rate_limit_is_waited_out_once(monkeypatch):
    calls, sleeps = [], []

    async def fake_call(**_kw):
        calls.append(1)
        if len(calls) == 1:
            raise llm.LLMError("groq: HTTP 429: rate limited", retry_after=3.0)
        return Out(ok=True)

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(llm, "providers", lambda: [("groq", "m", fake_call, "k")])
    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)
    out = await REAL_CALL(system="s", user="u", output_model=Out)
    assert out.ok and len(calls) == 2 and sleeps == [3.5]


@pytest.mark.asyncio
async def test_long_rate_limit_fails_fast(monkeypatch):
    async def fake_call(**_kw):
        raise llm.LLMError("groq: HTTP 429: rate limited", retry_after=600.0, detail="org_01SECRET")

    monkeypatch.setattr(llm, "providers", lambda: [("groq", "m", fake_call, "k")])
    with pytest.raises(llm.LLMError) as info:
        await REAL_CALL(system="s", user="u", output_model=Out)
    assert "org_01SECRET" not in str(info.value)


def test_groq_json_validate_failed_is_reported_as_model_output(monkeypatch):
    import asyncio

    class FakeResp:
        status_code = 400
        text = '{"error":{"message":"Failed to generate JSON","code":"json_validate_failed"}}'

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            req = httpx.Request("POST", "https://api.groq.com/x")
            resp = httpx.Response(400, text=FakeResp.text, request=req)
            resp.raise_for_status()

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeClient)
    with pytest.raises(llm.LLMError) as info:
        asyncio.run(llm.groq_call(system="s", user="u", output_model=Out, api_key="k", model="m"))
    assert "did not match" in str(info.value) and "json_validate_failed" in info.value.detail

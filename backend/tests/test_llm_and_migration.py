import json

import httpx
import pytest
import respx
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app.agents import llm as llm_mod
from app.agents.llm import GEMINI_URL, GROQ_URL, LLMError, strict_json_schema
from app.agents.llm import structured_call as real_structured_call  # captured before the autouse fake
from app.config import get_settings
from app.db import create_db_and_tables
from app.schemas import Recommendation

GOOD = {"action": "FIGHT", "confidence": "high", "reasoning": "Tracking shows delivery.",
        "cited_evidence_ids": [1, 2], "missing_items": [], "economic_note": "Worth fighting."}
GEMINI = GEMINI_URL.format(model="gemini-3.8-flash")


def groq_ok(payload=GOOD, finish="stop"):
    return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": finish,
                                                  "message": {"role": "assistant", "content": json.dumps(payload)}}]})


def gemini_ok(payload=GOOD, finish="STOP", thought=False):
    parts = ([{"text": "thinking...", "thought": True}] if thought else []) + [{"text": json.dumps(payload)}]
    return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": parts},
                                                     "finishReason": finish}]})


async def call():
    return await real_structured_call(system="SYS", user="USER", output_model=Recommendation)


@pytest.fixture
def env(monkeypatch):
    def set_(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    yield set_
    monkeypatch.undo()
    get_settings.cache_clear()


# ---- schema ------------------------------------------------------------------------

def test_strict_schema():
    s = strict_json_schema(Recommendation)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"]) == set(GOOD)
    assert "title" not in json.dumps(s).replace('"reasoning"', "")
    assert s["properties"]["action"]["enum"] == ["FIGHT", "SETTLE", "GATHER_MORE"]


# ---- Groq (primary) --------------------------------------------------------------------

@respx.mock
async def test_groq_success_request_shape():
    g = respx.post(GROQ_URL).mock(return_value=groq_ok())
    gem = respx.post(GEMINI)
    out = await call()
    assert out == Recommendation(**GOOD) and gem.call_count == 0
    req = g.calls[0].request
    assert req.headers["Authorization"] == "Bearer test-groq-key"
    body = json.loads(req.content)
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "Recommendation"
    assert rf["json_schema"]["schema"] == strict_json_schema(Recommendation)
    assert body["reasoning_effort"] == "medium" and body["include_reasoning"] is False
    assert llm_mod.last_provider.get() == "groq:openai/gpt-oss-120b"


# ---- fallback to Gemini -----------------------------------------------------------------

@pytest.mark.parametrize("groq_response", [
    httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "Rate limit reached"}}),
    httpx.Response(503, json={"error": {"message": "over capacity"}}),
    httpx.Response(400, json={"error": {"message": "Generated JSON does not match the expected schema."}}),
    groq_ok({"action": "MAYBE"}),           # parses as JSON but not as a Recommendation
    groq_ok(GOOD, finish="length"),         # truncated
])
@respx.mock
async def test_groq_failure_falls_back_to_gemini(groq_response):
    respx.post(GROQ_URL).mock(return_value=groq_response)
    gem = respx.post(GEMINI).mock(return_value=gemini_ok())
    out = await call()
    assert out.action == "FIGHT" and gem.call_count == 1
    assert llm_mod.last_provider.get() == "gemini:gemini-3.8-flash"


@respx.mock
async def test_groq_network_error_falls_back():
    respx.post(GROQ_URL).mock(side_effect=httpx.ConnectError("down"))
    respx.post(GEMINI).mock(return_value=gemini_ok())
    assert (await call()).action == "FIGHT"


@respx.mock
async def test_gemini_request_shape_and_thought_parts_skipped(env):
    env(LLM_PROVIDERS="gemini")
    gem = respx.post(GEMINI).mock(return_value=gemini_ok(thought=True))
    assert (await call()).action == "FIGHT"
    req = gem.calls[0].request
    assert req.headers["x-goog-api-key"] == "test-gemini-key" and "key=" not in str(req.url)
    body = json.loads(req.content)
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseJsonSchema"] == strict_json_schema(Recommendation)
    assert "responseSchema" not in cfg  # deprecated field


@pytest.mark.parametrize("gemini_response,match", [
    (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "prompt blocked"),
    (gemini_ok(finish="MAX_TOKENS"), "finishReason=MAX_TOKENS"),
    (gemini_ok(finish="SAFETY"), "finishReason=SAFETY"),
    (httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}), "HTTP 429"),
])
@respx.mock
async def test_both_fail_raises_with_both_reasons(gemini_response, match, monkeypatch):
    monkeypatch.setattr(llm_mod, "OVERLOAD_WAIT_S", 0)
    respx.post(GROQ_URL).mock(return_value=httpx.Response(429, json={"error": {}}))
    gem = respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/v1beta/models/.+:generateContent")
    gem.mock(return_value=gemini_response)
    with pytest.raises(LLMError, match=match) as ei:
        await call()
    assert "groq: HTTP 429" in str(ei.value)
    assert gem.call_count == 2  # both default Gemini models tried
    assert llm_mod.last_provider.get() is None


# ---- configuration --------------------------------------------------------------------

@respx.mock
async def test_provider_without_key_is_skipped(env):
    env(GROQ_API_KEY="")
    g = respx.post(GROQ_URL)
    respx.post(GEMINI).mock(return_value=gemini_ok())
    assert (await call()).action == "FIGHT" and g.call_count == 0


async def test_no_keys_configured(env):
    env(GROQ_API_KEY="", GEMINI_API_KEY="")
    with pytest.raises(LLMError, match="no LLM provider configured"):
        await call()


async def test_unknown_provider_name(env):
    env(LLM_PROVIDERS="groq,openai")
    with pytest.raises(LLMError, match="unknown LLM provider 'openai'"):
        await call()


@respx.mock
async def test_order_and_models_configurable(env):
    env(LLM_PROVIDERS="gemini,groq", GEMINI_MODEL="gemini-2.5-flash", GROQ_MODEL="qwen/qwen3.8-27b")
    gem = respx.post(GEMINI_URL.format(model="gemini-2.5-flash")).mock(return_value=httpx.Response(500))
    g = respx.post(GROQ_URL).mock(return_value=groq_ok())
    assert (await call()).action == "FIGHT" and gem.call_count == 1
    body = json.loads(g.calls[0].request.content)
    assert body["model"] == "qwen/qwen3.8-27b" and "reasoning_effort" not in body  # gpt-oss-only params


# ---- migration (unchanged) -------------------------------------------------------------

def test_migration_adds_new_nullable_columns(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        c.execute(text('CREATE TABLE disputes (id INTEGER PRIMARY KEY, paypal_dispute_id VARCHAR NOT NULL, '
                       'transaction_id VARCHAR, order_id INTEGER, reason VARCHAR NOT NULL, stage VARCHAR, '
                       'status VARCHAR NOT NULL, amount NUMERIC(12,2) NOT NULL, currency VARCHAR(3) NOT NULL, '
                       'seller_response_due DATETIME, raw JSON)'))
        c.execute(text("INSERT INTO disputes (paypal_dispute_id, reason, status, amount, currency) "
                       "VALUES ('PP-1', 'X', 'OPEN', 1, 'USD')"))
    create_db_and_tables(eng)
    create_db_and_tables(eng)
    assert "agent_status" in {c["name"] for c in inspect(eng).get_columns("disputes")}
    with eng.connect() as c:
        assert c.execute(text("SELECT paypal_dispute_id, agent_status FROM disputes")).one() == ("PP-1", None)


@respx.mock
async def test_second_gemini_model_when_first_overloaded(monkeypatch):
    monkeypatch.setattr(llm_mod, "OVERLOAD_WAIT_S", 0)
    respx.post(GROQ_URL).mock(return_value=httpx.Response(429, json={"error": {}}))
    busy = respx.post(GEMINI).mock(return_value=httpx.Response(
        503, json={"error": {"code": 503, "message": "This model is currently experiencing high demand.",
                             "status": "UNAVAILABLE"}}))
    ok = respx.post(GEMINI_URL.format(model="gemini-2.5-flash")).mock(return_value=gemini_ok())
    assert (await call()).action == "FIGHT"
    assert busy.call_count == 2 and ok.call_count == 1  # one quick retry, then the next model
    assert llm_mod.last_provider.get() == "gemini:gemini-2.5-flash"


@respx.mock
async def test_gemini_overload_retried_once_on_same_model(monkeypatch):
    monkeypatch.setattr(llm_mod, "OVERLOAD_WAIT_S", 0)
    respx.post(GROQ_URL).mock(return_value=httpx.Response(429, json={"error": {}}))
    busy = httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})
    first = respx.post(GEMINI).mock(side_effect=[busy, gemini_ok()])
    second = respx.post(GEMINI_URL.format(model="gemini-2.5-flash"))
    assert (await call()).action == "FIGHT"
    assert first.call_count == 2 and second.call_count == 0
    assert llm_mod.last_provider.get() == "gemini:gemini-3.8-flash"


def test_strict_schema_keeps_fields_named_title():
    """Regression (found live, Phase 9B): fields called "title" were deleted from the schema but still required,
    so Groq and Gemini rejected every troubleshooting plan."""
    from pydantic import BaseModel

    from app.agents.llm import strict_json_schema

    class Item(BaseModel):
        title: str
        why: str

    class Plan(BaseModel):
        title: str
        items: list[Item]

    schema = strict_json_schema(Plan)
    assert set(schema["properties"]) == {"title", "items"} and schema["required"] == ["title", "items"]
    item = schema["$defs"]["Item"]
    assert set(item["properties"]) == {"title", "why"} and item["required"] == ["title", "why"]
    assert "title" not in item and "title" not in schema  # the annotations are still stripped
    assert item["additionalProperties"] is False

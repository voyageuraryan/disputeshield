"""LLM access: Groq (primary) -> Google Gemini (fallback), structured output into a Pydantic model.

Both are called over plain httpx (no vendor SDKs). Checked against the docs (2026-10-31):

Groq   POST https://api.groq.com/openai/v1/chat/completions  (Authorization: Bearer <key>)
       response_format {"type": "json_schema", "json_schema": {"name", "schema", "strict": true}}
       Strict mode: openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.8-27b (and
       meta-models/muse-spark-open, Enterprise). Strict needs every property in `required`
       and `additionalProperties: false`. gpt-oss takes reasoning_effort low/medium/high;
       include_reasoning=false keeps reasoning out of the reply. Answer: choices[0].message.content.
       Free plan (gpt-oss-120b): 30 RPM, 1K RPD, 8K TPM, 200K TPD; 429 carries retry-after.

Gemini POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
       (x-goog-api-key header). generationConfig.responseMimeType "application/json" +
       responseJsonSchema (responseSchema is deprecated). Answer: candidates[0].content.parts[].text
       (parts marked "thought" are skipped). finishReason STOP = complete.
       Free tier: content may be used by Google to improve its products.

Any provider failure (HTTP error, 429, refusal/block, truncation, JSON that doesn't match the
model) moves on to the next provider; if all fail, LLMError. The analyst then retries once and
finally marks the case NEEDS_HUMAN (rule 7).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextvars import ContextVar
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.tracing import observe
from app.config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT_S = 60.0
MAX_OUTPUT_TOKENS = 4096  # keeps a request inside Groq's free 8K tokens-per-minute budget
OVERLOAD_RETRIES = 1      # Gemini 503 "high demand": one quick retry of the same model
OVERLOAD_WAIT_S = 1.5

# Which provider/model answered the most recent structured_call in this task (for the timeline).
last_provider: ContextVar[str | None] = ContextVar("last_provider", default=None)
# Token use of the last successful call, as the provider reported it: {"input": n, "output": n} (evals report it).
last_usage: ContextVar[dict | None] = ContextVar("last_usage", default=None)


class LLMError(Exception):
    pass


class StructuredLLM(Protocol):
    async def __call__(self, *, system: str, user: str, output_model: type[T]) -> T: ...


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema made strict-mode friendly: no titles, every property required,
    additionalProperties false on every object."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("title", None)  # the schema's own "title" annotation
            node.pop("default", None)  # strict mode: every field is required anyway ("default" isn't supported)
            # length/size/pattern bounds aren't reliably accepted by strict modes; Pydantic still enforces them
            # when the answer is parsed (and invalid output is retried once, rule 7)
            for k in ("maxLength", "minLength", "maxItems", "minItems", "pattern"):
                if isinstance(node.get(k), (int, str)) and "type" in node:
                    node.pop(k)
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for k, v in node.items():
                if k in ("properties", "$defs") and isinstance(v, dict):
                    # a mapping of NAME -> schema: a field may itself be called "title", so don't treat
                    # the mapping as a schema (that deleted fields named "title" while still requiring them)
                    for sub in v.values():
                        walk(sub)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


def _parse(text: str | None, output_model: type[T], provider: str) -> T:
    if not text:
        raise LLMError(f"{provider}: empty response")
    try:
        return output_model.model_validate_json(text)
    except ValidationError as exc:
        raise LLMError(f"{provider}: output did not match {output_model.__name__}: {exc}") from exc


def _http_error(provider: str, exc: Exception) -> LLMError:
    if isinstance(exc, httpx.HTTPStatusError):
        r = exc.response
        hint = f" retry-after={r.headers['retry-after']}s" if r.status_code == 429 and "retry-after" in r.headers else ""
        try:
            detail = r.json()
        except ValueError:
            detail = r.text[:300]
        return LLMError(f"{provider}: HTTP {r.status_code}{hint}: {detail}")
    return LLMError(f"{provider}: {type(exc).__name__}: {exc}")


async def groq_call(*, system: str, user: str, output_model: type[T], api_key: str, model: str) -> T:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": output_model.__name__, "schema": strict_json_schema(output_model), "strict": True}},
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "temperature": get_settings().llm_temperature,  # documented 0-2 (default 1)
        "seed": get_settings().llm_seed,                # documented: best-effort determinism
    }
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "medium"
        body["include_reasoning"] = False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            r = await http.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
            r.raise_for_status()
    except httpx.HTTPError as exc:
        raise _http_error("groq", exc) from exc
    data = r.json()
    u = data.get("usage") or {}
    last_usage.set({"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")} if u else None)
    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise LLMError("groq: output truncated (finish_reason=length)")
    return _parse((choice.get("message") or {}).get("content"), output_model, "groq")


async def gemini_call(*, system: str, user: str, output_model: type[T], api_key: str, model: str) -> T:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "responseJsonSchema": strict_json_schema(output_model),
                             "maxOutputTokens": MAX_OUTPUT_TOKENS * 2,
                             # temperature is ignored on Gemini 3.6 Flash+; the seed still applies
                             "temperature": get_settings().llm_temperature, "seed": get_settings().llm_seed},
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
            for attempt in range(OVERLOAD_RETRIES + 1):
                r = await http.post(GEMINI_URL.format(model=model), json=body, headers={"x-goog-api-key": api_key})
                # 503 "This model is currently experiencing high demand" is usually momentary (seen live on
                # gemini-3.8-flash): wait briefly and retry the same model once before moving on.
                if r.status_code == 503 and attempt < OVERLOAD_RETRIES:
                    log.info("gemini %s overloaded (503); retrying in %.1fs", model, OVERLOAD_WAIT_S)
                    await asyncio.sleep(OVERLOAD_WAIT_S)
                    continue
                break
            r.raise_for_status()
    except httpx.HTTPError as exc:
        raise _http_error("gemini", exc) from exc
    data = r.json()
    u = data.get("usageMetadata") or {}
    last_usage.set({"input": u.get("promptTokenCount"), "output": u.get("candidatesTokenCount")} if u else None)
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise LLMError(f"gemini: prompt blocked ({block})")
    cand = (data.get("candidates") or [{}])[0]
    finish = cand.get("finishReason")
    if finish and finish != "STOP":
        raise LLMError(f"gemini: finishReason={finish}")
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [] if not p.get("thought"))
    return _parse(text, output_model, "gemini")


def providers() -> list[tuple[str, str, Any, str]]:
    """Configured (provider, model) attempts in order: (name, model, call, api_key).
    GROQ_MODEL / GEMINI_MODEL may list several comma-separated models, tried in order (the newest
    free Gemini model often returns 503 "high demand"). Providers without a key are skipped."""
    s = get_settings()
    table = {
        "groq": (s.groq_model, groq_call, s.groq_api_key.get_secret_value()),
        "gemini": (s.gemini_model, gemini_call, s.gemini_api_key.get_secret_value()),
    }
    out = []
    for name in (p.strip() for p in s.llm_providers.split(",") if p.strip()):
        if name not in table:
            raise LLMError(f"unknown LLM provider {name!r} in LLM_PROVIDERS")
        models, call, key = table[name]
        if key:
            out.extend((name, m.strip(), call, key) for m in models.split(",") if m.strip())
    return out


@observe(type="llm")
async def structured_call(*, system: str, user: str, output_model: type[T]) -> T:
    chain = providers()
    if not chain:
        raise LLMError("no LLM provider configured: set GROQ_API_KEY and/or GEMINI_API_KEY in backend/.env")
    errors = []
    for name, model, call, key in chain:
        try:
            result = await call(system=system, user=user, output_model=output_model, api_key=key, model=model)
        except LLMError as exc:
            log.warning("LLM provider %s (%s) failed: %s", name, model, exc)
            errors.append(str(exc))
            continue
        last_provider.set(f"{name}:{model}")
        log.info("LLM answered by %s (%s)", name, model)
        return result
    last_provider.set(None)
    raise LLMError(" | ".join(errors))

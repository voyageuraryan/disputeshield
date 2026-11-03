"""The eval judge: Gemini behind DeepEval's custom-model interface (deepeval.com/guides/guides-using-custom-llms).

DeepEvalBaseLLM contract (deepeval 4.3.2): load_model(), generate(prompt, schema=None), async a_generate(prompt,
schema=None), get_model_name(). With a schema, return an instance of that Pydantic schema; without, the raw text.
Passed as model= to every LLM-judged metric.

Why Gemini: the model under test is Groq gpt-oss-120b (the analyst / note writer); a judge from another family
avoids self-preference. The eval run pins LLM_PROVIDERS=groq so the model under test never silently falls back to
Gemini (conftest refuses to start if it could).

Request = the generateContent call app/agents/llm.py already uses (verified live): systemInstruction-free,
contents[user], generationConfig {responseMimeType: application/json, responseJsonSchema} when a schema is given.
Answers are cached on disk (judge model, prompt, schema); calls are spaced (EVALS_JUDGE_INTERVAL_S, default 7 s
for the free tier) and retried on 429/503, then the next model in EVALS_JUDGE_MODEL (default 2.5-flash, 3.8-flash).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Any

import httpx
from deepeval.models import DeepEvalBaseLLM
from pydantic import BaseModel, ValidationError

from app.agents.llm import GEMINI_URL
from app.config import get_settings
from evals.cache import DiskCache, digest

TIMEOUT_S = 45.0
# Stable model first: live, gemini-3.8-flash often answered 503 / timed out on the free tier (PROGRESS session 8,
# Phase 9B/9D runs) while 2.5-flash answered; the newer one stays as a fallback.
DEFAULT_JUDGE_MODELS = "gemini-2.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite"


class JudgeError(RuntimeError):
    pass


class GeminiJudge(DeepEvalBaseLLM):
    def __init__(self, models: list[str] | None = None, cache: DiskCache | None = None, interval: float | None = None,
                 role: str = "judge"):
        s = get_settings()
        names = models or [m.strip() for m in (os.getenv("EVALS_JUDGE_MODEL") or DEFAULT_JUDGE_MODELS).split(",")
                           if m.strip()]
        if not names:
            raise JudgeError("no judge model configured (EVALS_JUDGE_MODEL / GEMINI_MODEL)")
        self.models = names
        self.role = role  # judge / simulator: each may have its own key (EVALS_*_GEMINI_API_KEY), no failover
        self.api_key = s.gemini_key_for(role)
        self.cache = cache if cache is not None else DiskCache("judge")
        self.interval = float(os.getenv("EVALS_JUDGE_INTERVAL_S", "7")) if interval is None else interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0
        self.exhausted: set[str] = set()  # models whose DAILY quota is used up: skipped for the rest of the run
        super().__init__(model=names[0])

    # ---- DeepEvalBaseLLM -----------------------------------------------------------------------------------
    def load_model(self) -> "GeminiJudge":
        return self

    def get_model_name(self) -> str:
        return f"gemini-judge ({self.models[0]})"

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        text = self._cached_text(prompt, schema)
        return self._shape(text, schema)

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        text = await asyncio.to_thread(self._cached_text, prompt, schema)
        return self._shape(text, schema)

    # ---- internals ------------------------------------------------------------------------------------------
    @staticmethod
    def _shape(text: str, schema: type[BaseModel] | None) -> Any:
        if schema is None:
            return text
        try:
            return schema.model_validate_json(text)
        except ValidationError:
            m = re.search(r"\{.*\}", text, re.DOTALL)  # tolerate a fenced or prefixed JSON answer
            if m:
                return schema.model_validate(json.loads(m.group(0)))
            raise

    def _cached_text(self, prompt: str, schema: type[BaseModel] | None) -> str:
        key = digest(self.models[0], prompt, schema.__name__ if schema else "-",
                     json.dumps(schema.model_json_schema(), sort_keys=True) if schema else "")[:40]
        hit = self.cache.get(key)
        if hit is not None:
            return hit["text"]
        text = self._call(prompt, schema)
        self.cache.set(key, {"model": self.models[0], "schema": schema.__name__ if schema else None, "text": text})
        return text

    def _space(self) -> None:
        with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _body(self, prompt: str, schema: type[BaseModel] | None, with_schema: bool) -> dict:
        cfg: dict = {"temperature": 0, "maxOutputTokens": 4096}
        if schema is not None:
            cfg["responseMimeType"] = "application/json"
            if with_schema:
                cfg["responseJsonSchema"] = schema.model_json_schema()
        return {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}

    def _call(self, prompt: str, schema: type[BaseModel] | None) -> str:
        if not self.api_key:
            raise JudgeError("GEMINI_API_KEY is not set: the LLM-judged metrics need it")
        errors = []
        with httpx.Client(timeout=TIMEOUT_S) as http:
            for model in self.models:
                if model in self.exhausted:
                    errors.append(f"{model}: daily quota used up")
                    continue
                with_schema = True
                for attempt in range(4):
                    self._space()
                    self.calls += 1
                    try:
                        r = http.post(GEMINI_URL.format(model=model), json=self._body(prompt, schema, with_schema),
                                      headers={"x-goog-api-key": self.api_key})
                    except httpx.HTTPError as exc:
                        errors.append(f"{model}: {type(exc).__name__}")
                        break
                    if r.status_code == 400 and with_schema and schema is not None:
                        with_schema = False  # a schema feature Gemini doesn't support: JSON mode + validation
                        continue
                    if r.status_code == 429 and "exceeded your current quota" in r.text:
                        # a DAILY quota: waiting and retrying can't help (seen live: minutes lost per metric)
                        self.exhausted.add(model)
                        errors.append(f"{model}: HTTP 429 (daily quota used up)")
                        break
                    if r.status_code in (429, 503) and attempt < 3:
                        time.sleep(min(float(r.headers.get("retry-after") or 10 * (attempt + 1)), 60))
                        continue
                    if r.status_code != 200:
                        # the start of Gemini's message says which quota (per minute / per day); no secrets in it
                        msg = ((r.json() if "json" in r.headers.get("content-type", "") else {}).get("error") or {}
                               ).get("message", "")
                        errors.append(f"{model}: HTTP {r.status_code}" + (f" ({msg[:160]})" if msg else ""))
                        break
                    cand = (r.json().get("candidates") or [{}])[0]
                    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or []
                                   if not p.get("thought"))
                    if text:
                        return text
                    errors.append(f"{model}: empty answer ({cand.get('finishReason')})")
                    break
        raise JudgeError("judge unavailable: " + " | ".join(errors))


class NoCallModel(DeepEvalBaseLLM):
    """For metrics that need a model object but never call it (ToolCorrectness without available_tools) in
    --no-judge runs; DeepEval would otherwise default to OpenAI. Raises if anything does call it."""

    def __init__(self):
        super().__init__(model="no-llm")

    def load_model(self) -> "NoCallModel":
        return self

    def get_model_name(self) -> str:
        return "no-llm (deterministic)"

    def generate(self, prompt: str, schema=None):  # noqa: ANN001
        raise JudgeError("this metric was expected to be deterministic but asked a model")

    async def a_generate(self, prompt: str, schema=None):  # noqa: ANN001
        return self.generate(prompt, schema)

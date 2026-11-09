"""Check every LLM key in backend/.env with one tiny request each, and spot .env mistakes. Never prints a key.

Usage: cd backend && .venv/bin/python -m app.scripts.check_llm_keys [--judge-models gemini-3.8-flash,gemini-3.5-flash-lite]

What it checks
- backend/.env: a key set twice (the LAST line wins, silently), quotes, spaces or commas inside a key
- GROQ_API_KEY against GROQ_MODEL
- GEMINI_API_KEY, RESEARCH_GEMINI_API_KEY, EVALS_JUDGE_GEMINI_API_KEY, EVALS_SIMULATOR_GEMINI_API_KEY against each
  Gemini model the evals / app would use (a key that's unset falls back to GEMINI_API_KEY, as in the app)
Each answer is one of: OK, quota used up (429), key rejected (400/401/403), model not available (404), other.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter

import httpx

from app.config import ENV_FILE, get_settings

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
KEY_NAMES = ("GROQ_API_KEY", "GEMINI_API_KEY", "RESEARCH_GEMINI_API_KEY", "EVALS_JUDGE_GEMINI_API_KEY",
             "EVALS_SIMULATOR_GEMINI_API_KEY", "TAVILY_API_KEY")


def fingerprint(value: str) -> str:
    """Enough to tell keys apart without showing them."""
    return f"{value[:4]}…({len(value)} chars, #{hashlib.sha256(value.encode()).hexdigest()[:6]})" if value else "(empty)"


def env_problems(path=ENV_FILE) -> list[str]:
    if not path.exists():
        return [f"{path} not found"]
    names: Counter = Counter()
    out = []
    for n, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (p.strip() for p in line.split("=", 1))
        names[name] += 1
        if name in KEY_NAMES and value:
            v = value.strip("\"'")
            if value != v:
                out.append(f"line {n}: {name} is in quotes; remove them")
            if any(c in v for c in " ,;"):
                out.append(f"line {n}: {name} contains spaces or commas: one key per variable (the app reads one key)")
    for name, count in names.items():
        if count > 1:
            out.append(f"{name} is set {count} times: only the LAST line counts; keep one line")
    return out


def classify(r: httpx.Response) -> str:
    if r.status_code == 200:
        return "OK"
    text = r.text[:400].replace("\n", " ")
    kind = {429: "quota used up / rate limited", 400: "key rejected or bad request", 401: "key rejected",
            403: "key not allowed", 404: "model not available for this key"}.get(r.status_code, "error")
    return f"{r.status_code} {kind}: {text[:200]}"


def check_groq(key: str, model: str) -> str:
    try:
        r = httpx.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30,
                       json={"model": model, "messages": [{"role": "user", "content": "Reply OK"}],
                             "max_completion_tokens": 5})
    except httpx.HTTPError as exc:
        return f"connection failed: {type(exc).__name__}"
    return classify(r)


def check_gemini(key: str, model: str) -> str:
    try:
        r = httpx.post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": key}, timeout=30,
                       json={"contents": [{"parts": [{"text": "Reply OK"}]}], "generationConfig": {"maxOutputTokens": 5}})
    except httpx.HTTPError as exc:
        return f"connection failed: {type(exc).__name__}"
    return classify(r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judge-models", default=os.getenv("EVALS_JUDGE_MODEL") or "gemini-3.8-flash,gemini-3.5-flash-lite")
    args = ap.parse_args(argv)
    s = get_settings()
    bad = 0

    print(f"== {ENV_FILE}")
    problems = env_problems()
    for p in problems:
        print("  PROBLEM:", p)
    print("  no problems" if not problems else "")
    bad += len(problems)

    groq = s.groq_api_key.get_secret_value()
    print(f"\n== Groq  GROQ_API_KEY {fingerprint(groq)}")
    if groq:
        for model in [m.strip() for m in s.groq_model.split(",") if m.strip()]:
            res = check_groq(groq, model)
            bad += res != "OK"
            print(f"  {model}: {res}")
    else:
        print("  not set")
        bad += 1

    main_key = s.gemini_api_key.get_secret_value()
    roles = {"GEMINI_API_KEY (app)": main_key,
             "RESEARCH_GEMINI_API_KEY": s.research_gemini_api_key.get_secret_value() or main_key,
             "EVALS_JUDGE_GEMINI_API_KEY": s.evals_judge_gemini_api_key.get_secret_value() or main_key,
             "EVALS_SIMULATOR_GEMINI_API_KEY": s.evals_simulator_gemini_api_key.get_secret_value() or main_key}
    app_models = [m.strip() for m in s.gemini_model.split(",") if m.strip()]
    judge_models = [m.strip() for m in args.judge_models.split(",") if m.strip()]
    for role, key in roles.items():
        models = judge_models if "JUDGE" in role else app_models
        print(f"\n== Gemini  {role} {fingerprint(key)}")
        if not key:
            print("  not set")
            continue
        if not key.startswith(("AIza", "AQ.")):
            print("  NOTE: Gemini API keys from aistudio.google.com/apikey start with 'AIza' (older) or 'AQ.' (newer). "
                  "This one doesn't: if it is rejected, create a new key there.")
        for model in models:
            res = check_gemini(key, model)
            bad += res != "OK"
            print(f"  {model}: {res}")
    print("\nAll good." if not bad else f"\n{bad} problem(s) above.")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

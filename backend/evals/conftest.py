"""Eval suite set-up. Run from backend/:  scripts/run_evals.sh [--refresh] [--only id1,id2] [--no-judge]

Environment is fixed BEFORE the app is imported:
- the model under test is pinned to Groq (LLM_PROVIDERS=groq; EVALS_MODEL_PROVIDERS to change it) so a Groq
  outage never silently swaps in Gemini, which is the judge (self-preference);
- a throwaway in-memory database and artifacts under evals/.cache/ (never the real DB; nothing goes to PayPal);
- DeepEval telemetry off.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).parent
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["LLM_PROVIDERS"] = os.getenv("EVALS_MODEL_PROVIDERS", "groq")
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["CHECKPOINT_DB"] = str(HERE / ".cache" / "checkpoints.db")
os.environ["ARTIFACTS_DIR"] = str(HERE / ".cache" / "artifacts")
os.environ["POLL_DISPUTES"] = "false"
os.environ.setdefault("LLM_REDACT_PII", "true")
# DeepEval cancels a test case's metrics after 180 s by default; the judge is deliberately spaced for the free tier
# (EVALS_JUDGE_INTERVAL_S) and may retry, so give each case room (overridable).
os.environ.setdefault("DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE", "900")

import asyncio  # noqa: E402

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


def pytest_addoption(parser):
    parser.addoption("--refresh", action="store_true", help="re-run the model under test and the judge (ignore cache)")
    parser.addoption("--only", default="", help="comma-separated golden ids (or id prefixes) to run")
    parser.addoption("--no-judge", action="store_true", help="deterministic metrics only (no Gemini judge calls)")


def pytest_configure(config):
    if config.getoption("--refresh"):
        os.environ["EVALS_REFRESH"] = "1"
    if config.getoption("--only"):
        os.environ["EVALS_ONLY"] = config.getoption("--only")
    if config.getoption("--no-judge"):
        os.environ["EVALS_JUDGE"] = "0"
    s = get_settings()
    providers = [p.strip() for p in s.llm_providers.split(",") if p.strip()]
    if "gemini" in providers:
        raise pytest.UsageError("The judge is Gemini, so Gemini can't also be the model under test "
                                "(EVALS_MODEL_PROVIDERS must not include gemini).")
    if "groq" in providers and not s.groq_api_key.get_secret_value():
        raise pytest.UsageError("GROQ_API_KEY is missing (backend/.env): the eval suite runs the real model.")


@pytest.fixture(scope="session")
def outputs():
    """Every selected golden through the real pipeline once (cached on disk), before any metric runs."""
    from evals.cache import DiskCache
    from evals.pipeline import run_all
    from evals.report import selected_goldens

    return asyncio.run(run_all(selected_goldens(), cache=DiskCache("pipeline")))


@pytest.fixture(scope="session")
def judge():
    if os.getenv("EVALS_JUDGE", "1") == "0":
        return None
    from evals.judge import GeminiJudge

    return GeminiJudge()


def pytest_sessionfinish(session, exitstatus):
    from evals.report import write_report

    write_report()

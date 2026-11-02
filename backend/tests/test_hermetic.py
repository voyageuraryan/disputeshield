"""Unit tests must never pick up real credentials or call home (Phase 10A/10B)."""

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_deepeval_pytest_plugin_is_not_registered(request):
    # it would autoload .env files into os.environ and send telemetry (see pytest.ini)
    assert not request.config.pluginmanager.has_plugin("deepeval")


def test_importing_deepeval_adds_nothing_from_dotenv():
    """With the flags tests/conftest.py sets, importing deepeval from backend/ (where the real .env lives) adds none
    of its keys to the environment. Runs in a subprocess and compares key NAMES only (no values are printed)."""
    code = ("import json, os; before = set(os.environ); import deepeval, deepeval.metrics, deepeval.tracing; "
            "print(json.dumps(sorted(set(os.environ) - before)))")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "PYTHONPATH")}
    env.update(DEEPEVAL_DISABLE_DOTENV="1", DEEPEVAL_TELEMETRY_OPT_OUT="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    added = set(json.loads(r.stdout.strip().splitlines()[-1]))
    dotenv = BACKEND / ".env"
    if dotenv.exists():
        keys = {line.split("=", 1)[0].strip() for line in dotenv.read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#")}
        assert not added & keys, f"deepeval loaded these .env keys: {sorted(added & keys)}"
    assert not {k for k in added if "KEY" in k or "SECRET" in k or "TOKEN" in k}


def test_tracing_is_off_by_default():
    from app import tracing

    assert os.getenv("DEEPEVAL_TRACING") in (None, "", "0") and tracing.ENABLED is False

    def f():
        return 1

    assert tracing.observe(type="agent")(f) is f  # zero overhead: the function isn't wrapped

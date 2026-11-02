"""Unit tests must never pick up real credentials or call home (Phase 10A)."""

import os
import sys


def test_deepeval_plugin_is_not_loaded():
    # deepeval's pytest plugin autoloads .env files into os.environ and sends telemetry (see pytest.ini)
    assert not any(m == "deepeval" or m.startswith("deepeval.") for m in sys.modules)


def test_tracing_is_off_by_default():
    from app import tracing

    assert os.getenv("DEEPEVAL_TRACING") in (None, "", "0") and tracing.ENABLED is False

    def f():
        return 1

    assert tracing.observe(type="agent")(f) is f  # zero overhead: the function isn't wrapped

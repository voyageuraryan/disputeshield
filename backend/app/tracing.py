"""Optional DeepEval tracing (Phase 10A).

`observe(type=...)` is DeepEval's @observe when DEEPEVAL_TRACING=1 and deepeval is installed (dev/eval only);
otherwise it returns the function unchanged, so production has zero overhead: nothing is wrapped and deepeval
isn't imported. The flag is read once, at import time (decoration time).

DeepEval only uploads traces when a Confident AI API key is configured; without one they stay in-process
(the eval harness reads them). DEEPEVAL_TELEMETRY_OPT_OUT=1 is set alongside so deepeval sends no usage
telemetry either.
"""

from __future__ import annotations

import os
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

ENABLED = os.getenv("DEEPEVAL_TRACING", "").strip().lower() in ("1", "true", "yes")


def _identity(fn: F) -> F:
    return fn


def observe(type: str | None = None, **kwargs: Any) -> Callable[[F], F]:  # noqa: A002 (DeepEval's name)
    if not ENABLED:
        return _identity
    try:
        _quiet()
        from deepeval.tracing import observe as deepeval_observe
    except ImportError:  # deepeval is a dev dependency; production images don't have it
        return _identity
    return deepeval_observe(type=type, **kwargs)


def _quiet() -> None:
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")  # no usage telemetry
    os.environ.setdefault("CONFIDENT_TRACE_VERBOSE", "0")     # no "skipping trace posting" line per trace


def update_trace(**kwargs: Any) -> None:
    """deepeval.tracing.update_current_trace when tracing is on; a no-op otherwise."""
    if not ENABLED:
        return
    try:
        from deepeval.tracing import update_current_trace
    except ImportError:
        return
    try:
        update_current_trace(**kwargs)
    except Exception:  # noqa: BLE001 - tracing must never break the pipeline
        pass

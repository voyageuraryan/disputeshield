"""Phase 10C: simulated buyers vs the real troubleshooting agent (evals/troubleshooting_sim.py), scored by the
conversational metrics. Results go into the same report; the gates (test_zz_gates.py) cover them."""

from __future__ import annotations

import os

import pytest

from evals.metrics import conversational as conv
from evals.report import metric_row, record_conv, selected_conversations

CONVS = selected_conversations()


def measure(metric, case) -> None:  # noqa: ANN001
    try:
        metric.measure(case)
    except Exception as exc:  # noqa: BLE001 - judge quota / timeout: recorded, the gates count it as incomplete
        metric.score, metric.success, metric.error = None, None, f"{type(exc).__name__}: {str(exc)[:300]}"


@pytest.mark.parametrize("golden", CONVS, ids=[g.id for g in CONVS])
def test_conversation(golden, conversations, judge):
    only = os.getenv("EVALS_ONLY", "")
    if golden.live_search and golden.id not in only:
        pytest.skip("live-search smoke golden: run it on its own with --only conv-live-search-smoke")
    res = conversations(golden)
    if res.get("error"):
        record_conv(golden, res, [])
        pytest.skip(f"{res['error']}: {res.get('detail', '')[:200]}")
    g = golden.model_dump(mode="json")
    case = conv.test_case(res, g)
    det = conv.deterministic(g, res)
    rows = []
    for m in det:
        measure(m, case)
        rows.append(metric_row(m, "deterministic"))
    if judge is not None:
        for m in conv.judged(judge):
            measure(m, case)
            rows.append(metric_row(m, "judged"))
    record_conv(golden, res, rows)
    failed = [f"{m.__name__}: {m.reason}" for m in det if not m.success]
    assert not failed, "; ".join(failed)

"""Runs last (file order): the whole-run gates over decision goldens AND troubleshooting conversations."""

from __future__ import annotations

import os

import pytest

from evals import gates
from evals.report import ROWS, merged_rows


def test_gates():
    if not ROWS:
        pytest.skip("nothing ran")
    rows = merged_rows(ROWS) if os.getenv("EVALS_MERGE") == "1" else ROWS  # chunked: gate the whole report
    result = gates.check(rows)
    assert result["passed"], "eval gates failed:\n" + "\n".join(result["failures"])

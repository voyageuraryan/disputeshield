"""Two simultaneous retries of one FAILED decision: only one runs (no double send)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api import decisions


@pytest.mark.asyncio
async def test_second_simultaneous_retry_is_refused(monkeypatch):
    runs = []

    async def slow_retry(decision_id, body, session, engine, paypal):
        runs.append(decision_id)
        await asyncio.sleep(0.05)
        return "ok"

    monkeypatch.setattr(decisions, "_retry", slow_retry)
    results = await asyncio.gather(
        decisions.retry(7, None, None, None, None, merchant={"email": "m@x.com"}),
        decisions.retry(7, None, None, None, None, merchant={"email": "m@x.com"}),
        return_exceptions=True)
    assert runs == [7]
    assert sum(r == "ok" for r in results) == 1
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 409

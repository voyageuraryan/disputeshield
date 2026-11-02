"""GET /api/admin/evals/latest: the latest eval report (backend/evals/reports/latest.json) for the Trust page
(Phase 16). Merchant sign-in required (mounted with require_merchant). Read-only; 404 until a report exists."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(tags=["evals"])
REPORT = Path(__file__).resolve().parents[2] / "evals" / "reports" / "latest.json"


@router.get("/api/admin/evals/latest")
def latest_eval_report() -> JSONResponse:
    try:
        data = json.loads(REPORT.read_text())
    except FileNotFoundError:
        raise HTTPException(404, "No eval report yet: run scripts/run_evals.sh") from None
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"The eval report couldn't be read: {type(exc).__name__}") from exc
    return JSONResponse(data, headers={"Cache-Control": "no-store"})

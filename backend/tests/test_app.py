from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import create_engine

from app.config import Settings
from app.db import create_db_and_tables
from app.main import app
from app.schemas import CaseFile, EvidenceItem, Recommendation


def test_health():
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_creates_tables():
    eng = create_engine("sqlite://")
    create_db_and_tables(eng)
    assert set(inspect(eng).get_table_names()) == {
        "orders", "shipments", "messages", "disputes", "evidence_items", "agent_steps", "decisions",
        "processed_events",  # idempotency ledger for webhook/poll ingestion
        "merchant_settings",  # merchant-editable settings (settle threshold)
    }


def test_settings_reject_live_paypal():
    with pytest.raises(ValueError, match="sandbox only"):
        Settings(_env_file=None, paypal_base_url="https://api-m.paypal.com")


def test_recommendation_rejects_unknown_citations():
    case = CaseFile(
        dispute_id="PP-D-1", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", amount=Decimal("10"),
        currency="USD", due_date=None, order_id=None,
        evidence=[EvidenceItem(id=1, type="tracking", source="paypal", source_ref="paypal:tracker/X", summary="s")],
    )
    rec = Recommendation(action="FIGHT", confidence="high", reasoning="r", cited_evidence_ids=[1],
                         missing_items=[], economic_note="e")
    assert rec.check_citations(case) is rec
    with pytest.raises(ValueError, match=r"\[2\]"):
        rec.model_copy(update={"cited_evidence_ids": [1, 2]}).check_citations(case)

"""Record REAL web-research results for the troubleshooting simulations (Phase 10C), once:

    cd backend && python -m evals.record_research        (uses GEMINI_API_KEY / TAVILY_API_KEY from backend/.env)

Writes evals/goldens/fixtures/research.json keyed by product model, so simulated conversations are reproducible
and spend no search quota. Re-record when the products or the research prompt change. Never fabricate it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from app.agents.web_research import ResearchError, research
from app.db import create_db_and_tables
from evals.troubleshooting_sim import FIXTURE

PRODUCTS = (("Wireless earbuds", "SoundPeak E5", "left earbud does not charge; case light stays off"),
            ("Smart LED desk lamp", "Lumina LD-200", "lamp does not turn on; no light at all"))


async def main() -> int:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(engine)
    out, today = {}, date.today()
    for name, model, symptom in PRODUCTS:
        with Session(engine) as s:
            try:
                notes = await research(name, model, symptom, db=s, today=today)
            except ResearchError as exc:
                print(f"{model}: FAILED: {str(exc)[:300]}")
                return 1
        out[model] = {"recorded": today.isoformat(), "product_name": name, "symptom": symptom, **notes.model_dump(mode="json")}
        print(f"{model}: {notes.provider}, {len(notes.sources)} sources, {len(notes.notes)} notes")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"wrote {FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

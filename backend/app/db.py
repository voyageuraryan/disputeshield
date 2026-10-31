from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers tables on SQLModel.metadata)
from app.config import get_settings

_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=_connect_args)


def create_db_and_tables(bind=None) -> None:
    eng = bind or engine
    SQLModel.metadata.create_all(eng)
    _add_missing_columns(eng)


def _add_missing_columns(eng) -> None:
    """Tiny migration: create_all never alters existing tables, so add new *nullable*
    columns to them. Anything more than that needs a real migration tool."""
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                if not col.nullable:
                    raise RuntimeError(f"cannot auto-add NOT NULL column {table.name}.{col.name}")
                ddl_type = col.type.compile(dialect=eng.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))


def get_engine() -> Engine:
    """Dependency so background work (agent runs) can open its own sessions; tests override it."""
    return engine


def get_session(eng: Engine = Depends(get_engine)) -> Iterator[Session]:
    with Session(eng) as session:
        yield session

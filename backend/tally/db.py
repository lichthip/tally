"""Engine/session management and schema bootstrap."""
from __future__ import annotations

import csv
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base, Symbol

DATA_DIR = Path(__file__).parent / "data"


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    # pool_pre_ping: free-tier Postgres (Neon/Render) drops idle connections; ping first.
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


@lru_cache
def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Unit of work: commit on success, roll back on any exception."""
    factory = factory or get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Append-only enforcement lives in the database, not just in application code:
# even a direct psql UPDATE is refused. TRUNCATE (used by tests) is unaffected.
APPEND_ONLY_DDL = """
CREATE OR REPLACE FUNCTION tally_forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'trade_ideas is append-only: % on id % rejected', TG_OP, OLD.id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trade_ideas_append_only ON trade_ideas;
CREATE TRIGGER trade_ideas_append_only
    BEFORE UPDATE OR DELETE ON trade_ideas
    FOR EACH ROW EXECUTE FUNCTION tally_forbid_mutation();
"""


def init_db(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Serialise concurrent bootstraps (e.g. API + worker starting together).
        conn.execute(text("SELECT pg_advisory_xact_lock(7231001)"))
        conn.execute(text(APPEND_ONLY_DDL))
    seed_symbols(engine)


def seed_symbols(engine: Engine | None = None) -> int:
    """Load the bundled symbol master if the table is empty."""
    engine = engine or get_engine()
    factory = sessionmaker(bind=engine)
    with session_scope(factory) as s:
        if s.execute(select(Symbol.ticker).limit(1)).first():
            return 0
        n = 0
        with open(DATA_DIR / "symbols.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                aliases = [a.strip().lower() for a in row["aliases"].split("|") if a.strip()]
                s.add(Symbol(ticker=row["ticker"], name=row["name"],
                             exchange=row["exchange"], aliases=aliases))
                n += 1
        return n

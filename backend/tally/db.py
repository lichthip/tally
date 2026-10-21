# =============================================================================
# db.py: CONNECTING TO POSTGRES, AND CREATING THE TABLES
# -----------------------------------------------------------------------------
# Vocabulary (SQLAlchemy, the Python library we use to talk to the database):
#   Engine          = the connection pool: a set of open connections to Postgres,
#                     reused so we don't pay to reconnect on every query.
#   Session         = one "conversation" with the database. You add/change rows
#                     through it, then commit (save) or rollback (undo everything).
#   sessionmaker    = a factory: calling it gives you a new Session. We pass it
#                     around as `sf` / `session_factory` all over the code.
#   transaction     = a group of changes that succeed together or fail together.
#
# Who calls this file: runtime.py (gets the session factory), __main__.py
# (init-db), the API (via deps.py), and almost every service through session_scope.
# =============================================================================
"""Engine/session management and schema bootstrap."""
from __future__ import annotations

import csv                                 # reading the bundled symbols.csv
from contextlib import contextmanager      # lets us write `with session_scope() as s:`
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base, Symbol    # Base knows about every table class in models.py

# Folder next to this file holding symbols.csv and eval_posts.jsonl.
DATA_DIR = Path(__file__).parent / "data"


# Build the connection pool ONCE (lru_cache) and reuse it everywhere.
@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url    # use the given URL, or the configured one
    # pool_pre_ping: free-tier Postgres (Neon/Render) drops idle connections; ping first.
    # pool_size=10 connections kept open, plus up to 10 extra under load.
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


# Also built once. expire_on_commit=False: after commit, objects we loaded keep their
# values (otherwise touching `post.text` after commit would trigger another query,
# or fail if the session is closed).
@lru_cache
def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


# The helper used everywhere:
#     with session_scope(sf) as s:
#         s.add(...)          # do work
#     # leaving the block = COMMIT; an exception inside = ROLLBACK
# This is the "unit of work" pattern: either all the changes in the block are saved,
# or none are. Nobody can forget to commit or forget to roll back.
@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Unit of work: commit on success, roll back on any exception."""
    factory = factory or get_session_factory()
    session = factory()
    try:
        yield session          # hand the session to the code inside the `with` block
        session.commit()       # block finished normally -> save
    except Exception:
        session.rollback()     # anything went wrong -> undo all of it
        raise                  # ...and still report the error to the caller
    finally:
        session.close()        # always give the connection back to the pool


# Append-only enforcement lives in the database, not just in application code:
# even a direct psql UPDATE is refused. TRUNCATE (used by tests) is unaffected.
#
# This is raw SQL run once at startup. It creates:
#   1. a function that always raises an error, and
#   2. a TRIGGER that runs that function BEFORE any UPDATE or DELETE on trade_ideas.
# Result: once an idea is written it can never be edited or deleted, not even by a
# bug in our own code. That is the "honest track record" guarantee.
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


# Called on startup (and by `python -m tally init-db`). Safe to run many times.
def init_db(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    # Create every table defined in models.py that doesn't exist yet.
    # (It never alters or drops existing tables.)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:          # engine.begin() = a transaction that auto-commits
        # Serialise concurrent bootstraps (e.g. API + worker starting together).
        # An advisory lock is a named lock (the number is just a name we picked).
        # If two processes start at once, the second waits here until the first commits,
        # so they don't both try to create the trigger at the same moment.
        conn.execute(text("SELECT pg_advisory_xact_lock(7231001)"))
        conn.execute(text(APPEND_ONLY_DDL))
    seed_symbols(engine)


# Fill the `symbols` table (ticker -> name, exchange, aliases) from data/symbols.csv,
# but only the first time (if the table is empty). The symbol master is what lets us
# check "is NVDA a real ticker?" and map "nvidia" -> NVDA.
def seed_symbols(engine: Engine | None = None) -> int:
    """Load the bundled symbol master if the table is empty."""
    engine = engine or get_engine()
    factory = sessionmaker(bind=engine)
    with session_scope(factory) as s:
        # Is there at least one row already? Then do nothing.
        if s.execute(select(Symbol.ticker).limit(1)).first():
            return 0
        n = 0
        with open(DATA_DIR / "symbols.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):     # each row is a dict: {"ticker": ..., "aliases": ...}
                # aliases look like "nvidia|nvda corp" in the CSV -> ["nvidia", "nvda corp"]
                aliases = [a.strip().lower() for a in row["aliases"].split("|") if a.strip()]
                s.add(Symbol(ticker=row["ticker"], name=row["name"],
                             exchange=row["exchange"], aliases=aliases))
                n += 1
        return n     # leaving the `with` commits all the inserts at once

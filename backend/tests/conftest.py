"""Test fixtures.

Pure-logic tests (marking, skill, extraction validation) need nothing.
Integration tests need a real Postgres (SKIP LOCKED, JSONB, triggers can't be faked with
SQLite) and optionally Redis. Point TEST_DATABASE_URL / TEST_REDIS_URL at them; tests
that need an unavailable service are skipped, not failed.
"""
from __future__ import annotations

import os

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/tally_test")
TEST_REDIS = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

# Must be set before tally.config is first imported anywhere.
os.environ["DATABASE_URL"] = TEST_DB
os.environ["REDIS_URL"] = TEST_REDIS
os.environ["LLM_PROVIDER"] = "rules"
os.environ["MARKET_DATA_PROVIDER"] = "synthetic"
os.environ["ADMIN_TOKEN"] = "test-token"
os.environ["EMBEDDED_WORKER"] = "false"
os.environ["EMBEDDED_SCHEDULER"] = "false"

from sqlalchemy import text  # noqa: E402

from tally.config import get_settings  # noqa: E402

TABLES = ["jobs", "idea_marks", "trade_ideas", "raw_posts", "contributor_stats", "contributors",
          "signal_snapshots", "scoring_runs", "llm_cache", "prices", "price_coverage",
          "worker_heartbeats"]


@pytest.fixture(scope="session")
def engine():
    from tally.db import get_engine, init_db
    get_settings.cache_clear()
    eng = get_engine()
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres not available at {TEST_DB}: {e}")
    init_db(eng)
    return eng


@pytest.fixture
def sf(engine):
    from tally.db import get_session_factory
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    return get_session_factory()


@pytest.fixture
def redis_client():
    import redis
    client = redis.Redis.from_url(TEST_REDIS, decode_responses=True)
    try:
        client.ping()
        info = client.info("server")
        if int(info["redis_version"].split(".")[0]) < 7:
            raise RuntimeError("Redis >= 7 required for XAUTOCLAIM semantics")
    except Exception:  # noqa: BLE001
        import fakeredis
        client = fakeredis.FakeRedis(decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()

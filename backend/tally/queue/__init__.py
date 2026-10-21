# =============================================================================
# queue/__init__.py: PICKS WHICH QUEUE TO BUILD (factory)
# -----------------------------------------------------------------------------
# A QUEUE here is a to-do list of jobs: "please extract post 42". The API adds jobs
# fast; workers take them out slowly (limited by the LLM's rate limit). The queue sits
# in between so the API never waits for the LLM.
#
# Two implementations exist (Postgres table, Redis Streams). build_queue() reads the
# QUEUE_BACKEND setting and returns one of them. Everyone else only sees the JobQueue
# interface, so they don't know or care which one it is.
# =============================================================================
"""Queue factory: the backend is chosen by config, callers only see JobQueue."""
from __future__ import annotations

from .base import JobQueue, Message, QueueStats
from .postgres_queue import PostgresQueue
from .redis_queue import RedisStreamQueue

__all__ = ["JobQueue", "Message", "QueueStats", "PostgresQueue", "RedisStreamQueue", "build_queue"]


# Called by runtime.py at startup (and by the bench tool with an explicit backend).
def build_queue(backend: str | None = None, queue: str = "extract",
                lease_seconds: int | None = None) -> JobQueue:
    # Imported inside the function to avoid a circular import (config/db import other
    # modules that import this package).
    from ..config import get_settings
    from ..db import get_session_factory

    st = get_settings()
    backend = backend or st.queue_backend                 # argument wins, else the setting
    lease = lease_seconds if lease_seconds is not None else st.lease_seconds
    if backend == "postgres":
        return PostgresQueue(get_session_factory(), queue=queue, lease_seconds=lease,
                             max_attempts=st.max_attempts)
    if backend == "redis":
        import redis        # only imported if Redis is actually used
        # decode_responses=True: Redis gives us Python strings instead of raw bytes.
        client = redis.Redis.from_url(st.redis_url, decode_responses=True)
        return RedisStreamQueue(client, queue=queue, lease_seconds=lease,
                                max_attempts=st.max_attempts)
    raise ValueError(f"unknown queue backend: {backend}")

"""Queue factory: the backend is chosen by config, callers only see JobQueue."""
from __future__ import annotations

from .base import JobQueue, Message, QueueStats
from .postgres_queue import PostgresQueue
from .redis_queue import RedisStreamQueue

__all__ = ["JobQueue", "Message", "QueueStats", "PostgresQueue", "RedisStreamQueue", "build_queue"]


def build_queue(backend: str | None = None, queue: str = "extract",
                lease_seconds: int | None = None) -> JobQueue:
    from ..config import get_settings
    from ..db import get_session_factory

    st = get_settings()
    backend = backend or st.queue_backend
    lease = lease_seconds if lease_seconds is not None else st.lease_seconds
    if backend == "postgres":
        return PostgresQueue(get_session_factory(), queue=queue, lease_seconds=lease,
                             max_attempts=st.max_attempts)
    if backend == "redis":
        import redis
        client = redis.Redis.from_url(st.redis_url, decode_responses=True)
        return RedisStreamQueue(client, queue=queue, lease_seconds=lease,
                                max_attempts=st.max_attempts)
    raise ValueError(f"unknown queue backend: {backend}")

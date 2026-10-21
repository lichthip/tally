"""The JobQueue contract.

Two implementations satisfy it (Postgres and Redis Streams) and the rest of the system
only ever talks to this interface, so the backend is a config switch, and the two can be
benchmarked against each other on identical workloads.

Delivery guarantee: AT-LEAST-ONCE.
  * A claimed message is leased to one worker for `lease_seconds`.
  * If the worker acks, it is done. If the worker crashes (or is too slow), the lease
    expires and another worker reclaims it -> the same message can be processed twice.
  * Therefore every job handler MUST be idempotent. (Exactly-once delivery is not
    achievable across a network; we get "effectively once" via idempotent handlers.)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Message:
    id: str                 # backend-specific id (job row id / stream entry id)
    payload: dict[str, Any]
    attempts: int           # delivery count INCLUDING this one
    # Opaque token the backend uses to reject stale acks (fencing). For Postgres this
    # is the attempt number at claim time; for Redis it is the consumer name.
    receipt: str = ""


@dataclass
class QueueStats:
    backend: str
    queue: str
    pending: int            # waiting to be picked up
    in_flight: int          # claimed, not yet acked
    dead: int               # exhausted retries
    done: int | None = None
    oldest_pending_age_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class JobQueue(ABC):
    backend: str = "abstract"
    #: True if enqueue can join the caller's DB transaction (no dual-write problem).
    transactional_enqueue: bool = False

    def __init__(self, queue: str, lease_seconds: int, max_attempts: int) -> None:
        self.queue = queue
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    @abstractmethod
    def enqueue(self, payload: dict[str, Any], *, session: Session | None = None) -> str:
        """Add a job. If `session` is given and the backend is transactional, the job
        becomes visible only when that session commits."""

    @abstractmethod
    def claim(self, worker_id: str, max_messages: int = 1, block_ms: int = 1000) -> list[Message]:
        """Lease up to `max_messages` jobs to `worker_id`, waiting up to `block_ms`.
        Includes jobs whose previous lease expired (crashed workers)."""

    @abstractmethod
    def ack(self, msg: Message) -> bool:
        """Mark done. Returns False if our lease was lost (someone else owns it now)."""

    @abstractmethod
    def fail(self, msg: Message, error: str) -> bool:
        """Record a failed attempt. Returns True if the job was dead-lettered."""

    @abstractmethod
    def extend_lease(self, msg: Message) -> bool:
        """Heartbeat for long jobs: push the lease deadline out again."""

    @abstractmethod
    def stats(self) -> QueueStats: ...

    @abstractmethod
    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]: ...

    @abstractmethod
    def purge(self) -> None:
        """Delete everything in this queue (tests and benchmarks only)."""

    def backoff_seconds(self, attempts: int) -> float:
        """Exponential backoff: 2, 4, 8, 16 ... seconds, capped at 5 minutes."""
        return float(min(2 ** attempts, 300))

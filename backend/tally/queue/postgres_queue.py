"""Postgres-backed job queue using SELECT ... FOR UPDATE SKIP LOCKED.

Why this works for concurrency:
  * FOR UPDATE row-locks the job rows we select, inside our transaction.
  * SKIP LOCKED makes other workers silently skip rows already locked, instead of
    blocking on them. So N workers can claim in parallel and never get the same row.
  * The claim is one statement (CTE + UPDATE ... RETURNING), so "select a job" and
    "mark it mine" are atomic. No window where two workers both see it as free.

Crash recovery uses LEASES rather than a held lock/mutex: a claimed job carries a
`lease_until` deadline. A crashed worker can't release anything, but its lease simply
expires, and the claim query treats an expired 'running' job as claimable again.

Fencing: `attempts` increments on every claim, so it doubles as a fencing token. An ack
or fail only applies if the row's attempts still equals the value we claimed with. A
slow worker whose lease was stolen therefore can't overwrite the new owner's state.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .base import JobQueue, Message, QueueStats

CLAIM_SQL = text(
    """
    WITH claimable AS (
        SELECT id FROM jobs
        WHERE queue = :queue
          AND (
                (status = 'pending' AND available_at <= now())
             OR (status = 'running' AND lease_until < now())      -- crashed/slow worker
          )
        ORDER BY available_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT :n
    )
    UPDATE jobs j
       SET status      = 'running',
           attempts    = j.attempts + 1,
           locked_by   = :worker,
           lease_until = now() + make_interval(secs => :lease)
      FROM claimable
     WHERE j.id = claimable.id
    RETURNING j.id, j.payload, j.attempts, j.max_attempts
    """
)


class PostgresQueue(JobQueue):
    backend = "postgres"
    transactional_enqueue = True

    def __init__(self, session_factory: sessionmaker[Session], queue: str = "extract",
                 lease_seconds: int = 120, max_attempts: int = 5, poll_interval_s: float = 0.2):
        super().__init__(queue, lease_seconds, max_attempts)
        self._sf = session_factory
        self._poll = poll_interval_s

    # ------------------------------------------------------------------ enqueue
    def enqueue(self, payload: dict[str, Any], *, session: Session | None = None) -> str:
        stmt = text(
            "INSERT INTO jobs (queue, payload, max_attempts) "
            "VALUES (:q, CAST(:p AS jsonb), :m) RETURNING id"
        )
        params = {"q": self.queue, "p": _json(payload), "m": self.max_attempts}
        if session is not None:
            # Same transaction as the caller's writes: both commit or neither does.
            return str(session.execute(stmt, params).scalar_one())
        with self._sf.begin() as s:
            return str(s.execute(stmt, params).scalar_one())

    # -------------------------------------------------------------------- claim
    def claim(self, worker_id: str, max_messages: int = 1, block_ms: int = 1000) -> list[Message]:
        # Postgres has no blocking pop, so we poll. (LISTEN/NOTIFY would remove the
        # polling delay; see docs/DESIGN_DECISIONS.md.)
        deadline = time.monotonic() + block_ms / 1000
        while True:
            msgs = self._claim_once(worker_id, max_messages)
            if msgs or time.monotonic() >= deadline:
                return msgs
            time.sleep(self._poll * (0.5 + random.random()))  # jitter avoids lockstep

    def _claim_once(self, worker_id: str, n: int) -> list[Message]:
        out: list[Message] = []
        with self._sf.begin() as s:
            rows = s.execute(CLAIM_SQL, {"queue": self.queue, "n": n, "worker": worker_id,
                                         "lease": self.lease_seconds}).all()
            for row in rows:
                if row.attempts > row.max_attempts:
                    # Its previous holder crashed on the final attempt: poison message.
                    s.execute(text(
                        "UPDATE jobs SET status='dead', finished_at=now(), "
                        "last_error=coalesce(last_error, 'lease expired on final attempt') "
                        "WHERE id=:id"), {"id": row.id})
                    continue
                out.append(Message(id=str(row.id), payload=row.payload,
                                   attempts=row.attempts, receipt=str(row.attempts)))
        return out

    # ---------------------------------------------------------------- ack/fail
    def ack(self, msg: Message) -> bool:
        with self._sf.begin() as s:
            res = s.execute(text(
                "UPDATE jobs SET status='done', finished_at=now(), lease_until=NULL "
                "WHERE id=:id AND attempts=:fence AND status='running'"),
                {"id": int(msg.id), "fence": int(msg.receipt)})
            return res.rowcount == 1

    def fail(self, msg: Message, error: str) -> bool:
        dead = msg.attempts >= self.max_attempts
        with self._sf.begin() as s:
            if dead:
                s.execute(text(
                    "UPDATE jobs SET status='dead', finished_at=now(), last_error=:e, "
                    "lease_until=NULL WHERE id=:id AND attempts=:fence"),
                    {"id": int(msg.id), "fence": int(msg.receipt), "e": error[:2000]})
            else:
                s.execute(text(
                    "UPDATE jobs SET status='pending', last_error=:e, lease_until=NULL, "
                    "available_at = now() + make_interval(secs => :delay) "
                    "WHERE id=:id AND attempts=:fence"),
                    {"id": int(msg.id), "fence": int(msg.receipt), "e": error[:2000],
                     "delay": self.backoff_seconds(msg.attempts)})
        return dead

    def extend_lease(self, msg: Message) -> bool:
        with self._sf.begin() as s:
            res = s.execute(text(
                "UPDATE jobs SET lease_until = now() + make_interval(secs => :lease) "
                "WHERE id=:id AND attempts=:fence AND status='running'"),
                {"id": int(msg.id), "fence": int(msg.receipt), "lease": self.lease_seconds})
            return res.rowcount == 1

    # ------------------------------------------------------------ observability
    def stats(self) -> QueueStats:
        with self._sf() as s:
            row = s.execute(text(
                """
                SELECT
                  count(*) FILTER (WHERE status='pending')                         AS pending,
                  count(*) FILTER (WHERE status='running' AND lease_until >= now()) AS in_flight,
                  count(*) FILTER (WHERE status='running' AND lease_until <  now()) AS expired,
                  count(*) FILTER (WHERE status='dead')                            AS dead,
                  count(*) FILTER (WHERE status='done')                            AS done,
                  extract(epoch FROM now() - min(created_at) FILTER (WHERE status='pending'))
                                                                                    AS oldest
                FROM jobs WHERE queue = :q
                """), {"q": self.queue}).one()
        return QueueStats(backend=self.backend, queue=self.queue,
                          pending=row.pending + row.expired, in_flight=row.in_flight,
                          dead=row.dead, done=row.done,
                          oldest_pending_age_s=float(row.oldest) if row.oldest else None,
                          extra={"expired_leases": row.expired})

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._sf() as s:
            rows = s.execute(text(
                "SELECT id, payload, attempts, last_error, finished_at FROM jobs "
                "WHERE queue=:q AND status='dead' ORDER BY finished_at DESC LIMIT :n"),
                {"q": self.queue, "n": limit}).all()
        return [{"id": str(r.id), "payload": r.payload, "attempts": r.attempts,
                 "error": r.last_error, "at": r.finished_at.isoformat() if r.finished_at else None}
                for r in rows]

    def cleanup(self, older_than_days: int = 7) -> int:
        """Delete finished jobs past retention so the table (and its indexes) stay small.
        Dead jobs are kept: they're the audit trail of what failed."""
        with self._sf.begin() as s:
            res = s.execute(text(
                "DELETE FROM jobs WHERE queue=:q AND status='done' "
                "AND finished_at < now() - make_interval(days => :d)"),
                {"q": self.queue, "d": older_than_days})
            return res.rowcount

    def purge(self) -> None:
        with self._sf.begin() as s:
            s.execute(text("DELETE FROM jobs WHERE queue=:q"), {"q": self.queue})


def _json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)

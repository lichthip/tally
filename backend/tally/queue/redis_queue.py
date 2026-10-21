"""Redis Streams job queue (consumer groups).

Mapping to queue concepts:
  * XADD                  -> enqueue (append to an append-only log)
  * XREADGROUP ... '>'    -> claim new messages; each goes to exactly one consumer in the group
  * XACK                  -> ack; removes it from the group's Pending Entries List (PEL)
  * XAUTOCLAIM min-idle   -> reclaim messages whose consumer went quiet (= lease expiry)
  * XPENDING              -> per-message delivery count, used for retry limits

Failure handling is idiomatic Streams: a failed message is simply NOT acked. It stays in
the PEL and is reclaimed after `lease_seconds` of idleness, which also acts as a natural
backoff. Once its delivery count exceeds `max_attempts` it is copied to a dead-letter
stream and acked.

Trade-offs vs Postgres (see docs): much higher throughput and true blocking reads, but
enqueue cannot join a Postgres transaction (dual write), retries can't have per-attempt
exponential delays without extra machinery, and durability depends on Redis persistence
settings (AOF/RDB).
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis
from sqlalchemy.orm import Session

from .base import JobQueue, Message, QueueStats


class RedisStreamQueue(JobQueue):
    backend = "redis"
    transactional_enqueue = False

    def __init__(self, client: redis.Redis, queue: str = "extract",
                 lease_seconds: int = 120, max_attempts: int = 5, group: str = "workers"):
        super().__init__(queue, lease_seconds, max_attempts)
        self.r = client
        self.stream = f"tally:{queue}"
        self.dlq = f"tally:{queue}:dead"
        self.errors = f"tally:{queue}:errors"       # hash: message id -> last error
        self.group = group
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            # id="0": a brand new group also sees entries added before it existed.
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    # ------------------------------------------------------------------ enqueue
    def enqueue(self, payload: dict[str, Any], *, session: Session | None = None) -> str:
        # `session` is ignored on purpose: Redis can't take part in a SQL transaction.
        # Callers must enqueue AFTER their DB commit, and a sweeper repairs the gap.
        return self.r.xadd(self.stream, {"payload": json.dumps(payload, default=str)})

    # -------------------------------------------------------------------- claim
    def claim(self, worker_id: str, max_messages: int = 1, block_ms: int = 1000) -> list[Message]:
        # 1) Steal messages whose previous consumer has been idle longer than the lease.
        msgs = self._reclaim_stale(worker_id, max_messages)
        if msgs:
            return msgs
        # 2) Otherwise block for genuinely new messages.
        resp = self.r.xreadgroup(self.group, worker_id, {self.stream: ">"},
                                 count=max_messages, block=block_ms)
        out: list[Message] = []
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                out.append(Message(id=entry_id, payload=json.loads(fields["payload"]),
                                   attempts=1, receipt=worker_id))
        return out

    def _reclaim_stale(self, worker_id: str, n: int) -> list[Message]:
        res = self.r.xautoclaim(self.stream, self.group, worker_id,
                                min_idle_time=self.lease_seconds * 1000,
                                start_id="0-0", count=n)
        entries = res[1] if res else []
        out: list[Message] = []
        for entry_id, fields in entries:
            if fields is None:   # entry was trimmed/deleted while pending
                self.r.xack(self.stream, self.group, entry_id)
                continue
            deliveries = self._delivery_count(entry_id)
            if deliveries > self.max_attempts:
                self._dead_letter(entry_id, fields, deliveries)
                continue
            out.append(Message(id=entry_id, payload=json.loads(fields["payload"]),
                               attempts=deliveries, receipt=worker_id))
        return out

    def _delivery_count(self, entry_id: str) -> int:
        info = self.r.xpending_range(self.stream, self.group, min=entry_id, max=entry_id, count=1)
        return int(info[0]["times_delivered"]) if info else 1

    def _dead_letter(self, entry_id: str, fields: dict, deliveries: int) -> None:
        err = self.r.hget(self.errors, entry_id) or "exceeded max deliveries"
        pipe = self.r.pipeline(transaction=True)      # MULTI/EXEC: all or nothing
        pipe.xadd(self.dlq, {"payload": fields["payload"], "source_id": entry_id,
                             "attempts": deliveries, "error": err, "at": time.time()})
        pipe.xack(self.stream, self.group, entry_id)
        pipe.hdel(self.errors, entry_id)
        pipe.execute()

    # ---------------------------------------------------------------- ack/fail
    def ack(self, msg: Message) -> bool:
        pipe = self.r.pipeline(transaction=True)
        pipe.xack(self.stream, self.group, msg.id)
        pipe.hdel(self.errors, msg.id)
        acked, _ = pipe.execute()
        return acked == 1

    def fail(self, msg: Message, error: str) -> bool:
        if msg.attempts >= self.max_attempts:
            fields = self.r.xrange(self.stream, min=msg.id, max=msg.id)
            payload = fields[0][1] if fields else {"payload": json.dumps(msg.payload)}
            self.r.hset(self.errors, msg.id, error[:2000])
            self._dead_letter(msg.id, payload, msg.attempts)
            return True
        # Not acking IS the retry: XAUTOCLAIM will hand it out again after the lease.
        self.r.hset(self.errors, msg.id, error[:2000])
        return False

    def extend_lease(self, msg: Message) -> bool:
        # XCLAIM to ourselves with min-idle 0 resets the idle timer (JUSTID: don't bump count).
        claimed = self.r.xclaim(self.stream, self.group, msg.receipt, min_idle_time=0,
                                message_ids=[msg.id], justid=True)
        return bool(claimed)

    # ------------------------------------------------------------ observability
    def stats(self) -> QueueStats:
        groups = {g["name"]: g for g in self.r.xinfo_groups(self.stream)}
        g = groups.get(self.group, {})
        in_flight = int(g.get("pending", 0))
        lag = g.get("lag")
        return QueueStats(backend=self.backend, queue=self.queue,
                          pending=int(lag) if lag is not None else 0,
                          in_flight=in_flight, dead=self.r.xlen(self.dlq),
                          extra={"stream_length": self.r.xlen(self.stream),
                                 "consumers": int(g.get("consumers", 0))})

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for entry_id, f in self.r.xrevrange(self.dlq, count=limit):
            out.append({"id": f.get("source_id", entry_id), "payload": json.loads(f["payload"]),
                        "attempts": int(f.get("attempts", 0)), "error": f.get("error"),
                        "at": f.get("at")})
        return out

    def purge(self) -> None:
        self.r.delete(self.stream, self.dlq, self.errors)
        self._ensure_group()

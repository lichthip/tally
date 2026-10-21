# =============================================================================
# queue/redis_queue.py: THE SAME QUEUE INTERFACE, BUILT ON REDIS STREAMS (alternative)
# -----------------------------------------------------------------------------
# Redis = an in-memory data store (very fast; keeps data in RAM). A "Stream" is an
# append-only log of messages. A "consumer group" is a set of workers sharing that
# log: each message goes to exactly ONE worker in the group.
#
# Redis remembers which messages were handed out but not yet acked (the "Pending
# Entries List", PEL). If a worker dies, its messages sit in the PEL going "idle";
# XAUTOCLAIM lets another worker take over messages idle for longer than the lease.
#
# Selected with QUEUE_BACKEND=redis. It's ~9x faster at enqueue (benchmarked), but it
# can't share a transaction with Postgres, so it needs the sweeper in
# ingestion/service.py. That's why Postgres is the default.
# =============================================================================
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
    transactional_enqueue = False     # can't join a Postgres transaction -> needs the sweeper

    def __init__(self, client: redis.Redis, queue: str = "extract",
                 lease_seconds: int = 120, max_attempts: int = 5, group: str = "workers"):
        super().__init__(queue, lease_seconds, max_attempts)
        self.r = client
        # Redis key names (Redis is a big dictionary of named keys).
        self.stream = f"tally:{queue}"              # the main stream of jobs
        self.dlq = f"tally:{queue}:dead"            # dead-letter stream
        self.errors = f"tally:{queue}:errors"       # hash: message id -> last error
        self.group = group
        self._ensure_group()

    # Create the consumer group (and the stream) if they don't exist yet.
    def _ensure_group(self) -> None:
        try:
            # id="0": a brand new group also sees entries added before it existed.
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as e:
            # BUSYGROUP = "group already exists". That's fine; anything else is a real error.
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
        # ">" means "messages never delivered to anyone". block=... makes Redis WAIT (up
        # to block_ms) and reply the instant a message arrives, so there's no polling.
        resp = self.r.xreadgroup(self.group, worker_id, {self.stream: ">"},
                                 count=max_messages, block=block_ms)
        out: list[Message] = []
        # Response shape: [(stream_name, [(entry_id, {"payload": "..."}), ...])]
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                out.append(Message(id=entry_id, payload=json.loads(fields["payload"]),
                                   attempts=1, receipt=worker_id))
        return out

    # Take over messages that another worker claimed but has sat on for > lease_seconds.
    def _reclaim_stale(self, worker_id: str, n: int) -> list[Message]:
        res = self.r.xautoclaim(self.stream, self.group, worker_id,
                                min_idle_time=self.lease_seconds * 1000,   # Redis wants milliseconds
                                start_id="0-0", count=n)
        entries = res[1] if res else []
        out: list[Message] = []
        for entry_id, fields in entries:
            if fields is None:   # entry was trimmed/deleted while pending
                self.r.xack(self.stream, self.group, entry_id)
                continue
            deliveries = self._delivery_count(entry_id)
            if deliveries > self.max_attempts:
                self._dead_letter(entry_id, fields, deliveries)    # too many tries: give up
                continue
            out.append(Message(id=entry_id, payload=json.loads(fields["payload"]),
                               attempts=deliveries, receipt=worker_id))
        return out

    # Ask Redis how many times this message has been delivered.
    def _delivery_count(self, entry_id: str) -> int:
        info = self.r.xpending_range(self.stream, self.group, min=entry_id, max=entry_id, count=1)
        return int(info[0]["times_delivered"]) if info else 1

    # Copy the message to the dead-letter stream and ack the original, as one atomic unit.
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
        acked, _ = pipe.execute()     # results of the two commands, in order
        return acked == 1             # 0 = it wasn't pending for us any more

    def fail(self, msg: Message, error: str) -> bool:
        if msg.attempts >= self.max_attempts:
            # Last attempt failed: dead-letter it now.
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
        in_flight = int(g.get("pending", 0))     # Redis's "pending" = delivered but un-acked
        lag = g.get("lag")                       # entries not yet delivered to anyone
        return QueueStats(backend=self.backend, queue=self.queue,
                          pending=int(lag) if lag is not None else 0,
                          in_flight=in_flight, dead=self.r.xlen(self.dlq),
                          extra={"stream_length": self.r.xlen(self.stream),
                                 "consumers": int(g.get("consumers", 0))})

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for entry_id, f in self.r.xrevrange(self.dlq, count=limit):   # newest first
            out.append({"id": f.get("source_id", entry_id), "payload": json.loads(f["payload"]),
                        "attempts": int(f.get("attempts", 0)), "error": f.get("error"),
                        "at": f.get("at")})
        return out

    def purge(self) -> None:
        self.r.delete(self.stream, self.dlq, self.errors)
        self._ensure_group()

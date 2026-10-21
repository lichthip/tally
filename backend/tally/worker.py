"""Extraction worker: claim job -> extract -> persist -> ack.

The important properties, and where they come from:
  * Idempotent: the post row is locked (SELECT ... FOR UPDATE) and its status re-checked
    before writing. Ideas have UNIQUE(post_id, ticker). A duplicate job is a no-op.
  * No long transactions: the slow LLM call happens OUTSIDE any DB transaction. We only
    open a short transaction to read the post, and another short one to write results.
  * Quota-aware: a rate-limit token is acquired before claiming, so a worker never holds
    a lease while waiting for LLM quota.
  * Crash-safe: if the process dies mid-job, the lease expires and another worker
    re-runs it; thanks to idempotency that is safe.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import socket
import threading
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .db import session_scope
from .extraction.llm_clients import RateLimiter
from .extraction.pipeline import ExtractionPipeline
from .extraction.types import ExtractionOutcome, IdeaCandidate
from .models import RawPost, TradeIdea, WorkerHeartbeat
from .queue import JobQueue, Message

log = logging.getLogger(__name__)


def insert_ideas(s: Session, post: RawPost, candidates: list[IdeaCandidate], *,
                 extractor: str, prompt_version: str | None, effective_at: dt.datetime) -> int:
    n = 0
    for c in candidates:
        if not c.ticker:
            continue
        res = s.execute(pg_insert(TradeIdea).values(
            post_id=post.id, contributor_id=post.contributor_id, ticker=c.ticker,
            direction=c.direction, horizon_days=c.horizon_days, horizon_inferred=c.horizon_inferred,
            target_price=c.target_price, conviction=c.conviction, confidence=c.confidence,
            rationale=c.rationale, received_at=post.received_at, effective_at=effective_at,
            extractor=extractor, prompt_version=prompt_version,
        ).on_conflict_do_nothing(constraint="uq_idea_post_ticker").returning(TradeIdea.id))
        n += res.scalar() is not None   # None = conflict, the idea already existed
    return n


class ExtractionWorker:
    def __init__(self, queue: JobQueue, pipeline: ExtractionPipeline,
                 session_factory: sessionmaker[Session], *, worker_id: str | None = None,
                 limiter: RateLimiter | None = None, stop_event: threading.Event | None = None):
        self.queue = queue
        self.pipeline = pipeline
        self.sf = session_factory
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.limiter = limiter
        self.stop = stop_event or threading.Event()
        self.processed = 0
        self.failed = 0
        self.started_at = dt.datetime.now(dt.timezone.utc)
        self._last_beat = 0.0

    # ----------------------------------------------------------------- loop
    def run_forever(self) -> None:
        log.info("worker %s started (queue=%s)", self.worker_id, self.queue.backend)
        while not self.stop.is_set():
            self.run_once(block_ms=2000)
        log.info("worker %s stopped", self.worker_id)

    def run_once(self, block_ms: int = 0) -> int:
        """Claim and process at most one message. Returns number processed (0 or 1)."""
        self._heartbeat()
        if self.limiter and not self.limiter.acquire(timeout=5, stop=self.stop):
            return 0
        try:
            msgs = self.queue.claim(self.worker_id, max_messages=1, block_ms=block_ms)
        except Exception:  # noqa: BLE001 - e.g. DB/Redis briefly unavailable
            log.exception("claim failed")
            if self.limiter:
                self.limiter.refund()
            self.stop.wait(2)
            return 0
        if not msgs:
            if self.limiter:
                self.limiter.refund()
            return 0
        for msg in msgs:
            self._handle(msg)
        self._heartbeat(every_s=2.0)   # surface progress promptly on the System page
        return len(msgs)

    def _handle(self, msg: Message) -> None:
        try:
            result = self.process(msg)
            if result == "skipped" and self.limiter:
                self.limiter.refund()   # duplicate job: no LLM call was made
            if not self.queue.ack(msg):
                log.warning("lost lease on %s before ack; another worker owns it", msg.id)
            self.processed += 1
        except Exception as e:  # noqa: BLE001 - every failure goes through the retry path
            self.failed += 1
            err = f"{type(e).__name__}: {e}"
            dead = self.queue.fail(msg, err)
            log.warning("job %s attempt %d failed (%s)%s", msg.id, msg.attempts, err,
                        " -> dead-lettered" if dead else "")
            if dead:
                self._mark_post_failed(msg.payload.get("post_id"), err)

    # -------------------------------------------------------------- the job
    def process(self, msg: Message) -> str:
        post_id = int(msg.payload["post_id"])
        # 1) short read transaction
        with self.sf() as s:
            post = s.get(RawPost, post_id)
            if post is None or post.status != "pending":
                return "skipped"
            text, when = post.text, post.posted_at or post.received_at

        # 2) slow part, no transaction open
        outcome = self.pipeline.run(text, when)

        # 3) short write transaction, re-checking state under a row lock
        with session_scope(self.sf) as s:
            post = s.execute(select(RawPost).where(RawPost.id == post_id).with_for_update()).scalar_one()
            if post.status != "pending":
                return "skipped"   # a concurrent duplicate got here first
            self._apply_outcome(s, post, outcome)
        return outcome.status

    @staticmethod
    def _apply_outcome(s: Session, post: RawPost, outcome: ExtractionOutcome) -> None:
        post.status = outcome.status
        post.extraction = outcome.to_json()
        post.extractor = outcome.extractor
        post.processed_at = dt.datetime.now(dt.timezone.utc)
        post.error = None
        if outcome.status == "extracted":
            insert_ideas(s, post, outcome.ideas, extractor=outcome.extractor,
                         prompt_version=outcome.prompt_version, effective_at=post.received_at)

    def _mark_post_failed(self, post_id: int | None, err: str) -> None:
        if post_id is None:
            return
        try:
            with session_scope(self.sf) as s:
                post = s.get(RawPost, int(post_id))
                if post is not None and post.status == "pending":
                    post.status = "failed"
                    post.error = err[:2000]
        except Exception:  # noqa: BLE001
            log.exception("could not mark post %s failed", post_id)

    # ----------------------------------------------------------- heartbeat
    def _heartbeat(self, every_s: float = 15.0) -> None:
        import time
        now = time.monotonic()
        if now - self._last_beat < every_s:
            return
        self._last_beat = now
        try:
            with session_scope(self.sf) as s:
                ts = dt.datetime.now(dt.timezone.utc)
                stmt = pg_insert(WorkerHeartbeat).values(
                    worker_id=self.worker_id, queue_backend=self.queue.backend,
                    started_at=self.started_at, last_seen=ts,
                    processed=self.processed, failed=self.failed)
                s.execute(stmt.on_conflict_do_update(
                    index_elements=["worker_id"],
                    set_={"last_seen": ts, "processed": self.processed, "failed": self.failed}))
        except Exception:  # noqa: BLE001 - heartbeats are best-effort
            log.debug("heartbeat failed", exc_info=True)

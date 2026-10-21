"""Idempotent ingestion: store the raw post, then make sure a job exists for it.

Idempotency has two layers:
  1. `dedupe_key` is UNIQUE, and we INSERT ... ON CONFLICT DO NOTHING. Re-polling a
     source, a client retrying a timed-out request, or two API replicas receiving the
     same post can never create duplicates. The DB constraint is the arbiter, not an
     "if exists" check (that would race).
  2. The worker re-checks post status before writing ideas, so a duplicated *job*
     (at-least-once delivery) can't create duplicate ideas either.

The dual-write problem: with the Redis backend, "save post" (Postgres) and "enqueue"
(Redis) are two systems, with no shared transaction. If the process dies between them,
the post is saved but never processed. `requeue_stale_pending` is the repair loop for that.
With the Postgres backend the job is inserted in the same transaction, so the gap
doesn't exist. This is the main correctness argument for the Postgres queue.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..db import session_scope
from ..models import Contributor, RawPost
from ..queue import JobQueue
from ..schemas import IngestResult, PostIn

log = logging.getLogger(__name__)


def dedupe_key(post: PostIn) -> str:
    """Stable identity of a post.

    Sources with native ids (Reddit, RSS guid, a client's Idempotency-Key) use them.
    Otherwise we fall back to a content hash: same author + same normalised text
    (+ same claimed timestamp, if any) counts as the same post.
    """
    if post.external_id:
        basis = f"{post.source}|id|{post.external_id}"
    else:
        norm = re.sub(r"\s+", " ", post.text.strip().lower())
        ts = post.posted_at.isoformat() if post.posted_at else ""
        basis = f"{post.source}|{post.author.lower()}|{ts}|{norm}"
    return hashlib.sha256(basis.encode()).hexdigest()


def upsert_contributor(session: Session, source: str, handle: str, *,
                       is_simulated: bool = False, true_skill: float | None = None) -> int:
    stmt = (pg_insert(Contributor)
            .values(source=source, handle=handle, is_simulated=is_simulated, true_skill=true_skill)
            .on_conflict_do_nothing(constraint="uq_contributor_source_handle")
            .returning(Contributor.id))
    cid = session.execute(stmt).scalar_one_or_none()
    if cid is None:  # already existed (or a concurrent insert won the race)
        cid = session.execute(select(Contributor.id).where(
            Contributor.source == source, Contributor.handle == handle)).scalar_one()
    return cid


class IngestionService:
    def __init__(self, session_factory: sessionmaker[Session], queue: JobQueue):
        self.sf = session_factory
        self.queue = queue

    def ingest(self, post: PostIn, *, received_at: dt.datetime | None = None) -> IngestResult:
        """`received_at` may only be overridden by trusted backfills; the API never exposes it."""
        key = dedupe_key(post)
        job_id: str | None = None
        with session_scope(self.sf) as s:
            contributor_id = upsert_contributor(s, post.source, post.author)
            values = dict(source=post.source, external_id=post.external_id, dedupe_key=key,
                          contributor_id=contributor_id, text=post.text, url=post.url,
                          posted_at=post.posted_at)
            if received_at is not None:
                values["received_at"] = received_at
            post_id = s.execute(
                pg_insert(RawPost).values(**values)
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(RawPost.id)
            ).scalar_one_or_none()

            if post_id is None:
                existing = s.execute(select(RawPost.id, RawPost.status)
                                     .where(RawPost.dedupe_key == key)).one()
                return IngestResult(post_id=existing.id, status=existing.status, duplicate=True)

            if self.queue.transactional_enqueue:
                job_id = self.queue.enqueue({"post_id": post_id}, session=s)
                s.execute(update(RawPost).where(RawPost.id == post_id)
                          .values(last_enqueued_at=text("now()")))
        # --- committed. For non-transactional queues, enqueue now (dual write). ---
        if not self.queue.transactional_enqueue:
            job_id = self._enqueue_after_commit(post_id)
        return IngestResult(post_id=post_id, status="pending", duplicate=False, job_id=job_id)

    def _enqueue_after_commit(self, post_id: int) -> str | None:
        try:
            job_id = self.queue.enqueue({"post_id": post_id})
        except Exception:  # noqa: BLE001 - the sweeper will pick it up
            log.exception("enqueue failed for post %s; sweeper will retry", post_id)
            return None
        with session_scope(self.sf) as s:
            s.execute(update(RawPost).where(RawPost.id == post_id)
                      .values(last_enqueued_at=text("now()")))
        return job_id

    # Posts that produced no ideas can be sent through extraction again (after a prompt
    # fix, a model change, or a provider outage). Extracted posts can't: ideas are append-only.
    REQUEUEABLE = ("no_idea", "needs_review", "failed", "rejected")

    def requeue(self, post_ids: list[int]) -> dict[int, str]:
        """Reset eligible posts to pending and enqueue them. Returns {post_id: outcome}."""
        out: dict[int, str] = {}
        for pid in post_ids:
            with session_scope(self.sf) as s:
                post = s.execute(select(RawPost).where(RawPost.id == pid).with_for_update()).scalar_one_or_none()
                if post is None:
                    out[pid] = "not found"
                    continue
                if post.status not in self.REQUEUEABLE:
                    out[pid] = f"skipped: status is {post.status} (posts with ideas can't be redone)"
                    continue
                post.status, post.error = "pending", None
                if self.queue.transactional_enqueue:
                    self.queue.enqueue({"post_id": pid}, session=s)
                    post.last_enqueued_at = dt.datetime.now(dt.timezone.utc)
            if not self.queue.transactional_enqueue:
                self._enqueue_after_commit(pid)
            out[pid] = "re-queued"
        return out

    def requeue_stale_pending(self, stale_after_s: int = 600, limit: int = 500) -> int:
        """Repair loop for the dual-write gap: re-enqueue posts that are still pending
        but were never enqueued, or were enqueued long ago. Duplicated jobs are harmless
        because the worker is idempotent."""
        if self.queue.transactional_enqueue:
            return 0  # job and post commit atomically; there is no gap to repair
        with session_scope(self.sf) as s:
            ids = s.execute(text(
                """
                SELECT id FROM raw_posts
                WHERE status = 'pending'
                  AND ( (last_enqueued_at IS NULL AND received_at < now() - interval '1 minute')
                     OR last_enqueued_at < now() - make_interval(secs => :stale) )
                ORDER BY id LIMIT :n
                """), {"stale": stale_after_s, "n": limit}).scalars().all()
        for pid in ids:
            self._enqueue_after_commit(pid)
        if ids:
            log.info("sweeper re-enqueued %d posts", len(ids))
        return len(ids)

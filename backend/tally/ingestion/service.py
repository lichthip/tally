# =============================================================================
# ingestion/service.py: SAVE A POST SAFELY, AND MAKE SURE A JOB EXISTS FOR IT
# -----------------------------------------------------------------------------
# This is step 2 of the happy path ("filter & store"). Input: a PostIn. Output: a row
# in raw_posts with status "pending", and a job on the queue telling a worker to
# process it.
#
# Key word: IDEMPOTENT = doing it twice has the same effect as doing it once.
# Reddit is polled every hour and returns many of the same posts each time; a browser
# might retry a submit. None of that may create duplicates.
#
# Called by: runtime.poll() (sources), api/routes.py POST /api/posts (website),
#            __main__.py `submit`. Calls: the queue (enqueue), the database.
# =============================================================================
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
import hashlib          # sha256 hashing
import logging
import re

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert   # Postgres INSERT with ON CONFLICT support
from sqlalchemy.orm import Session, sessionmaker

from ..db import session_scope
from ..models import Contributor, RawPost
from ..queue import JobQueue
from ..schemas import IngestResult, PostIn

log = logging.getLogger(__name__)


# Build the post's fingerprint. Same post -> same key, always. sha256 turns any text
# into a fixed 64-character hex string; we use it so the key has a fixed size and an
# index on it stays small, whatever the post length.
def dedupe_key(post: PostIn) -> str:
    """Stable identity of a post.

    Sources with native ids (Reddit, RSS guid, a client's Idempotency-Key) use them.
    Otherwise we fall back to a content hash: same author + same normalised text
    (+ same claimed timestamp, if any) counts as the same post.
    """
    if post.external_id:
        # Best case: the site gave the post its own id. Source is included because
        # two different sites could both have a post with id "123".
        basis = f"{post.source}|id|{post.external_id}"
    else:
        # No id: lowercase, squash whitespace, so trivial differences don't matter.
        norm = re.sub(r"\s+", " ", post.text.strip().lower())
        ts = post.posted_at.isoformat() if post.posted_at else ""
        basis = f"{post.source}|{post.author.lower()}|{ts}|{norm}"
    return hashlib.sha256(basis.encode()).hexdigest()


# "Upsert" = insert if new, otherwise use the existing row. Returns the contributor's id.
# The * in the signature means is_simulated/true_skill must be passed by name.
def upsert_contributor(session: Session, source: str, handle: str, *,
                       is_simulated: bool = False, true_skill: float | None = None) -> int:
    # INSERT ... ON CONFLICT (source, handle) DO NOTHING RETURNING id
    # If the contributor already exists, Postgres skips the insert and returns nothing.
    stmt = (pg_insert(Contributor)
            .values(source=source, handle=handle, is_simulated=is_simulated, true_skill=true_skill)
            .on_conflict_do_nothing(constraint="uq_contributor_source_handle")
            .returning(Contributor.id))
    cid = session.execute(stmt).scalar_one_or_none()    # the new id, or None if it already existed
    if cid is None:  # already existed (or a concurrent insert won the race)
        cid = session.execute(select(Contributor.id).where(
            Contributor.source == source, Contributor.handle == handle)).scalar_one()
    return cid


# A "service" class: holds its dependencies (session factory + queue), given to it from
# outside (dependency injection, done in runtime.py). It doesn't create them itself,
# so tests can pass in a fake queue.
class IngestionService:
    def __init__(self, session_factory: sessionmaker[Session], queue: JobQueue):
        self.sf = session_factory
        self.queue = queue

    # THE main function of this file. Walk-through:
    #   1. compute the fingerprint
    #   2. in ONE transaction: upsert the contributor, insert the post (or detect the
    #      duplicate), and (Postgres queue) insert the job in the same transaction
    #   3. (Redis queue only) after committing, push the job to Redis
    def ingest(self, post: PostIn, *, received_at: dt.datetime | None = None) -> IngestResult:
        """`received_at` may only be overridden by trusted backfills; the API never exposes it."""
        key = dedupe_key(post)
        job_id: str | None = None
        with session_scope(self.sf) as s:                    # transaction starts
            contributor_id = upsert_contributor(s, post.source, post.author)
            values = dict(source=post.source, external_id=post.external_id, dedupe_key=key,
                          contributor_id=contributor_id, text=post.text, url=post.url,
                          posted_at=post.posted_at)
            # Normally received_at is left out, so Postgres stamps it with now() (OUR clock).
            if received_at is not None:
                values["received_at"] = received_at
            # INSERT ... ON CONFLICT (dedupe_key) DO NOTHING RETURNING id
            # Returns the new id, or None if a post with this key already exists.
            post_id = s.execute(
                pg_insert(RawPost).values(**values)
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(RawPost.id)
            ).scalar_one_or_none()

            # Duplicate: look up the existing post and report it. No new job.
            if post_id is None:
                existing = s.execute(select(RawPost.id, RawPost.status)
                                     .where(RawPost.dedupe_key == key)).one()
                return IngestResult(post_id=existing.id, status=existing.status, duplicate=True)

            # Postgres queue: insert the job using the SAME session `s`, so the post and
            # its job are committed together. Either both exist or neither does.
            if self.queue.transactional_enqueue:
                job_id = self.queue.enqueue({"post_id": post_id}, session=s)
                s.execute(update(RawPost).where(RawPost.id == post_id)
                          .values(last_enqueued_at=text("now()")))
        # --- committed. For non-transactional queues, enqueue now (dual write). ---
        # (Redis can't join a Postgres transaction, so we push to Redis only after the
        # post is safely saved. If we crash right here, the sweeper fixes it later.)
        if not self.queue.transactional_enqueue:
            job_id = self._enqueue_after_commit(post_id)
        return IngestResult(post_id=post_id, status="pending", duplicate=False, job_id=job_id)

    # Redis path: push a job, then note when we did it. If the push fails, we log it
    # and return None; requeue_stale_pending (the sweeper) will retry later.
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

    # Used by `python -m tally reprocess` and the System tab's "retry failed" button.
    def requeue(self, post_ids: list[int]) -> dict[int, str]:
        """Reset eligible posts to pending and enqueue them. Returns {post_id: outcome}."""
        out: dict[int, str] = {}
        for pid in post_ids:
            with session_scope(self.sf) as s:
                # with_for_update() = lock this row until we commit, so a worker can't
                # change it at the same moment.
                post = s.execute(select(RawPost).where(RawPost.id == pid).with_for_update()).scalar_one_or_none()
                if post is None:
                    out[pid] = "not found"
                    continue
                if post.status not in self.REQUEUEABLE:
                    out[pid] = f"skipped: status is {post.status} (posts with ideas can't be redone)"
                    continue
                post.status, post.error = "pending", None    # set two attributes at once
                if self.queue.transactional_enqueue:
                    self.queue.enqueue({"post_id": pid}, session=s)
                    post.last_enqueued_at = dt.datetime.now(dt.timezone.utc)
            if not self.queue.transactional_enqueue:
                self._enqueue_after_commit(pid)
            out[pid] = "re-queued"
        return out

    # THE SWEEPER (only does anything with Redis). The scheduler calls it every 2 minutes.
    # Finds posts stuck in "pending" that were never enqueued (crash in the gap) or were
    # enqueued >10 minutes ago, and enqueues them again. A duplicate job is harmless
    # because the worker skips posts that are no longer pending.
    def requeue_stale_pending(self, stale_after_s: int = 600, limit: int = 500) -> int:
        """Repair loop for the dual-write gap: re-enqueue posts that are still pending
        but were never enqueued, or were enqueued long ago. Duplicated jobs are harmless
        because the worker is idempotent."""
        if self.queue.transactional_enqueue:
            return 0  # job and post commit atomically; there is no gap to repair
        with session_scope(self.sf) as s:
            # Raw SQL with :named parameters (filled from the dict, which prevents SQL injection).
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

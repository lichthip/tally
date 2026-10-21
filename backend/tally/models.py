# =============================================================================
# models.py: EVERY DATABASE TABLE, WRITTEN AS A PYTHON CLASS
# -----------------------------------------------------------------------------
# This is "ORM" (Object-Relational Mapping): each class = one table, each attribute
# = one column, each object = one row. Instead of writing SQL like
#     INSERT INTO raw_posts (text, ...) VALUES (...)
# the code can write  RawPost(text=...)  and SQLAlchemy produces the SQL.
#
# How to read a column line:
#     text: Mapped[str] = mapped_column(Text)
#     ^name  ^Python type  ^database column type and options
#   Mapped[str | None] + nullable=True  -> the column may be empty (NULL)
#   primary_key=True                    -> the row's unique id
#   ForeignKey("contributors.id")       -> must point at an existing contributor row
#   server_default=...                  -> Postgres fills it in if we don't
#
# The tables fall into 4 groups (see the section headers below):
#   1. FACTS        contributors, raw_posts, trade_ideas: what happened; never rewritten
#   2. DERIVED      idea_marks, contributor_stats, signal_snapshots, scoring_runs:
#                   computed by the scoring job; can be deleted and rebuilt any time
#   3. REFERENCE /  symbols (valid tickers), prices + price_coverage (Yahoo cache),
#      CACHE        llm_cache (saved LLM answers)
#   4. INFRA        jobs (the Postgres queue), worker_heartbeats (System tab)
#
# Who uses it: almost every module imports the classes it reads or writes.
# db.py's init_db() turns these classes into real tables (CREATE TABLE).
# =============================================================================
"""Database schema (SQLAlchemy 2.0 ORM).

Design rules encoded here:
  * raw_posts is deduplicated by `dedupe_key` (UNIQUE) -> ingestion is idempotent.
  * trade_ideas is APPEND-ONLY. A Postgres trigger (see db.py) rejects UPDATE/DELETE,
    so nobody (including a buggy scoring job) can rewrite history after the fact.
  * Everything the scoring job computes lives in separate, fully re-computable tables
    (idea_marks, contributor_stats, signal_snapshots). Derived data can be thrown away
    and rebuilt from the immutable facts at any time.
"""
from __future__ import annotations

import datetime as dt

# Column types and table-level rules (constraints/indexes) from SQLAlchemy.
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB   # Postgres column that stores JSON (searchable)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# Every table class inherits from Base. Base keeps a registry of all of them, which is
# how init_db() can say "create all tables" (Base.metadata.create_all).
class Base(DeclarativeBase):
    pass


# Small helper so we don't repeat ourselves: a timestamp column (with timezone) that
# Postgres fills with now() when the row is inserted. **kw passes extra options through.
def _now_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


# --------------------------------------------------------------------------- #
# Facts: who said what, and when we received it
# --------------------------------------------------------------------------- #
# A person/account that posts ideas, e.g. (source="reddit", handle="u/someone").
class Contributor(Base):
    __tablename__ = "contributors"     # the real table name in Postgres
    # The same handle on two different sites is two different people, so uniqueness is
    # on the PAIR (source, handle). The constraint name is used by upsert_contributor().
    __table_args__ = (UniqueConstraint("source", "handle", name="uq_contributor_source_handle"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))       # "reddit", "substack", "manual", "sim"
    handle: Mapped[str] = mapped_column(String(128))      # the username
    is_simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Only for simulated contributors: the true skill we planted, to validate the scorer.
    true_skill: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = _now_col()


# The life cycle of a post (a "state machine"):
#   pending -> extracted     (ideas found and saved)
#           -> no_idea       (the post isn't a trade call)
#           -> needs_review  (something looked off; a human decides) -> extracted / rejected
#           -> failed        (errored 5 times; dead-lettered)
POST_STATUSES = ("pending", "extracted", "no_idea", "needs_review", "rejected", "failed")


# One incoming post exactly as received, before any interpretation.
class RawPost(Base):
    __tablename__ = "raw_posts"
    __table_args__ = (
        # Postgres itself refuses any status not in the list above (a typo can't sneak in).
        CheckConstraint(f"status IN {POST_STATUSES}", name="ck_post_status"),
        # An index is like a book's index: it makes "find posts with status X, by time" fast.
        Index("ix_raw_posts_status_received", "status", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)   # the site's own id
    # sha256 hex string (64 chars). UNIQUE = the database refuses a second copy of a post.
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id"))
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # posted_at = what the SITE says the posting time was (could be wrong or edited).
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # received_at is OUR clock, not the author's claim. Scoring uses this (point-in-time).
    received_at: Mapped[dt.datetime] = _now_col()
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)        # last error, if failed
    extraction: Mapped[dict | None] = mapped_column(JSONB, nullable=True) # full pipeline result (notes, raw LLM output)
    extractor: Mapped[str | None] = mapped_column(String(128), nullable=True)  # which model processed it
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When we last put a job for this post on the queue (the Redis sweeper uses it).
    last_enqueued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # relationship() = convenience links: post.contributor gives the Contributor object,
    # post.ideas gives the list of TradeIdea objects. No extra columns are created.
    contributor: Mapped[Contributor] = relationship()
    ideas: Mapped[list["TradeIdea"]] = relationship(back_populates="post")


# One structured call pulled out of a post: "ticker X, LONG/SHORT, for N trading days".
# One post can produce several ideas ("long AMD, short INTC" -> 2 rows).
class TradeIdea(Base):
    """An extracted, validated trade idea. Immutable once written (DB trigger enforces it)."""

    __tablename__ = "trade_ideas"
    __table_args__ = (
        # At most ONE idea per (post, ticker). If a job is accidentally run twice, the
        # second insert hits this and does nothing -> no duplicate ideas (idempotency).
        UniqueConstraint("post_id", "ticker", name="uq_idea_post_ticker"),
        CheckConstraint("direction IN ('LONG','SHORT')", name="ck_idea_direction"),
        # 1 to 252 trading days (252 = roughly one year of trading days).
        CheckConstraint("horizon_days BETWEEN 1 AND 252", name="ck_idea_horizon"),
        Index("ix_trade_ideas_contributor", "contributor_id"),   # fast "all ideas by person X"
        Index("ix_trade_ideas_ticker", "ticker"),                # fast "all ideas on NVDA"
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("raw_posts.id"))
    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id"))
    ticker: Mapped[str] = mapped_column(String(24))
    direction: Mapped[str] = mapped_column(String(5))
    horizon_days: Mapped[int] = mapped_column(Integer)           # trading days
    horizon_inferred: Mapped[bool] = mapped_column(Boolean)      # True = post gave no horizon
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    conviction: Mapped[str | None] = mapped_column(String(8), nullable=True)   # low/medium/high
    confidence: Mapped[float] = mapped_column(Float)             # extractor's confidence
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    # effective_at = the timestamp used to pick the entry price. Equals received_at for
    # auto-accepted ideas; equals approval time for human-reviewed ones (no hindsight).
    effective_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    extractor: Mapped[str] = mapped_column(String(128))          # provenance: which model
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ...and which prompt
    created_at: Mapped[dt.datetime] = _now_col()

    post: Mapped[RawPost] = relationship(back_populates="ideas")
    contributor: Mapped[Contributor] = relationship()
    # uselist=False: an idea has ONE mark (or none yet). viewonly: never written through here.
    mark: Mapped["IdeaMark | None"] = relationship(uselist=False, viewonly=True)


# --------------------------------------------------------------------------- #
# Derived data: recomputable by the scoring job
# --------------------------------------------------------------------------- #
# Where an idea is in its life:
#   pending_entry = we haven't reached the next market open after the post yet
#   open          = entered, horizon not finished; return so far is provisional
#   closed        = horizon finished; final result, never changes again
#   unscorable    = missing price data etc. (kept and counted, NOT silently dropped)
MARK_STATUSES = ("pending_entry", "open", "closed", "unscorable")


# "Mark to market": how is this idea doing against the benchmark? One row per idea,
# updated by every scoring run until the idea is closed.
class IdeaMark(Base):
    __tablename__ = "idea_marks"
    __table_args__ = (CheckConstraint(f"status IN {MARK_STATUSES}", name="ck_mark_status"),)

    # The primary key IS the idea id -> exactly one mark per idea.
    idea_id: Mapped[int] = mapped_column(ForeignKey("trade_ideas.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    entry_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_ticker: Mapped[str | None] = mapped_column(String(24), nullable=True)   # SPY / ES3.SI / 2800.HK
    bench_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)        # latest (or exit) date
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_return: Mapped[float | None] = mapped_column(Float, nullable=True)        # stock's own return
    bench_return: Mapped[float | None] = mapped_column(Float, nullable=True)      # benchmark's return
    excess_return: Mapped[float | None] = mapped_column(Float, nullable=True)     # direction x (stock - bench)
    normalised_excess: Mapped[float | None] = mapped_column(Float, nullable=True) # scaled to a 20-day horizon
    unscorable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = _now_col(onupdate=func.now())   # onupdate: refreshed on every UPDATE


# One row per contributor: the leaderboard numbers. Rebuilt on every scoring run.
class ContributorStat(Base):
    __tablename__ = "contributor_stats"

    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id"), primary_key=True)
    n_ideas: Mapped[int] = mapped_column(Integer)
    n_closed: Mapped[int] = mapped_column(Integer)
    n_open: Mapped[int] = mapped_column(Integer)
    n_unscorable: Mapped[int] = mapped_column(Integer)
    hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)        # % of closed ideas that beat the benchmark
    mean_excess: Mapped[float | None] = mapped_column(Float, nullable=True)     # RAW average (fooled by luck)
    posterior_mean: Mapped[float | None] = mapped_column(Float, nullable=True)  # SHRUNK average (skill estimate)
    posterior_sd: Mapped[float | None] = mapped_column(Float, nullable=True)    # how unsure we are about it
    prob_positive: Mapped[float | None] = mapped_column(Float, nullable=True)   # P(true skill > 0)
    updated_at: Mapped[dt.datetime] = _now_col(onupdate=func.now())

    contributor: Mapped[Contributor] = relationship()


# A saved copy of the Signals page output (weights, VaR...) from each scoring run.
# JSONB because the shape is a nested document, not neat columns.
class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = _now_col()
    payload: Mapped[dict] = mapped_column(JSONB)


# A log line per scoring run (shown on the System tab): when, how much, any error.
class ScoringRun(Base):
    __tablename__ = "scoring_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = _now_col()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ideas_marked: Mapped[int] = mapped_column(Integer, server_default="0")
    contributors_scored: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Reference / cache data
# --------------------------------------------------------------------------- #
class Symbol(Base):
    """Symbol master: the only tickers we accept. The LLM proposes, this table disposes."""

    __tablename__ = "symbols"

    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)    # e.g. "NVDA", "D05.SI"
    name: Mapped[str] = mapped_column(String(256))                       # "NVIDIA Corporation"
    exchange: Mapped[str] = mapped_column(String(16))                    # "US", "SGX", "HKEX"
    # Other names people use: ["nvidia", "nvda"]. Stored as a JSON list.
    aliases: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))


class Price(Base):
    """Read-through cache of daily (split/dividend adjusted) bars."""

    __tablename__ = "prices"

    # Composite primary key: one row per (ticker, day).
    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float)     # entry uses the next OPEN after the post
    close: Mapped[float] = mapped_column(Float)    # exit uses the CLOSE at the end of the horizon


# Bookkeeping for the price cache: "for NVDA we hold dates A..B, downloaded at time T".
# That's how PriceStore decides whether it needs to call Yahoo at all.
class PriceCoverage(Base):
    __tablename__ = "price_coverage"

    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    covered_from: Mapped[dt.date] = mapped_column(Date)
    covered_to: Mapped[dt.date] = mapped_column(Date)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class LLMCacheEntry(Base):
    """Identical (model, prompt version, text) -> reuse the answer. Saves free-tier quota."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)   # sha256 of model|prompt|date|text
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict] = mapped_column(JSONB)                    # the LLM's JSON answer
    created_at: Mapped[dt.datetime] = _now_col()


# --------------------------------------------------------------------------- #
# Infrastructure: Postgres-backed job queue + worker heartbeats
# --------------------------------------------------------------------------- #
# pending = waiting; running = a worker holds a lease on it; done = acked;
# dead = failed max_attempts times (the "dead-letter" pile shown on the System tab).
JOB_STATUSES = ("pending", "running", "done", "dead")


# The queue IS this table. Enqueue = INSERT a row. Claim = grab a pending row with
# SELECT ... FOR UPDATE SKIP LOCKED (see queue/postgres_queue.py).
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"status IN {JOB_STATUSES}", name="ck_job_status"),
        # Partial index: the claim query only ever looks at live jobs, so keep the index
        # small no matter how many finished jobs pile up.
        # (postgresql_where = only rows matching this condition are indexed.)
        Index(
            "ix_jobs_claimable",
            "queue",
            "available_at",
            "id",
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue: Mapped[str] = mapped_column(String(64))         # queue name, e.g. "extract"
    payload: Mapped[dict] = mapped_column(JSONB)           # e.g. {"post_id": 42}
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    # How many times this job has been handed out. Also used as a "fencing token":
    # only the worker holding the CURRENT attempt number may ack it.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    # Don't hand the job out before this time. Used for retry backoff (wait 2s, 4s, 8s...).
    available_at: Mapped[dt.datetime] = _now_col()
    # While running: the worker owns the job until this time. If it crashes and the
    # lease expires, another worker may take the job.
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)   # which worker
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _now_col()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Each worker writes "I'm alive, processed N, failed M" every ~15s. The System tab
# shows these, so you can see if workers are running.
class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    queue_backend: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    processed: Mapped[int] = mapped_column(Integer, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, server_default="0")

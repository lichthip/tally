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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


# --------------------------------------------------------------------------- #
# Facts: who said what, and when we received it
# --------------------------------------------------------------------------- #
class Contributor(Base):
    __tablename__ = "contributors"
    __table_args__ = (UniqueConstraint("source", "handle", name="uq_contributor_source_handle"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    handle: Mapped[str] = mapped_column(String(128))
    is_simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Only for simulated contributors: the true skill we planted, to validate the scorer.
    true_skill: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = _now_col()


POST_STATUSES = ("pending", "extracted", "no_idea", "needs_review", "rejected", "failed")


class RawPost(Base):
    __tablename__ = "raw_posts"
    __table_args__ = (
        CheckConstraint(f"status IN {POST_STATUSES}", name="ck_post_status"),
        Index("ix_raw_posts_status_received", "status", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id"))
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # received_at is OUR clock, not the author's claim. Scoring uses this (point-in-time).
    received_at: Mapped[dt.datetime] = _now_col()
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extractor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_enqueued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    contributor: Mapped[Contributor] = relationship()
    ideas: Mapped[list["TradeIdea"]] = relationship(back_populates="post")


class TradeIdea(Base):
    """An extracted, validated trade idea. Immutable once written (DB trigger enforces it)."""

    __tablename__ = "trade_ideas"
    __table_args__ = (
        UniqueConstraint("post_id", "ticker", name="uq_idea_post_ticker"),
        CheckConstraint("direction IN ('LONG','SHORT')", name="ck_idea_direction"),
        CheckConstraint("horizon_days BETWEEN 1 AND 252", name="ck_idea_horizon"),
        Index("ix_trade_ideas_contributor", "contributor_id"),
        Index("ix_trade_ideas_ticker", "ticker"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("raw_posts.id"))
    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id"))
    ticker: Mapped[str] = mapped_column(String(24))
    direction: Mapped[str] = mapped_column(String(5))
    horizon_days: Mapped[int] = mapped_column(Integer)           # trading days
    horizon_inferred: Mapped[bool] = mapped_column(Boolean)      # True = post gave no horizon
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    conviction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)             # extractor's confidence
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    # effective_at = the timestamp used to pick the entry price. Equals received_at for
    # auto-accepted ideas; equals approval time for human-reviewed ones (no hindsight).
    effective_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    extractor: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = _now_col()

    post: Mapped[RawPost] = relationship(back_populates="ideas")
    contributor: Mapped[Contributor] = relationship()
    mark: Mapped["IdeaMark | None"] = relationship(uselist=False, viewonly=True)


# --------------------------------------------------------------------------- #
# Derived data: recomputable by the scoring job
# --------------------------------------------------------------------------- #
MARK_STATUSES = ("pending_entry", "open", "closed", "unscorable")


class IdeaMark(Base):
    __tablename__ = "idea_marks"
    __table_args__ = (CheckConstraint(f"status IN {MARK_STATUSES}", name="ck_mark_status"),)

    idea_id: Mapped[int] = mapped_column(ForeignKey("trade_ideas.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    entry_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_ticker: Mapped[str | None] = mapped_column(String(24), nullable=True)
    bench_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    excess_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    normalised_excess: Mapped[float | None] = mapped_column(Float, nullable=True)
    unscorable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = _now_col(onupdate=func.now())


class ContributorStat(Base):
    __tablename__ = "contributor_stats"

    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id"), primary_key=True)
    n_ideas: Mapped[int] = mapped_column(Integer)
    n_closed: Mapped[int] = mapped_column(Integer)
    n_open: Mapped[int] = mapped_column(Integer)
    n_unscorable: Mapped[int] = mapped_column(Integer)
    hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_excess: Mapped[float | None] = mapped_column(Float, nullable=True)
    posterior_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    posterior_sd: Mapped[float | None] = mapped_column(Float, nullable=True)
    prob_positive: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[dt.datetime] = _now_col(onupdate=func.now())

    contributor: Mapped[Contributor] = relationship()


class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = _now_col()
    payload: Mapped[dict] = mapped_column(JSONB)


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

    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    exchange: Mapped[str] = mapped_column(String(16))
    aliases: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))


class Price(Base):
    """Read-through cache of daily (split/dividend adjusted) bars."""

    __tablename__ = "prices"

    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)


class PriceCoverage(Base):
    __tablename__ = "price_coverage"

    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    covered_from: Mapped[dt.date] = mapped_column(Date)
    covered_to: Mapped[dt.date] = mapped_column(Date)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class LLMCacheEntry(Base):
    """Identical (model, prompt version, text) -> reuse the answer. Saves free-tier quota."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = _now_col()


# --------------------------------------------------------------------------- #
# Infrastructure: Postgres-backed job queue + worker heartbeats
# --------------------------------------------------------------------------- #
JOB_STATUSES = ("pending", "running", "done", "dead")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"status IN {JOB_STATUSES}", name="ck_job_status"),
        # Partial index: the claim query only ever looks at live jobs, so keep the index
        # small no matter how many finished jobs pile up.
        Index(
            "ix_jobs_claimable",
            "queue",
            "available_at",
            "id",
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[dt.datetime] = _now_col()
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _now_col()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    queue_backend: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    processed: Mapped[int] = mapped_column(Integer, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, server_default="0")

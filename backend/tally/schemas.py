# =============================================================================
# schemas.py: THE SHAPE OF DATA COMING IN FROM OUTSIDE
# -----------------------------------------------------------------------------
# models.py describes database TABLES. This file describes data CROSSING A BOUNDARY:
# a post arriving from Reddit, Substack, or the HTTP API.
# Pydantic (BaseModel) checks the data as it's created: wrong types, missing fields,
# or a 1 MB post are rejected with a clear error before they reach our logic.
#
# PostIn is the "common language" every source speaks (the Adapter pattern): Reddit,
# Substack and the website form all produce a PostIn, so ingestion only needs to
# understand ONE shape.
#
# Used by: sources/* (create PostIn), ingestion/service.py (consumes it),
#          api/routes.py (returns IngestResult).
# =============================================================================
"""Pydantic models for data crossing a boundary (HTTP API, source adapters)."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator


class PostIn(BaseModel):
    """A raw post from any source, normalised to one shape (the adapter pattern's target)."""

    # Field(...) adds validation rules. Text must be 3..5000 characters.
    text: str = Field(min_length=3, max_length=5000)
    author: str = Field(min_length=1, max_length=128)
    # Lowercase letters/digits/_/- only (a regex "pattern"). Defaults to "manual" (typed on the site).
    source: str = Field(default="manual", max_length=32, pattern=r"^[a-z0-9_\-]+$")
    # The site's own id for this post, if it has one. Used for deduplication.
    external_id: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=2000)
    posted_at: dt.datetime | None = None
    # Source-specific extras for display (e.g. StockTwits' author Bullish/Bearish tag).
    # Not stored and not shown to the LLM.
    meta: dict | None = None

    # Clean up the author name: "u/alice" (Reddit style) and "@alice" both become "alice",
    # so the same person isn't counted as two contributors.
    @field_validator("author")
    @classmethod
    def _clean_author(cls, v: str) -> str:
        return v.strip().removeprefix("u/").removeprefix("@")

    # A datetime without a timezone is ambiguous (10am WHERE?). If one arrives without
    # a timezone, we declare it UTC so every timestamp in the system is comparable.
    @field_validator("posted_at")
    @classmethod
    def _tz_aware(cls, v: dt.datetime | None) -> dt.datetime | None:
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return v


# What ingest() returns: the post's id, its status, whether we'd seen it before,
# and the queue job id (if a new job was created).
class IngestResult(BaseModel):
    post_id: int
    status: str
    duplicate: bool
    job_id: str | None = None

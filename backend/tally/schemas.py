"""Pydantic models for data crossing a boundary (HTTP API, source adapters)."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator


class PostIn(BaseModel):
    """A raw post from any source, normalised to one shape (the adapter pattern's target)."""

    text: str = Field(min_length=3, max_length=5000)
    author: str = Field(min_length=1, max_length=128)
    source: str = Field(default="manual", max_length=32, pattern=r"^[a-z0-9_\-]+$")
    external_id: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=2000)
    posted_at: dt.datetime | None = None
    # Source-specific extras for display (e.g. StockTwits' author Bullish/Bearish tag).
    # Not stored and not shown to the LLM.
    meta: dict | None = None

    @field_validator("author")
    @classmethod
    def _clean_author(cls, v: str) -> str:
        return v.strip().removeprefix("u/").removeprefix("@")

    @field_validator("posted_at")
    @classmethod
    def _tz_aware(cls, v: dt.datetime | None) -> dt.datetime | None:
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return v


class IngestResult(BaseModel):
    post_id: int
    status: str
    duplicate: bool
    job_id: str | None = None

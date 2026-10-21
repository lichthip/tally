"""Typed shapes for extraction.

`RawExtraction` validates whatever an extractor returned (LLM output is untrusted input!).
`IdeaCandidate` / `ExtractionOutcome` are what the rest of the system consumes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

_DIRECTION_SYNONYMS = {
    "LONG": "LONG", "BUY": "LONG", "BULLISH": "LONG", "UP": "LONG",
    "SHORT": "SHORT", "SELL": "SHORT", "BEARISH": "SHORT", "DOWN": "SHORT",
}


class RawIdea(BaseModel):
    ticker: str | None = None
    company_name: str | None = None
    direction: Literal["LONG", "SHORT"]
    horizon_days: int | None = Field(default=None, ge=0, le=3650)
    target_date: dt.date | None = None
    target_price: float | None = Field(default=None, gt=0)
    conviction: Literal["low", "medium", "high"] = "medium"
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""

    @field_validator("direction", mode="before")
    @classmethod
    def _norm_direction(cls, v: Any) -> Any:
        if isinstance(v, str):
            return _DIRECTION_SYNONYMS.get(v.strip().upper(), v)
        return v

    @field_validator("conviction", mode="before")
    @classmethod
    def _norm_conviction(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else (v or "medium")

    @field_validator("target_date", mode="before")
    @classmethod
    def _lenient_date(cls, v: Any) -> Any:
        # A malformed date shouldn't sink the whole extraction; drop it instead.
        if isinstance(v, str):
            try:
                return dt.date.fromisoformat(v.strip()[:10])
            except ValueError:
                return None
        return v

    @field_validator("ticker", "company_name", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
            return None
        return v


class RawExtraction(BaseModel):
    is_trade_idea: bool
    reason: str = ""
    ideas: list[RawIdea] = Field(default_factory=list, max_length=10)


@dataclass
class IdeaCandidate:
    ticker: str | None          # None = could not be resolved to a known symbol
    direction: str
    horizon_days: int           # trading days
    horizon_inferred: bool
    target_price: float | None
    conviction: str
    confidence: float
    rationale: str
    company_name: str | None = None


@dataclass
class ExtractionOutcome:
    status: Literal["extracted", "no_idea", "needs_review"]
    ideas: list[IdeaCandidate]
    notes: list[str]
    extractor: str
    prompt_version: str | None
    raw: dict[str, Any] | None = None
    cached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidates": [asdict(i) for i in self.ideas],
            "notes": self.notes,
            "raw": self.raw,
            "cached": self.cached,
            "prompt_version": self.prompt_version,
        }

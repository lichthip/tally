# =============================================================================
# extraction/types.py: THE DATA SHAPES USED DURING EXTRACTION
# -----------------------------------------------------------------------------
# Three stages of the same information:
#   RawIdea / RawExtraction  = what the LLM (or the keyword baseline) RETURNED, after
#                              Pydantic checks it. LLM output is untrusted, like user
#                              input: it can be missing fields or have wrong types.
#   IdeaCandidate            = one idea AFTER our checks (ticker resolved, horizon in
#                              trading days). Ready to be saved.
#   ExtractionOutcome        = the final verdict for a whole post: status + candidates
#                              + notes explaining why. The worker saves this.
#
# BaseModel (Pydantic) = validates input. @dataclass = a plain data holder (no validation
# needed, because we build these ourselves from already-validated data).
# =============================================================================
"""Typed shapes for extraction.

`RawExtraction` validates whatever an extractor returned (LLM output is untrusted input!).
`IdeaCandidate` / `ExtractionOutcome` are what the rest of the system consumes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# The LLM might say "BUY" or "bullish" instead of "LONG". Map all of them to our two words.
_DIRECTION_SYNONYMS = {
    "LONG": "LONG", "BUY": "LONG", "BULLISH": "LONG", "UP": "LONG",
    "SHORT": "SHORT", "SELL": "SHORT", "BEARISH": "SHORT", "DOWN": "SHORT",
}


# One idea as the extractor described it. If any rule here fails (e.g. confidence = 7),
# Pydantic raises a ValidationError and the pipeline sends the post to review.
class RawIdea(BaseModel):
    ticker: str | None = None
    company_name: str | None = None
    direction: Literal["LONG", "SHORT"]                         # must be exactly one of these
    horizon_days: int | None = Field(default=None, ge=0, le=3650)   # ge = >=, le = <=  (max 10 years)
    target_date: dt.date | None = None
    target_price: float | None = Field(default=None, gt=0)      # gt = strictly > 0
    conviction: Literal["low", "medium", "high"] = "medium"
    confidence: float = Field(ge=0.0, le=1.0)                   # a probability between 0 and 1
    rationale: str = ""

    # mode="before": this runs BEFORE the type check, so we can fix "buy " -> "LONG"
    # first, and only then check it's LONG or SHORT.
    @field_validator("direction", mode="before")
    @classmethod
    def _norm_direction(cls, v: Any) -> Any:
        if isinstance(v, str):
            return _DIRECTION_SYNONYMS.get(v.strip().upper(), v)
        return v

    # "High " -> "high"; missing -> "medium".
    @field_validator("conviction", mode="before")
    @classmethod
    def _norm_conviction(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else (v or "medium")

    @field_validator("target_date", mode="before")
    @classmethod
    def _lenient_date(cls, v: Any) -> Any:
        # A malformed date shouldn't sink the whole extraction; drop it instead.
        # [:10] keeps "2026-06-30" even if the model wrote "2026-06-30T00:00:00".
        if isinstance(v, str):
            try:
                return dt.date.fromisoformat(v.strip()[:10])
            except ValueError:
                return None
        return v

    # Models sometimes write the STRING "null" or "N/A" instead of a real null. Treat as empty.
    @field_validator("ticker", "company_name", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
            return None
        return v


# The whole answer for one post: "is this a trade idea?", why, and the ideas (max 10;
# more than that is almost certainly garbage or a prompt-injection attempt).
class RawExtraction(BaseModel):
    is_trade_idea: bool
    reason: str = ""
    ideas: list[RawIdea] = Field(default_factory=list, max_length=10)


# One idea after OUR checks. Built by ExtractionPipeline._build_candidates.
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


# The pipeline's verdict for one post. The worker copies status onto the post, saves
# to_json() into raw_posts.extraction (for the Review tab), and inserts the ideas.
@dataclass
class ExtractionOutcome:
    status: Literal["extracted", "no_idea", "needs_review"]
    ideas: list[IdeaCandidate]
    notes: list[str]                  # human-readable reasons, shown on the Review tab
    extractor: str                    # e.g. "groq:openai/gpt-oss-120b"
    prompt_version: str | None
    raw: dict[str, Any] | None = None # the extractor's raw answer, kept for debugging/audit
    cached: bool = False              # True if the LLM answer came from llm_cache
    extra: dict[str, Any] = field(default_factory=dict)

    # Convert to a plain dict that can be stored as JSON. asdict() turns a dataclass into a dict.
    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidates": [asdict(i) for i in self.ideas],
            "notes": self.notes,
            "raw": self.raw,
            "cached": self.cached,
            "prompt_version": self.prompt_version,
        }

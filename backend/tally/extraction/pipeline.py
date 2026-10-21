"""Extraction pipeline: run an extractor, then refuse to trust its output blindly.

    text -> extractor (LLM or rules) -> schema validation -> symbol resolution
         -> horizon normalisation -> confidence gate -> outcome

Outcomes:
  extracted     every candidate passed validation; ideas get written
  no_idea       the post contains no trade idea (questions, sarcasm, news...)
  needs_review  something is off (unknown ticker, low confidence, malformed output,
                contradictory output); a human decides
"""
from __future__ import annotations

import datetime as dt
import logging

from pydantic import ValidationError

from .extractors import IdeaExtractor
from .llm_clients import LLMBadOutput
from .symbols import SymbolResolver
from .types import ExtractionOutcome, IdeaCandidate, RawExtraction

log = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


def calendar_to_trading_days(calendar_days: int) -> int:
    return max(1, min(TRADING_DAYS_PER_YEAR, round(calendar_days * TRADING_DAYS_PER_YEAR / 365)))


class ExtractionPipeline:
    def __init__(self, extractor: IdeaExtractor, resolver: SymbolResolver,
                 confidence_threshold: float = 0.6, default_horizon_days: int = 20,
                 second_opinion: IdeaExtractor | None = None):
        """`second_opinion`: an independent, cheap extractor (the keyword baseline) that is
        consulted only when the main extractor says "no idea". If the two disagree, the post
        goes to human review instead of being silently dropped."""
        self.extractor = extractor
        self.resolver = resolver
        self.threshold = confidence_threshold
        self.default_horizon = default_horizon_days
        self.second_opinion = second_opinion

    def run(self, text: str, posted_at: dt.datetime) -> ExtractionOutcome:
        ex = self.extractor
        base = dict(extractor=ex.id, prompt_version=ex.prompt_version)
        try:
            raw = ex.extract(text, posted_at)
        except LLMBadOutput as e:
            # Deterministic failure (non-JSON, safety block): retrying won't help.
            return ExtractionOutcome("needs_review", [], [f"bad model output: {e}"], **base)
        cached = bool(raw.pop("_cached", False))

        try:
            parsed = RawExtraction.model_validate(raw)
        except ValidationError as e:
            return ExtractionOutcome("needs_review", [], [f"schema validation failed: {e.errors()[:3]}"],
                                     raw=raw, cached=cached, **base)

        if not parsed.is_trade_idea:
            if parsed.ideas:
                return ExtractionOutcome("needs_review", [], ["contradictory output: is_trade_idea=false "
                                                              "but ideas were returned"],
                                         raw=raw, cached=cached, **base)
            disagreement = self._second_opinion(text, posted_at)
            if disagreement is not None:
                candidates, found = disagreement
                return ExtractionOutcome(
                    "needs_review", candidates,
                    [f"LLM: no idea ({parsed.reason or 'no reason given'})",
                     f"keyword baseline disagrees, found: {found}. Sent to human review "
                     "rather than silently dropped"],
                    raw=raw, cached=cached, **base)
            return ExtractionOutcome("no_idea", [], [parsed.reason or "no trade idea"],
                                     raw=raw, cached=cached, **base)
        if not parsed.ideas:
            return ExtractionOutcome("needs_review", [], ["is_trade_idea=true but no ideas extracted"],
                                     raw=raw, cached=cached, **base)

        candidates, notes, flagged = self._build_candidates(parsed.ideas, posted_at)
        status = "needs_review" if flagged else "extracted"
        return ExtractionOutcome(status, candidates, notes, raw=raw, cached=cached, **base)

    def _second_opinion(self, text: str, posted_at: dt.datetime):
        """Returns (candidates, description) if the baseline sees an idea the LLM didn't."""
        if self.second_opinion is None:
            return None
        try:
            other = RawExtraction.model_validate(self.second_opinion.extract(text, posted_at))
        except (ValidationError, LLMBadOutput):
            return None
        if not other.is_trade_idea or not other.ideas:
            return None
        candidates, _, _ = self._build_candidates(other.ideas, posted_at)
        found = ", ".join(f"{c.ticker or c.company_name} {c.direction}" for c in candidates)
        return candidates, found

    def _build_candidates(self, ideas, posted_at: dt.datetime) -> tuple[list[IdeaCandidate], list[str], bool]:
        candidates: list[IdeaCandidate] = []
        notes: list[str] = []
        flagged = False
        seen: set[str] = set()
        for idea in ideas:
            ticker, how = self.resolver.resolve(idea.ticker, idea.company_name)
            if ticker is None:
                flagged = True
                notes.append(f"unresolved symbol: {how}")
            elif ticker in seen:
                notes.append(f"duplicate mention of {ticker} merged")
                continue
            else:
                seen.add(ticker)
                if how != "symbol master":
                    notes.append(f"{ticker} resolved via {how}")

            horizon, inferred = self._horizon(idea.horizon_days, idea.target_date, posted_at)
            if idea.confidence < self.threshold:
                flagged = True
                notes.append(f"low confidence {idea.confidence:.2f} for {ticker or idea.company_name}")
            candidates.append(IdeaCandidate(
                ticker=ticker, direction=idea.direction, horizon_days=horizon,
                horizon_inferred=inferred, target_price=idea.target_price,
                conviction=idea.conviction, confidence=idea.confidence,
                rationale=idea.rationale[:500], company_name=idea.company_name))
        return candidates, notes, flagged

    def _horizon(self, horizon_days: int | None, target_date: dt.date | None,
                 posted_at: dt.datetime) -> tuple[int, bool]:
        """Returns (trading days, inferred?). Explicit date beats duration beats default."""
        if target_date is not None:
            cal = (target_date - posted_at.date()).days
            if cal > 0:
                return calendar_to_trading_days(cal), False
        if horizon_days:
            return calendar_to_trading_days(horizon_days), False
        return self.default_horizon, True

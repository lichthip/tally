# =============================================================================
# extraction/pipeline.py: STEPS 3+4: EXTRACT, THEN CHECK EVERYTHING
# -----------------------------------------------------------------------------
# The worker calls pipeline.run(text, posted_at) and gets an ExtractionOutcome back.
# Core idea: NEVER TRUST THE LLM BLINDLY. Its answer passes through several gates:
#   1. Did it return valid JSON?           no -> needs_review
#   2. Does it match our schema (Pydantic)? no -> needs_review
#   3. Is it self-consistent?   ("not an idea" but lists ideas?) -> needs_review
#   4. LLM says "no idea"? Ask the keyword baseline (second opinion). If the baseline
#      DOES see an idea, they disagree -> needs_review (instead of silently dropping it)
#   5. For each idea: is the ticker real? (SymbolResolver) no -> needs_review
#   6. Convert the horizon to trading days (default 20 if none given)
#   7. Confidence below 0.6? -> needs_review
#   Everything passed -> "extracted".
#
# Notice: this file does NO database writes and NO queue work. It's pure-ish logic,
# easy to test. The worker handles persistence. (Separation of concerns.)
# =============================================================================
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

TRADING_DAYS_PER_YEAR = 252     # markets are closed at weekends and on holidays


# "3 months" (90 calendar days) -> about 62 trading days. Clamped to 1..252.
def calendar_to_trading_days(calendar_days: int) -> int:
    return max(1, min(TRADING_DAYS_PER_YEAR, round(calendar_days * TRADING_DAYS_PER_YEAR / 365)))


class ExtractionPipeline:
    # Everything it needs is passed in (dependency injection, done in runtime.py), so
    # tests can pass a fake extractor and a resolver with a tiny symbol list.
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

    # THE main method: post text in, verdict out.
    def run(self, text: str, posted_at: dt.datetime) -> ExtractionOutcome:
        ex = self.extractor
        # Fields every outcome carries; spread into each one with **base.
        base = dict(extractor=ex.id, prompt_version=ex.prompt_version)
        # Gate 1: call the extractor.
        # Note: only LLMBadOutput is caught here. Transient errors (timeouts, 429, 500)
        # are deliberately NOT caught: they bubble up to the worker, which tells the queue
        # "failed, retry later".
        try:
            raw = ex.extract(text, posted_at)
        except LLMBadOutput as e:
            # Deterministic failure (non-JSON, safety block): retrying won't help.
            return ExtractionOutcome("needs_review", [], [f"bad model output: {e}"], **base)
        # .pop removes the "_cached" flag from the dict (and gives us its value), so it
        # doesn't trip the schema check below.
        cached = bool(raw.pop("_cached", False))

        # Gate 2: does the answer match the expected shape?
        try:
            parsed = RawExtraction.model_validate(raw)
        except ValidationError as e:
            return ExtractionOutcome("needs_review", [], [f"schema validation failed: {e.errors()[:3]}"],
                                     raw=raw, cached=cached, **base)

        if not parsed.is_trade_idea:
            # Gate 3: contradiction ("not an idea" but it listed ideas anyway).
            if parsed.ideas:
                return ExtractionOutcome("needs_review", [], ["contradictory output: is_trade_idea=false "
                                                              "but ideas were returned"],
                                         raw=raw, cached=cached, **base)
            # Gate 4: second opinion. This is what sent your sarcastic TSLA post to Review.
            disagreement = self._second_opinion(text, posted_at)
            if disagreement is not None:
                candidates, found = disagreement
                return ExtractionOutcome(
                    "needs_review", candidates,
                    [f"LLM: no idea ({parsed.reason or 'no reason given'})",
                     f"keyword baseline disagrees, found: {found}. Sent to human review "
                     "rather than silently dropped"],
                    raw=raw, cached=cached, **base)
            # Both agree there's no idea.
            return ExtractionOutcome("no_idea", [], [parsed.reason or "no trade idea"],
                                     raw=raw, cached=cached, **base)
        # Another contradiction: "is an idea" but no ideas given.
        if not parsed.ideas:
            return ExtractionOutcome("needs_review", [], ["is_trade_idea=true but no ideas extracted"],
                                     raw=raw, cached=cached, **base)

        # Gates 5-7, applied to each idea.
        candidates, notes, flagged = self._build_candidates(parsed.ideas, posted_at)
        status = "needs_review" if flagged else "extracted"
        return ExtractionOutcome(status, candidates, notes, raw=raw, cached=cached, **base)

    # Ask the keyword baseline. Returns (its candidates, a description) if it finds an
    # idea, or None if it agrees there's none (or there's no second opinion configured).
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
        candidates, _, _ = self._build_candidates(other.ideas, posted_at)   # _ = "ignore this value"
        found = ", ".join(f"{c.ticker or c.company_name} {c.direction}" for c in candidates)
        return candidates, found

    # Turn RawIdeas into IdeaCandidates, checking each one. Returns
    # (candidates, notes explaining anything odd, flagged = "a human should look").
    def _build_candidates(self, ideas, posted_at: dt.datetime) -> tuple[list[IdeaCandidate], list[str], bool]:
        candidates: list[IdeaCandidate] = []
        notes: list[str] = []
        flagged = False
        seen: set[str] = set()
        for idea in ideas:
            # Gate 5: is the ticker real?
            ticker, how = self.resolver.resolve(idea.ticker, idea.company_name)
            if ticker is None:
                flagged = True
                notes.append(f"unresolved symbol: {how}")     # your $QZXR test ends up here
            elif ticker in seen:
                notes.append(f"duplicate mention of {ticker} merged")
                continue                                      # skip the duplicate
            else:
                seen.add(ticker)
                if how != "symbol master":
                    notes.append(f"{ticker} resolved via {how}")

            # Gate 6: horizon in trading days (and whether we had to guess it).
            horizon, inferred = self._horizon(idea.horizon_days, idea.target_date, posted_at)
            # Gate 7: is the extractor confident enough?
            if idea.confidence < self.threshold:
                flagged = True
                notes.append(f"low confidence {idea.confidence:.2f} for {ticker or idea.company_name}")
            candidates.append(IdeaCandidate(
                ticker=ticker, direction=idea.direction, horizon_days=horizon,
                horizon_inferred=inferred, target_price=idea.target_price,
                conviction=idea.conviction, confidence=idea.confidence,
                rationale=idea.rationale[:500], company_name=idea.company_name))
        return candidates, notes, flagged

    # Priority: an explicit date ("by March") beats a duration ("2 weeks"), which beats
    # the default (20 trading days, flagged as inferred so we know it was a guess).
    def _horizon(self, horizon_days: int | None, target_date: dt.date | None,
                 posted_at: dt.datetime) -> tuple[int, bool]:
        """Returns (trading days, inferred?). Explicit date beats duration beats default."""
        if target_date is not None:
            cal = (target_date - posted_at.date()).days      # calendar days until the target
            if cal > 0:                                      # ignore dates in the past
                return calendar_to_trading_days(cal), False
        if horizon_days:
            return calendar_to_trading_days(horizon_days), False
        return self.default_horizon, True

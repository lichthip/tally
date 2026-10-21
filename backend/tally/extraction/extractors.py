# =============================================================================
# extraction/extractors.py: TURN POST TEXT INTO A "RAW" ANSWER (LLM or keywords)
# -----------------------------------------------------------------------------
# Strategy pattern: IdeaExtractor is the interface ("give me text, I give you a dict").
# Two strategies implement it:
#   LLMIdeaExtractor   asks an LLM (via an LLMClient), with a database cache
#   RuleBasedExtractor keyword/regex rules. No LLM, free, deterministic. Used:
#                       (a) as the extractor when no API key is configured
#                       (b) as the KEYWORD PRE-FILTER (mentions_security) before posts
#                           are even saved, so chatter costs no LLM calls
#                       (c) as the "second opinion" when the LLM says "no idea"
#                       (d) as the baseline the eval compares the LLM against
# build_extractor() at the bottom picks one based on settings (factory).
#
# The pipeline (pipeline.py) calls extractor.extract(text, posted_at) and doesn't know
# which strategy it got.
# =============================================================================
"""Extractors: free text -> raw extraction dict (validated later by the pipeline).

Strategy pattern: the pipeline depends on the IdeaExtractor interface. Implementations:
  * LLMIdeaExtractor   - any LLMClient (Gemini, Groq, OpenRouter), with a response cache
  * RuleBasedExtractor - deterministic regex/keyword baseline. Used offline, in tests, and
                         as the baseline the eval compares the LLM against.
"""
from __future__ import annotations

import calendar          # month names ("january"...) and days-in-month
import datetime as dt
import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import LLMCacheEntry
from .llm_clients import (
    GeminiClient,
    LLMClient,
    LLMPermanentError,
    LLMRateLimited,
    OpenAICompatClient,
)
from .prompt import GEMINI_RESPONSE_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger(__name__)


# The interface. Every extractor has an id (recorded on each idea for provenance), an
# optional prompt version, a flag saying whether it uses an LLM, and extract().
class IdeaExtractor(ABC):
    id: str
    prompt_version: str | None = None
    uses_llm: bool = False

    @abstractmethod
    def extract(self, text: str, posted_at: dt.datetime) -> dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# LLM extractor + cache
# --------------------------------------------------------------------------- #
# Protocol = "duck typing" with type hints: any object with these get/put methods
# counts as a ResponseCache, no inheritance needed. Tests pass a simple dict-based fake.
class ResponseCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, model: str, prompt_version: str, response: dict[str, Any]) -> None: ...


# The real cache: the llm_cache table in Postgres.
class DbResponseCache:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.sf = session_factory

    # Look up a saved answer by key. Returns the JSON dict, or None if never seen.
    def get(self, key: str) -> dict[str, Any] | None:
        with self.sf() as s:
            return s.execute(select(LLMCacheEntry.response).where(LLMCacheEntry.key == key)).scalar()

    # Save an answer. ON CONFLICT DO NOTHING: if two workers cache the same thing at
    # once, the second insert is silently skipped instead of erroring.
    def put(self, key: str, model: str, prompt_version: str, response: dict[str, Any]) -> None:
        with self.sf.begin() as s:
            s.execute(pg_insert(LLMCacheEntry)
                      .values(key=key, model=model, prompt_version=prompt_version, response=response)
                      .on_conflict_do_nothing(index_elements=["key"]))


class LLMIdeaExtractor(IdeaExtractor):
    uses_llm = True

    def __init__(self, client: LLMClient, cache: ResponseCache | None = None,
                 max_inline_wait_s: float = 20.0):
        self.client = client                    # which provider (Gemini/Groq/OpenRouter)
        self.cache = cache
        self.id = client.model_id               # e.g. "groq:openai/gpt-oss-120b"
        self.prompt_version = PROMPT_VERSION
        self.max_inline_wait_s = max_inline_wait_s

    # Cache key = sha256 of model + prompt version + posted DATE + text. If any of these
    # change, it's a different question, so a different key (no stale answers).
    def cache_key(self, text: str, posted_at: dt.datetime) -> str:
        # The posted date is in the key because relative horizons ("by March") depend on it.
        basis = f"{self.id}|{self.prompt_version}|{posted_at.date().isoformat()}|{text}"
        return hashlib.sha256(basis.encode()).hexdigest()

    def extract(self, text: str, posted_at: dt.datetime) -> dict[str, Any]:
        key = self.cache_key(text, posted_at)
        # 1) Cache hit? Return the saved answer, no API call.
        #    `:=` (the "walrus") assigns and tests in one go: hit = cache.get(key); if hit is not None...
        if self.cache and (hit := self.cache.get(key)) is not None:
            return {**hit, "_cached": True}   # flag returned, not stored: thread-safe
        # 2) Cache miss: build the prompt and call the LLM.
        user = build_user_prompt(text, posted_at)
        try:
            result = self.client.complete_json(SYSTEM_PROMPT, user, GEMINI_RESPONSE_SCHEMA)
        except LLMRateLimited as e:
            # Short waits are cheaper to absorb inline than to bounce through the queue.
            # (If the provider says "retry in 5s", just wait 5s and try once more. If it
            # says 60s, raise, and the queue's backoff handles it.)
            if e.retry_after is not None and e.retry_after <= self.max_inline_wait_s:
                time.sleep(e.retry_after)
                result = self.client.complete_json(SYSTEM_PROMPT, user, GEMINI_RESPONSE_SCHEMA)
            else:
                raise
        # 3) Save the answer for next time.
        if self.cache:
            self.cache.put(key, self.id, self.prompt_version or "", result)
        return result


# --------------------------------------------------------------------------- #
# Rule-based baseline
# --------------------------------------------------------------------------- #
# Word lists used by the keyword rules. Sets give fast "is this word in here?" checks.
_BULL = {"buy", "buying", "bought", "long", "calls", "call", "bullish", "loading", "load",
         "accumulate", "accumulating", "adding", "added", "undervalued", "moon", "rip",
         "breakout", "upside"}
_BEAR = {"short", "shorting", "shorted", "puts", "put", "bearish", "overvalued", "dump",
         "crash", "collapse", "downside", "fade", "fading", "tank"}
# Patterns meaning "this is NOT the author's own call": a question mark at the end,
# "should I", "anyone", "thoughts on", analyst upgrades, "X says buy"...
_NOT_IDEA_PATTERNS = [r"\?\s*$", r"\bshould i\b", r"\bwould you\b", r"\bif it\b", r"\bif .* dips?\b",
                      r"\bwhat do you think\b", r"\banyone\b", r"\bthoughts on\b", r"\bupgraded?\b",
                      r"\bdowngraded?\b", r"\bsays? (to )?buy\b"]
# Capitalised words that happen to be tickers but usually aren't meant as tickers
# ("I", "AI", "CEO", "IT", "ON"...), so they're ignored.
_STOPWORDS = {"I", "A", "AI", "CEO", "CFO", "USA", "US", "ETF", "IPO", "DD", "IMO", "ATH", "EPS",
              "GDP", "FOMO", "YOLO", "LOL", "EOY", "PT", "IT", "ON", "ALL", "FOR", "BE", "SO",
              "GO", "OR", "AT", "NOW", "ARE", "CAN", "OUT", "BIG", "FED", "CPI", "TA", "OK"}
# Aliases that are ordinary words or generic market terms. Matching them by keyword gives
# false positives ("Robinhood Gold" -> GLD), so the baseline ignores them; the LLM, which
# reads context, still resolves them through the symbol master.
_AMBIGUOUS_ALIASES = {"gold", "silver", "oil", "crude", "s&p", "s&p 500", "spx", "the market",
                      "nasdaq", "nasdaq 100", "qqq", "the dow", "dow jones", "semis",
                      "semiconductors", "small caps", "russell 2000", "treasuries", "long bonds",
                      "20 year treasuries", "block", "square", "strategy", "sti",
                      "straits times index", "hang seng", "hsi", "target corp"}
# {"january": 1, ..., "jan": 1, ...} for reading "by March".
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


class RuleBasedExtractor(IdeaExtractor):
    """Deliberately simple baseline. Its failure modes (sarcasm, puts vs calls nuance,
    hypotheticals) are exactly what the eval set measures the LLM against."""

    id = "rules:v1"
    prompt_version = None

    # symbols: [(ticker, [aliases...])], from SymbolResolver.symbol_list().
    def __init__(self, symbols: Iterable[tuple[str, list[str]]]):
        self.tickers: set[str] = set()
        self.alias_to_ticker: dict[str, str] = {}
        for ticker, aliases in symbols:
            self.tickers.add(ticker.upper())
            for a in aliases:
                if a.lower() not in _AMBIGUOUS_ALIASES:      # skip "gold", "oil", "block"...
                    self.alias_to_ticker[a.lower()] = ticker.upper()
        # Longest aliases first so "bank of america" wins over "america".
        # Builds ONE big regex: \b(bank of america|nvidia|apple|...)\b. re.escape makes
        # characters like "&" or "." literal. \b = word boundary ("apple" not "pineapple").
        self._alias_re = re.compile(
            r"\b(" + "|".join(re.escape(a) for a in sorted(self.alias_to_ticker, key=len, reverse=True))
            + r")\b") if self.alias_to_ticker else None

    # The keyword "extraction". Steps:
    #   1. looks like a question/hypothetical/hearsay? -> not an idea
    #   2. find ticker mentions; none -> not an idea
    #   3. for each ticker, find the nearest direction word (falling back to the whole
    #      post's mood); no direction -> skip that ticker
    #   4. pick up a horizon ("next 3 months") and target price if present
    def extract(self, text: str, posted_at: dt.datetime) -> dict[str, Any]:
        lower = text.lower()
        if any(re.search(p, lower) for p in _NOT_IDEA_PATTERNS):
            return {"is_trade_idea": False, "reason": "question/hypothetical/hearsay pattern", "ideas": []}

        mentions = self._find_mentions(text)
        if not mentions:
            return {"is_trade_idea": False, "reason": "no known security mentioned", "ideas": []}

        global_dir = self._direction(lower)                    # mood of the whole post
        horizon_days, target_date = self._horizon(lower, posted_at.date())
        target_price = self._target_price(lower)
        ideas, seen = [], set()
        for ticker, pos in mentions:
            if ticker in seen:            # the same ticker mentioned twice -> one idea
                continue
            seen.add(ticker)
            # `or`: use the local direction if found, otherwise the post's overall mood.
            local = self._nearest_direction(lower, pos) or global_dir
            if local is None:
                continue
            ideas.append({
                "ticker": ticker, "company_name": None, "direction": local,
                "horizon_days": horizon_days, "target_date": target_date,
                # A target price only makes sense if exactly one ticker was mentioned.
                "target_price": target_price if len(mentions) == 1 else None,
                # A $cashtag is a stronger signal than a bare word, so slightly higher confidence.
                "conviction": "medium", "confidence": 0.75 if "$" in text else 0.65,
                "rationale": "keyword match",
            })
        if not ideas:
            return {"is_trade_idea": False, "reason": "no directional language", "ideas": []}
        return {"is_trade_idea": True, "reason": "keyword match", "ideas": ideas}

    # THE KEYWORD PRE-FILTER: "does this post mention any known stock at all?"
    # runtime.poll() calls this on every fetched post; False -> dropped, no LLM call.
    def mentions_security(self, text: str) -> bool:
        return bool(self._find_mentions(text))

    # Find every ticker mention and WHERE it is in the text: [(ticker, position), ...].
    # Three ways to spot one:
    def _find_mentions(self, text: str) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        # 1) Cashtags: $NVDA, $brk.b -> "BRK-B". Any cashtag counts, even unknown ones.
        for m in re.finditer(r"\$([A-Za-z]{1,5}(?:[.-][A-Za-z]{1,2})?)\b", text):
            found.append((m.group(1).upper().replace(".", "-"), m.start()))
        # 2) Bare capitalised words (2-5 letters) that are known tickers and not stopwords.
        for m in re.finditer(r"\b([A-Z]{2,5})\b", text):
            tok = m.group(1)
            if tok in self.tickers and tok not in _STOPWORDS:
                found.append((tok, m.start()))
        # 3) Company names / aliases ("nvidia", "dbs").
        if self._alias_re:
            for m in self._alias_re.finditer(text.lower()):
                found.append((self.alias_to_ticker[m.group(1)], m.start()))
        found.sort(key=lambda x: x[1])      # order by position in the text
        return found

    # Mood of a text fragment: count bullish words vs bearish words.
    # @staticmethod = doesn't need `self`; it's just a function that lives in the class.
    @staticmethod
    def _direction(fragment: str) -> str | None:
        words = re.findall(r"[a-z]+", fragment)
        bull = sum(w in _BULL for w in words)      # True counts as 1, False as 0
        bear = sum(w in _BEAR for w in words)
        # "selling puts" is bullish; "selling calls" is bearish-ish. Undo the naive count.
        if re.search(r"\bsell(ing)? puts?\b", fragment):
            bull, bear = bull + 2, max(0, bear - 1)
        if bull > bear:
            return "LONG"
        if bear > bull:
            return "SHORT"
        return None                  # a tie: can't tell

    # Direction for ONE ticker: look at the 5 words before it and the 5 after it, and use
    # the CLOSEST direction word. That's how "long AMD, short INTC" gives AMD LONG and INTC
    # SHORT, instead of one mood for the whole post (an earlier bug fixed with real data).
    @staticmethod
    def _nearest_direction(lower: str, pos: int, window: int = 5) -> str | None:
        """Direction word closest to a ticker mention, looking up to `window` words either
        side. Ties go to the word BEFORE the ticker ('long AMD, short INTC' gives AMD LONG,
        INTC SHORT), and a word after it still counts ('HOOD seems overvalued' gives SHORT;
        'TSLA puts' gives SHORT). 'selling puts' is bullish, 'selling calls' bearish."""
        before = re.findall(r"[a-z]+", lower[max(0, pos - 80):pos])[-window:]
        after = re.findall(r"[a-z]+", lower[pos:pos + 80])[1:window + 1]   # [0] is the ticker

        # Inner helper: the verdict for a single word (with the word before it, for options).
        def verdict(word: str, prev: str | None) -> str | None:
            if word in ("puts", "put", "calls", "call"):
                # Buying calls = LONG, buying puts = SHORT; "sell" in front flips it.
                # (`!=` on two booleans works as "exactly one of them is true".)
                bullish = word.startswith("call") != bool(prev and prev.startswith("sell"))
                return "LONG" if bullish else "SHORT"
            if word in _BULL:
                return "LONG"
            if word in _BEAR:
                return "SHORT"
            return None

        # Options right after the ticker define the position: "buying TSLA puts" is SHORT
        # even though "buying" is closer. "selling (TSLA) puts" flips it.
        for i, w in enumerate(after[:2]):
            if w in ("puts", "put", "calls", "call"):
                prev = after[i - 1] if i > 0 else (before[-1] if before else None)
                return verdict(w, prev)

        # Otherwise: walk outwards from the ticker on both sides and keep the closest hit.
        best: tuple[int, int, str] | None = None   # (distance, side 0=before 1=after, verdict)
        for i, w in enumerate(reversed(before)):   # nearest word before the ticker first
            prev = before[-i - 2] if len(before) - i - 2 >= 0 else None
            v = verdict(w, prev)
            if v:
                best = (i + 1, 0, v)
                break
        for i, w in enumerate(after):
            v = verdict(w, after[i - 1] if i > 0 else None)
            if v:
                # Tuples compare item by item: a smaller distance wins; on equal distance,
                # side 0 (before) beats side 1 (after).
                if best is None or (i + 1, 1) < best[:2]:
                    best = (i + 1, 1, v)
                break
        return best[2] if best else None

    # Find a time horizon. Returns (calendar days, target date): at most one is set.
    @staticmethod
    def _horizon(lower: str, posted: dt.date) -> tuple[int | None, str | None]:
        # Only forward-looking durations count: "for the next 6 months", "in 2 weeks".
        # A bare "3 years" is often backward-looking ("CAGR over the last 3 years").
        m = re.search(r"\b(?:next|in|within|for|hold(?:ing)? for)\s+(?:the\s+next\s+)?(\d+)\s*"
                      r"(day|week|month|year)s?\b", lower)
        if m:
            mult = {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
            return int(m.group(1)) * mult, None
        # "end of year" / "EOY" -> 31 December of the posting year.
        if re.search(r"\b(end of (the )?year|eoy)\b", lower):
            return None, dt.date(posted.year, 12, 31).isoformat()
        # "by March" -> the last day of the next March (this year, or next year if March
        # has already passed).
        m = re.search(r"\bby (" + "|".join(_MONTHS) + r")\b", lower)
        if m:
            month = _MONTHS[m.group(1)]
            year = posted.year + (1 if month <= posted.month else 0)
            last = calendar.monthrange(year, month)[1]       # number of days in that month
            return None, dt.date(year, month, last).isoformat()
        return None, None

    # A price target: "to 200", "target $150", "PT 90". The (?!...) part ("negative
    # lookahead") rejects "to 20%" or "in 3 days", which are not prices.
    @staticmethod
    def _target_price(lower: str) -> float | None:
        m = re.search(r"\b(?:to|target|pt|at)\s*\$?(\d{1,6}(?:\.\d+)?)\b(?!\s*(?:%|day|week|month|year))", lower)
        return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
# Build the configured extractor. Called once, by runtime.py. Missing API key ->
# a clear LLMPermanentError at startup, instead of mysterious failures later.
def build_extractor(settings: Settings, session_factory: sessionmaker[Session] | None,
                    symbols: Iterable[tuple[str, list[str]]] = ()) -> IdeaExtractor:
    cache = DbResponseCache(session_factory) if session_factory else None
    p = settings.llm_provider
    t = settings.llm_timeout_seconds
    if p == "gemini":
        if not settings.gemini_api_key:
            raise LLMPermanentError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        return LLMIdeaExtractor(GeminiClient(settings.gemini_api_key, settings.gemini_model, t), cache)
    if p == "groq":
        if not settings.groq_api_key:
            raise LLMPermanentError("LLM_PROVIDER=groq but GROQ_API_KEY is not set")
        # Groq speaks the OpenAI format, so it's the OpenAI-compatible client with Groq's URL.
        return LLMIdeaExtractor(OpenAICompatClient(
            "groq", "https://api.groq.com/openai/v1", settings.groq_api_key, settings.groq_model, t), cache)
    if p == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMPermanentError("LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set")
        return LLMIdeaExtractor(OpenAICompatClient(
            "openrouter", "https://openrouter.ai/api/v1", settings.openrouter_api_key,
            settings.openrouter_model, t, extra_headers={"X-Title": "Tally"}), cache)
    # LLM_PROVIDER=rules (the default): no LLM at all.
    return RuleBasedExtractor(symbols)

# =============================================================================
# extraction/symbols.py: "IS THIS A REAL TICKER?" (the LLM proposes, our code decides)
# -----------------------------------------------------------------------------
# The LLM might return "Apple", "AAPL", "$aapl", "NASDAQ: AAPL", or a ticker it made up.
# SymbolResolver turns that into one confirmed ticker, or None (-> human review).
#
# It keeps the symbol master in memory (a set of tickers + a dict alias -> ticker) so
# lookups are instant, and only touches the database when it learns a new ticker.
#
# Used by: ExtractionPipeline._build_candidates (pipeline.py). Built in runtime.py.
# =============================================================================
"""Symbol resolution: the LLM proposes a ticker, our code decides whether to accept it.

Order of checks:
  1. exact ticker in the symbol master
  2. company name matches a known alias ("Apple" -> AAPL, "DBS" -> D05.SI)
  3. ticker not known locally, but the market data provider has prices for it:
     accept and add it to the symbol master (self-extending, still validated)
  4. otherwise unresolved -> the post goes to human review
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..market.exchanges import exchange_code
from ..models import Symbol

log = logging.getLogger(__name__)
# Exchange suffixes where the dot is part of the ticker (D05.SI, 0700.HK), so it must NOT
# be turned into a dash.
_EXCHANGE_SUFFIX = re.compile(r"\.(SI|HK|L)$")


# Clean up a raw ticker string into Yahoo's format:
#   " $brk.b "     -> "BRK-B"
#   "NASDAQ: AAPL" -> "AAPL"
#   "d05.si"       -> "D05.SI"  (suffix kept)
def normalise_ticker(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip().upper().lstrip("$")
    t = re.sub(r"^(NASDAQ|NYSE|SGX|HKEX|AMEX)\s*:\s*", "", t)   # drop an exchange prefix
    if not _EXCHANGE_SUFFIX.search(t):
        t = t.replace(".", "-")      # BRK.B -> BRK-B (Yahoo format)
    return t or None


class SymbolResolver:
    # exists_fn: a function "does this ticker have price history?" (PriceStore.symbol_exists).
    # Passing a FUNCTION in (instead of importing Yahoo code here) keeps this class easy
    # to test: tests pass a fake function.
    def __init__(self, session_factory: sessionmaker[Session] | None,
                 exists_fn: Callable[[str], bool] | None = None,
                 symbols: list[tuple[str, str, list[str]]] | None = None):
        """`symbols` (ticker, name, aliases) can be injected directly for tests."""
        self.sf = session_factory
        self.exists_fn = exists_fn
        self._lock = threading.Lock()           # several worker threads share one resolver
        self.tickers: set[str] = set()          # {"AAPL", "NVDA", ...}: a set gives O(1) membership checks
        self.aliases: dict[str, str] = {}       # {"apple": "AAPL", "nvidia": "NVDA", ...}
        if symbols is not None:
            self._load(symbols)
        else:
            self.refresh()

    # Build the in-memory set and dict from a list of (ticker, name, aliases).
    def _load(self, symbols: list[tuple[str, str, list[str]]]) -> None:
        tickers, aliases = set(), {}
        for ticker, name, al in symbols:
            tickers.add(ticker)
            aliases[name.lower()] = ticker      # the full company name counts as an alias too
            for a in al:
                aliases[a.lower()] = ticker
        # Swap in the new data under the lock, so another thread never sees half of it.
        with self._lock:
            self.tickers, self.aliases = tickers, aliases

    # Reload the symbol master from the database.
    def refresh(self) -> None:
        if self.sf is None:
            return
        with self.sf() as s:
            rows = s.execute(select(Symbol.ticker, Symbol.name, Symbol.aliases)).all()
        self._load([(r.ticker, r.name, list(r.aliases or [])) for r in rows])

    # The reverse view, [(ticker, [aliases...])], which is what RuleBasedExtractor wants.
    def symbol_list(self) -> list[tuple[str, list[str]]]:
        inv: dict[str, list[str]] = {}
        for alias, t in self.aliases.items():
            inv.setdefault(t, []).append(alias)   # setdefault: create the list the first time
        return [(t, inv.get(t, [])) for t in self.tickers]

    # THE main method. Returns (ticker or None, a note saying HOW it was resolved).
    # The note ends up on the Review tab, e.g. "unknown ticker 'QZXR' / company 'Quazar'".
    def resolve(self, ticker: str | None, company_name: str | None) -> tuple[str | None, str]:
        t = normalise_ticker(ticker)
        # Check 1: is it a ticker we already know?
        if t and t in self.tickers:
            return t, "symbol master"
        # Check 2: does the company name match an alias? Try as-is, then without a
        # suffix like "Inc", "Corp" or "Holdings" ("Apple Inc." -> "apple").
        if company_name:
            key = company_name.strip().lower()
            for candidate in (key, re.sub(r"\b(inc|corp|corporation|ltd|limited|plc|holdings?)\.?$", "", key).strip()):
                if candidate in self.aliases:
                    return self.aliases[candidate], f"alias '{company_name}'"
        # Check 3: unknown to us, but Yahoo has price history for it -> it's real. Learn it.
        if t and self.exists_fn is not None and self.exists_fn(t):
            self._add(t, company_name or t)
            return t, "added after price-history check"
        # Check 4: give up; the pipeline flags the post for human review.
        return None, f"unknown ticker {ticker!r} / company {company_name!r}"

    # Remember a newly confirmed ticker in memory AND in the database.
    def _add(self, ticker: str, name: str) -> None:
        with self._lock:
            self.tickers.add(ticker)
        if self.sf is None:
            return
        with self.sf.begin() as s:
            s.execute(pg_insert(Symbol).values(ticker=ticker, name=name[:256],
                                               exchange=exchange_code(ticker), aliases=[])
                      .on_conflict_do_nothing(index_elements=["ticker"]))   # already there? fine
        log.info("added %s to symbol master", ticker)

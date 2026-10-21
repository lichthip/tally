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
_EXCHANGE_SUFFIX = re.compile(r"\.(SI|HK|L)$")


def normalise_ticker(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip().upper().lstrip("$")
    t = re.sub(r"^(NASDAQ|NYSE|SGX|HKEX|AMEX)\s*:\s*", "", t)
    if not _EXCHANGE_SUFFIX.search(t):
        t = t.replace(".", "-")      # BRK.B -> BRK-B (Yahoo format)
    return t or None


class SymbolResolver:
    def __init__(self, session_factory: sessionmaker[Session] | None,
                 exists_fn: Callable[[str], bool] | None = None,
                 symbols: list[tuple[str, str, list[str]]] | None = None):
        """`symbols` (ticker, name, aliases) can be injected directly for tests."""
        self.sf = session_factory
        self.exists_fn = exists_fn
        self._lock = threading.Lock()
        self.tickers: set[str] = set()
        self.aliases: dict[str, str] = {}
        if symbols is not None:
            self._load(symbols)
        else:
            self.refresh()

    def _load(self, symbols: list[tuple[str, str, list[str]]]) -> None:
        tickers, aliases = set(), {}
        for ticker, name, al in symbols:
            tickers.add(ticker)
            aliases[name.lower()] = ticker
            for a in al:
                aliases[a.lower()] = ticker
        with self._lock:
            self.tickers, self.aliases = tickers, aliases

    def refresh(self) -> None:
        if self.sf is None:
            return
        with self.sf() as s:
            rows = s.execute(select(Symbol.ticker, Symbol.name, Symbol.aliases)).all()
        self._load([(r.ticker, r.name, list(r.aliases or [])) for r in rows])

    def symbol_list(self) -> list[tuple[str, list[str]]]:
        inv: dict[str, list[str]] = {}
        for alias, t in self.aliases.items():
            inv.setdefault(t, []).append(alias)
        return [(t, inv.get(t, [])) for t in self.tickers]

    def resolve(self, ticker: str | None, company_name: str | None) -> tuple[str | None, str]:
        t = normalise_ticker(ticker)
        if t and t in self.tickers:
            return t, "symbol master"
        if company_name:
            key = company_name.strip().lower()
            for candidate in (key, re.sub(r"\b(inc|corp|corporation|ltd|limited|plc|holdings?)\.?$", "", key).strip()):
                if candidate in self.aliases:
                    return self.aliases[candidate], f"alias '{company_name}'"
        if t and self.exists_fn is not None and self.exists_fn(t):
            self._add(t, company_name or t)
            return t, "added after price-history check"
        return None, f"unknown ticker {ticker!r} / company {company_name!r}"

    def _add(self, ticker: str, name: str) -> None:
        with self._lock:
            self.tickers.add(ticker)
        if self.sf is None:
            return
        with self.sf.begin() as s:
            s.execute(pg_insert(Symbol).values(ticker=ticker, name=name[:256],
                                               exchange=exchange_code(ticker), aliases=[])
                      .on_conflict_do_nothing(index_elements=["ticker"]))
        log.info("added %s to symbol master", ticker)

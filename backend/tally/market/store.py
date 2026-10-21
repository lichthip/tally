"""Read-through price cache in Postgres.

Scoring touches the same tickers every run; refetching from Yahoo each time would be slow
and get us rate-limited. We remember which date range we hold per ticker
(price_coverage) and only go to the provider when the request falls outside it or the
data is stale.

Subtle point: adjusted prices are rewritten retroactively after every dividend/split,
so a refresh re-downloads the WHOLE covered range, not just the new days. Mixing old
adjusted closes with new ones would corrupt returns.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..models import Price, PriceCoverage
from .providers import Bar, MarketDataProvider

log = logging.getLogger(__name__)


class PriceStore:
    def __init__(self, session_factory: sessionmaker[Session], provider: MarketDataProvider,
                 refresh_after: dt.timedelta = dt.timedelta(hours=6)):
        self.sf = session_factory
        self.provider = provider
        self.refresh_after = refresh_after
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, ticker: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(ticker, threading.Lock())

    def get_bars(self, ticker: str, start: dt.date, end: dt.date | None = None) -> list[Bar]:
        end = end or _today()
        with self._lock(ticker):   # avoid two threads fetching the same ticker at once
            self._ensure_cached(ticker, start, end)
        with self.sf() as s:
            rows = s.execute(select(Price.date, Price.open, Price.close)
                             .where(Price.ticker == ticker, Price.date >= start, Price.date <= end)
                             .order_by(Price.date)).all()
        return [Bar(r.date, r.open, r.close) for r in rows]

    def _ensure_cached(self, ticker: str, start: dt.date, end: dt.date) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        with self.sf() as s:
            cov = s.get(PriceCoverage, ticker)
        if cov is not None and cov.covered_from <= start:
            if cov.covered_to >= end or now - cov.fetched_at < self.refresh_after:
                return  # cache hit
        fetch_from = min(start, cov.covered_from) if cov else start
        fetch_to = _today()
        bars = self.provider.get_daily_bars(ticker, fetch_from, fetch_to)
        if not bars:
            log.info("no price data for %s (%s..%s)", ticker, fetch_from, fetch_to)
            return  # don't record coverage: a transient failure shouldn't be cached
        with self.sf.begin() as s:
            rows = [{"ticker": ticker, "date": b.date, "open": b.open, "close": b.close} for b in bars]
            for i in range(0, len(rows), 1000):
                stmt = pg_insert(Price).values(rows[i:i + 1000])
                s.execute(stmt.on_conflict_do_update(
                    index_elements=["ticker", "date"],
                    set_={"open": stmt.excluded.open, "close": stmt.excluded.close}))
            cov_stmt = pg_insert(PriceCoverage).values(
                ticker=ticker, covered_from=fetch_from, covered_to=fetch_to, fetched_at=now)
            s.execute(cov_stmt.on_conflict_do_update(
                index_elements=["ticker"],
                set_={"covered_from": fetch_from, "covered_to": fetch_to, "fetched_at": now}))

    def symbol_exists(self, ticker: str) -> bool:
        return self.provider.symbol_exists(ticker)


def _today() -> dt.date:
    # UTC date + 1 so that Asian sessions that have already started "tomorrow" are included.
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).date()

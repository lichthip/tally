# =============================================================================
# market/providers.py: WHERE PRICES COME FROM
# -----------------------------------------------------------------------------
# Strategy pattern again: MarketDataProvider is the interface ("give me daily bars for
# ticker X between two dates"). Two implementations:
#   YFinanceProvider   real prices from Yahoo Finance (free, unofficial)
#   SyntheticProvider  fake but deterministic prices, for tests / offline work
# Chosen by MARKET_DATA_PROVIDER in .env (build_provider at the bottom).
#
# A "bar" = one trading day: the date, the opening price, and the closing price.
# All prices are ADJUSTED for splits and dividends (explained in the docstring below).
#
# Nobody calls a provider directly except PriceStore (store.py), which caches results.
# =============================================================================
"""Market data providers (daily bars). Swappable behind one interface.

All prices are split- and dividend-ADJUSTED. Without adjustment a 10-for-1 split looks
like a 90% crash and every long idea on that stock would be scored as a disaster.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


# One day of prices. frozen = immutable (a bar never changes once created).
@dataclass(frozen=True)
class Bar:
    date: dt.date
    open: float
    close: float


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def get_daily_bars(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        """Bars with start <= date <= end, ascending. Empty list if unknown/no data."""

    # "Is this a real ticker?" = did it trade at all in the last two weeks?
    # Used by SymbolResolver for tickers not in our symbol master.
    def symbol_exists(self, ticker: str) -> bool:
        today = dt.date.today()
        return bool(self.get_daily_bars(ticker, today - dt.timedelta(days=14), today))


class YFinanceProvider(MarketDataProvider):
    """Free Yahoo Finance data via the yfinance library. Fine for a personal project;
    unofficial, rate-limited, and not something to run a fund on."""

    name = "yfinance"

    def get_daily_bars(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        import yfinance as yf  # imported lazily: heavy, and unused in tests

        try:
            # yfinance's `end` is exclusive, hence +1 day.
            # auto_adjust=True -> adjusted prices; actions=False -> skip dividend/split columns.
            df = yf.Ticker(ticker).history(start=start.isoformat(),
                                           end=(end + dt.timedelta(days=1)).isoformat(),
                                           auto_adjust=True, actions=False)
        except Exception as e:  # noqa: BLE001 - network/parsing errors from an unofficial API
            log.warning("yfinance failed for %s: %s", ticker, e)
            return []                         # "no data" rather than crashing the scoring run
        bars = []
        # df is a pandas DataFrame (a table); iterrows() gives (index=timestamp, row) pairs.
        for ts, row in df.iterrows():
            o, c = float(row["Open"]), float(row["Close"])
            # Skip broken rows: NaN ("not a number" = missing) or non-positive prices.
            if math.isnan(o) or math.isnan(c) or o <= 0 or c <= 0:
                continue
            bars.append(Bar(ts.date(), o, c))
        return bars


class SyntheticProvider(MarketDataProvider):
    """Deterministic fake prices (geometric Brownian motion, seeded by ticker).

    Used for tests and offline demos where Yahoo isn't reachable. Same ticker -> same
    series every run, so tests are reproducible.
    """

    name = "synthetic"
    EPOCH = dt.date(2020, 1, 1)                       # fake history starts here
    _VALID = re.compile(r"^[A-Z0-9]{1,6}([.-][A-Z]{1,2})?$")   # what a plausible ticker looks like

    # delisted: optionally make some tickers "stop trading" on a date (to test unscorable).
    def __init__(self, delisted: dict[str, dt.date] | None = None):
        self.delisted = delisted or {}
        self._cache: dict[str, list[Bar]] = {}

    # Generate the whole fake price history for one ticker (and remember it).
    def _series(self, ticker: str) -> list[Bar]:
        if ticker in self._cache:
            return self._cache[ticker]
        # Seed the random generator from the ticker's hash: same ticker -> same "random"
        # numbers -> same prices every run (reproducible tests).
        seed = int(hashlib.md5(ticker.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        is_bench = ticker in {"SPY", "ES3.SI", "2800.HK", "ISF.L"}
        mu = 0.08 if is_bench else rng.uniform(-0.05, 0.25)       # annual drift
        sigma = 0.16 if is_bench else rng.uniform(0.25, 0.6)      # annual vol
        price = rng.uniform(20, 400)
        bars: list[Bar] = []
        d = self.EPOCH
        end = dt.date.today() + dt.timedelta(days=1)
        stop = self.delisted.get(ticker)
        dt_ = 1 / 252                                 # one trading day, as a fraction of a year
        while d <= end:
            if d.weekday() < 5 and (stop is None or d <= stop):   # weekdays only (0=Mon..4=Fri)
                # Geometric Brownian motion (the standard textbook stock-price model): each
                # day the price moves by a random % drawn from a normal distribution. Plus a
                # small overnight "gap" so open != previous close.
                gap = rng.gauss(0, sigma * math.sqrt(dt_) * 0.3)
                o = price * math.exp(gap)
                ret = (mu - 0.5 * sigma ** 2) * dt_ + sigma * math.sqrt(dt_) * rng.gauss(0, 1)
                c = o * math.exp(ret)
                bars.append(Bar(d, round(o, 4), round(c, 4)))
                price = c
            d += dt.timedelta(days=1)
        self._cache[ticker] = bars
        return bars

    def get_daily_bars(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        if not self._VALID.match(ticker):
            return []
        return [b for b in self._series(ticker) if start <= b.date <= end]

    def symbol_exists(self, ticker: str) -> bool:
        return bool(self._VALID.match(ticker))


# Factory: setting value -> provider object. Called once in runtime.py.
def build_provider(name: str) -> MarketDataProvider:
    if name == "synthetic":
        return SyntheticProvider()
    return YFinanceProvider()

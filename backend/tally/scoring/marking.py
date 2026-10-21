"""Mark-to-market for a single idea. Pure functions: no DB, no network, easy to test.

Rules (each one prevents a specific way of fooling ourselves):
  * ENTRY = the first OPEN price a reader could actually have traded at after we received
    the idea, in the stock's own exchange timezone. Using the same day's close (which may
    already have happened) would be lookahead bias.
  * EXIT  = the close of the horizon's last trading day; until then the idea is OPEN and
    marked at the latest close. Only fully completed sessions can close an idea.
  * EXCESS RETURN = direction * (stock return - benchmark return) over the same dates.
    In a bull market every long looks smart; only beating the index is skill.
    A SHORT is scored as short-stock / long-benchmark.
  * NORMALISED excess = excess * sqrt(20 / horizon). Volatility grows with sqrt(time), so
    a 6-month call and a 3-day call aren't comparable raw. This puts them on one scale.
  * Missing data is never silently dropped (that inflates scores: survivorship bias).
    An idea whose price history stops is marked UNSCORABLE with a reason.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from dataclasses import dataclass

from ..market.exchanges import Exchange
from ..market.providers import Bar


@dataclass(frozen=True)
class IdeaSpec:
    ticker: str
    direction: str          # LONG / SHORT
    horizon_days: int       # trading days
    effective_at: dt.datetime


@dataclass
class MarkResult:
    status: str             # pending_entry | open | closed | unscorable
    entry_date: dt.date | None = None
    entry_price: float | None = None
    bench_ticker: str | None = None
    bench_entry_price: float | None = None
    mark_date: dt.date | None = None
    mark_price: float | None = None
    bench_mark_price: float | None = None
    raw_return: float | None = None
    bench_return: float | None = None
    excess_return: float | None = None
    normalised_excess: float | None = None
    unscorable_reason: str | None = None


def find_entry_index(bars: list[Bar], effective_at: dt.datetime, ex: Exchange) -> int | None:
    """Index of the first bar whose OPEN happens strictly after `effective_at`."""
    local = effective_at.astimezone(ex.tz)
    for i, b in enumerate(bars):
        if b.date > local.date():
            return i
        if b.date == local.date() and local.time() < ex.open_time:
            return i
    return None


def _bar_on_or_before(bars: list[Bar], d: dt.date) -> Bar | None:
    dates = [b.date for b in bars]
    i = bisect.bisect_right(dates, d) - 1
    return bars[i] if i >= 0 else None


def _bar_on_or_after(bars: list[Bar], d: dt.date) -> Bar | None:
    dates = [b.date for b in bars]
    i = bisect.bisect_left(dates, d)
    return bars[i] if i < len(bars) else None


def mark_idea(idea: IdeaSpec, bars: list[Bar], bench_bars: list[Bar], bench_ticker: str,
              ex: Exchange, now: dt.datetime, normalisation_horizon: int = 20,
              stale_after_days: int = 10) -> MarkResult:
    if not bars:
        return MarkResult("unscorable", bench_ticker=bench_ticker,
                          unscorable_reason=f"no price data for {idea.ticker}")
    if not bench_bars:
        return MarkResult("unscorable", bench_ticker=bench_ticker,
                          unscorable_reason=f"no benchmark data for {bench_ticker}")

    today_local = now.astimezone(ex.tz).date()
    last_bar_age = (today_local - bars[-1].date).days
    i = find_entry_index(bars, idea.effective_at, ex)
    if i is None:
        if last_bar_age > stale_after_days and idea.effective_at.astimezone(ex.tz).date() < today_local:
            return MarkResult("unscorable", bench_ticker=bench_ticker,
                              unscorable_reason=f"price history ends {bars[-1].date} (delisted or halted?)")
        return MarkResult("pending_entry", bench_ticker=bench_ticker)

    entry = bars[i]
    bench_entry = _bar_on_or_after(bench_bars, entry.date)
    if bench_entry is None:
        return MarkResult("pending_entry", bench_ticker=bench_ticker, entry_date=entry.date,
                          entry_price=entry.open)

    # A bar dated today may be an in-progress session; only completed bars can close an idea.
    complete = [b for b in bars if b.date < today_local]
    exit_idx = i + idea.horizon_days - 1
    if exit_idx < len(complete):
        status, mark = "closed", complete[exit_idx]
    else:
        status, mark = "open", bars[-1]
        if last_bar_age > stale_after_days:
            status = "unscorable"
    bench_mark = _bar_on_or_before(bench_bars, mark.date) or bench_entry

    sign = 1.0 if idea.direction == "LONG" else -1.0
    r = mark.close / entry.open - 1.0
    rb = bench_mark.close / bench_entry.open - 1.0
    excess = sign * (r - rb)
    norm = excess * math.sqrt(normalisation_horizon / idea.horizon_days)
    return MarkResult(
        status=status, entry_date=entry.date, entry_price=entry.open, bench_ticker=bench_ticker,
        bench_entry_price=bench_entry.open, mark_date=mark.date, mark_price=mark.close,
        bench_mark_price=bench_mark.close, raw_return=r, bench_return=rb, excess_return=excess,
        normalised_excess=norm,
        unscorable_reason=(f"price history ends {bars[-1].date} (delisted or halted?)"
                           if status == "unscorable" else None),
    )

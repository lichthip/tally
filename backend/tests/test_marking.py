"""Point-in-time correctness of mark-to-market. Pure functions, no DB."""
import datetime as dt

import pytest

from tally.market.exchanges import EXCHANGES, exchange_for
from tally.market.providers import Bar
from tally.scoring.marking import IdeaSpec, find_entry_index, mark_idea

UTC = dt.timezone.utc
US, SI = EXCHANGES["US"], EXCHANGES["SI"]


def weekday_bars(start: dt.date, n: int, open_=100.0, step=1.0) -> list[Bar]:
    """n weekday bars; open = close of previous day; close rises by `step` per day."""
    bars, d, price = [], start, open_
    while len(bars) < n:
        if d.weekday() < 5:
            bars.append(Bar(d, price, price + step))
            price += step
        d += dt.timedelta(days=1)
    return bars


def at(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=UTC)


class TestEntryRule:
    bars = weekday_bars(dt.date(2026, 10, 5), 10)   # Mon 5 Oct 2026 onwards

    def test_before_us_open_enters_same_day(self):
        # 13:00 UTC = 09:00 New York (EDT): before the 09:30 open
        i = find_entry_index(self.bars, at(2026, 10, 7, 13, 0), US)
        assert self.bars[i].date == dt.date(2026, 10, 7)

    def test_after_us_open_enters_next_day(self):
        # 14:00 UTC = 10:00 New York: today's open already happened -> tomorrow's open
        i = find_entry_index(self.bars, at(2026, 10, 7, 14, 0), US)
        assert self.bars[i].date == dt.date(2026, 10, 8)

    def test_same_instant_differs_by_exchange(self):
        # 13:14 UTC Wed = 21:14 Singapore (after SGX open) = 09:14 New York (before NYSE open)
        t = at(2026, 10, 7, 13, 14)
        assert self.bars[find_entry_index(self.bars, t, US)].date == dt.date(2026, 10, 7)
        assert self.bars[find_entry_index(self.bars, t, SI)].date == dt.date(2026, 10, 8)

    def test_weekend_rolls_to_monday(self):
        i = find_entry_index(self.bars, at(2026, 10, 10, 15), US)   # Saturday
        assert self.bars[i].date == dt.date(2026, 10, 12)

    def test_no_future_bar_returns_none(self):
        assert find_entry_index(self.bars, at(2026, 12, 1, 12), US) is None


def test_exchange_lookup():
    assert exchange_for("D05.SI").code == "SI"
    assert exchange_for("0700.HK").benchmark == "2800.HK"
    assert exchange_for("BRK-B").code == "US"


class TestMarkIdea:
    start = dt.date(2026, 1, 5)
    now = at(2026, 3, 1, 12)

    def test_long_beats_benchmark(self):
        stock = weekday_bars(self.start, 40, open_=100, step=1.0)
        bench = weekday_bars(self.start, 40, open_=100, step=0.2)
        idea = IdeaSpec("AAPL", "LONG", 5, at(2026, 1, 5, 12))  # before open -> enters 5 Jan
        m = mark_idea(idea, stock, bench, "SPY", US, self.now)
        assert m.status == "closed"
        assert m.entry_date == dt.date(2026, 1, 5) and m.entry_price == 100
        assert m.mark_date == stock[4].date          # 5 trading days: bars 0..4
        assert m.raw_return == pytest.approx(105 / 100 - 1)
        assert m.bench_return == pytest.approx(101 / 100 - 1)
        assert m.excess_return == pytest.approx(0.05 - 0.01)

    def test_short_is_mirror_image(self):
        stock = weekday_bars(self.start, 40, open_=100, step=1.0)
        bench = weekday_bars(self.start, 40, open_=100, step=0.2)
        long_ = mark_idea(IdeaSpec("AAPL", "LONG", 5, at(2026, 1, 5, 12)), stock, bench, "SPY", US, self.now)
        short = mark_idea(IdeaSpec("AAPL", "SHORT", 5, at(2026, 1, 5, 12)), stock, bench, "SPY", US, self.now)
        assert short.excess_return == pytest.approx(-long_.excess_return)

    def test_bull_market_long_with_no_edge_scores_zero(self):
        # Stock and index rise identically: raw return looks great, excess is zero.
        stock = weekday_bars(self.start, 40, step=1.0)
        bench = weekday_bars(self.start, 40, step=1.0)
        m = mark_idea(IdeaSpec("X", "LONG", 10, at(2026, 1, 5, 12)), stock, bench, "SPY", US, self.now)
        assert m.raw_return > 0.09 and m.excess_return == pytest.approx(0)

    def test_open_until_horizon_completes(self):
        stock = weekday_bars(self.start, 10)
        bench = weekday_bars(self.start, 10)
        now = dt.datetime.combine(stock[-1].date + dt.timedelta(days=1), dt.time(12), UTC)
        m = mark_idea(IdeaSpec("X", "LONG", 20, at(2026, 1, 5, 12)), stock, bench, "SPY", US, now)
        assert m.status == "open" and m.mark_date == stock[-1].date

    def test_todays_bar_cannot_close_an_idea(self):
        stock = weekday_bars(self.start, 5)           # 5 Jan .. 9 Jan
        bench = weekday_bars(self.start, 5)
        now = at(2026, 1, 9, 16)                       # Friday 11:00 NY: today's bar in progress
        m = mark_idea(IdeaSpec("X", "LONG", 5, at(2026, 1, 5, 12)), stock, bench, "SPY", US, now)
        assert m.status == "open"
        later = at(2026, 1, 10, 12)
        assert mark_idea(IdeaSpec("X", "LONG", 5, at(2026, 1, 5, 12)), stock, bench, "SPY", US,
                         later).status == "closed"

    def test_normalisation_scales_by_sqrt_horizon(self):
        stock = weekday_bars(self.start, 40, step=1.0)
        bench = weekday_bars(self.start, 40, step=0.0)
        m = mark_idea(IdeaSpec("X", "LONG", 5, at(2026, 1, 5, 12)), stock, bench, "SPY", US, self.now)
        assert m.normalised_excess == pytest.approx(m.excess_return * 2)   # sqrt(20/5) = 2

    def test_missing_prices_is_unscorable_not_dropped(self):
        m = mark_idea(IdeaSpec("GONE", "LONG", 5, at(2026, 1, 5, 12)), [], weekday_bars(self.start, 5),
                      "SPY", US, self.now)
        assert m.status == "unscorable" and "no price data" in m.unscorable_reason

    def test_delisted_mid_idea_is_unscorable(self):
        stock = weekday_bars(self.start, 5)            # history stops 9 Jan
        bench = weekday_bars(self.start, 40)
        m = mark_idea(IdeaSpec("GONE", "LONG", 20, at(2026, 1, 5, 12)), stock, bench, "SPY", US, self.now)
        assert m.status == "unscorable" and "delisted" in m.unscorable_reason

    def test_idea_after_last_bar_is_pending(self):
        stock = weekday_bars(self.start, 5)
        bench = weekday_bars(self.start, 5)
        now = at(2026, 1, 9, 22)
        m = mark_idea(IdeaSpec("X", "LONG", 5, at(2026, 1, 9, 21)), stock, bench, "SPY", US, now)
        assert m.status == "pending_entry"

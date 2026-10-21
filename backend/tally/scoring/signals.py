# =============================================================================
# scoring/signals.py: TURN SKILLED PEOPLE'S OPEN IDEAS INTO "WHAT TO BUY/SELL" + RISK
# -----------------------------------------------------------------------------
# The "so what" of the leaderboard. For each ticker with open ideas:
#   score = weighted vote:  sum(weight x (+1 for LONG, -1 for SHORT)) / sum(weight)
#           in [-1, 1]: +1 = every skilled person is long, -1 = all short.
#   weight_mass = total weight behind it (how much skilled conviction there is)
# Weights come from skill.contributor_weight: unproven people get 0, so they don't count.
#
# Then a paper portfolio (portfolio_weights) and its risk (historical_risk):
#   VaR 95% (1 day) = "on 95% of days you'd lose less than this"
#   CVaR            = "on the worst 5% of days, the AVERAGE loss"
# computed by replaying the last ~250 real trading days through today's weights
# ("historical simulation": no bell-curve assumption).
#
# Pure functions; called by scoring/job.py (_rebuild_signals).
# =============================================================================
"""Aggregate live ideas into per-ticker signals, then measure the risk of acting on them.

This is the "so what" of the leaderboard: ideas from contributors with demonstrated skill
count more; ideas from unproven contributors count for nothing.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from typing import Hashable, Iterable, Mapping


# One row of the Signals table.
@dataclass
class TickerSignal:
    ticker: str
    score: float            # skill-weighted net direction in [-1, 1]
    weight_mass: float      # sum of contributor weights behind it (conviction)
    n_ideas: int
    n_long: int
    n_short: int
    n_contributors: int


def build_signals(open_ideas: Iterable[tuple[str, str, Hashable]],
                  weights: Mapping[Hashable, float]) -> list[TickerSignal]:
    """open_ideas: (ticker, direction, contributor_id). Latest view per contributor/ticker only."""
    # by_ticker = {"NVDA": {contributor_id: "LONG", ...}, ...}
    # One vote per person per ticker: if someone posted LONG then later SHORT, only the
    # latest counts (the ideas arrive sorted oldest-first, so later ones overwrite).
    by_ticker: dict[str, dict[Hashable, str]] = {}
    for ticker, direction, cid in open_ideas:
        by_ticker.setdefault(ticker, {})[cid] = direction   # later ideas overwrite earlier
    out = []
    for ticker, views in by_ticker.items():
        signed = mass = 0.0
        for cid, d in views.items():
            w = weights.get(cid, 0.0)                 # unknown contributor -> weight 0
            signed += w * (1 if d == "LONG" else -1)
            mass += w
        out.append(TickerSignal(
            ticker=ticker, score=signed / mass if mass > 0 else 0.0, weight_mass=mass,
            n_ideas=len(views), n_long=sum(d == "LONG" for d in views.values()),
            n_short=sum(d == "SHORT" for d in views.values()), n_contributors=len(views)))
    # Strongest signals first: |score x mass| = strong agreement AND lots of skill behind it.
    out.sort(key=lambda s: abs(s.score * s.weight_mass), reverse=True)
    return out


# A paper portfolio: each ticker's position is proportional to score x mass, scaled so
# the absolute weights add up to 1 (100% "gross exposure"). Positive = long, negative = short.
# Example: NVDA 0.6, TSLA -0.4 -> 60% of capital long NVDA, 40% short TSLA.
def portfolio_weights(signals: list[TickerSignal]) -> dict[str, float]:
    """Signed exposure proportional to score * conviction, scaled to gross exposure 1."""
    raw = {s.ticker: s.score * s.weight_mass for s in signals if s.weight_mass > 0 and s.score != 0}
    gross = sum(abs(v) for v in raw.values())
    return {t: v / gross for t, v in raw.items()} if gross > 0 else {}


# [(date, close), ...] -> {date: % change from the previous close}.
# zip(closes, closes[1:]) pairs each day with the next day.
def daily_returns(closes: list[tuple[dt.date, float]]) -> dict[dt.date, float]:
    return {d1: c1 / c0 - 1 for (_, c0), (d1, c1) in zip(closes, closes[1:], strict=False) if c0 > 0}


def historical_risk(weights: Mapping[str, float], returns: Mapping[str, Mapping[dt.date, float]],
                    level: float = 0.95, lookback: int = 250) -> dict | None:
    """Historical-simulation VaR/CVaR of the signal portfolio (1-day horizon).

    Replays the last `lookback` days of actual returns through today's weights. No
    normality assumption, so fat tails in the history show up in the estimate."""
    tickers = [t for t in weights if returns.get(t)]
    if not tickers:
        return None
    # Only use dates where EVERY ticker has a return (different exchanges have different holidays).
    common = set.intersection(*(set(returns[t]) for t in tickers))
    dates = sorted(common)[-lookback:]                # the most recent `lookback` days
    if len(dates) < 30:
        return None                                   # too little history to say anything
    # The portfolio's profit/loss on each historical day, sorted worst first.
    pnl = sorted(sum(weights[t] * returns[t][d] for t in tickers) for d in dates)
    k = max(1, int(math.floor((1 - level) * len(pnl))))   # how many days make up the worst 5%
    var = -pnl[k - 1]                                 # the loss at the 5% cut-off (as a positive number)
    cvar = -sum(pnl[:k]) / k                          # the average loss beyond the cut-off
    mean = sum(pnl) / len(pnl)
    vol = math.sqrt(sum((x - mean) ** 2 for x in pnl) / (len(pnl) - 1))   # daily standard deviation
    return {"level": level, "days": len(dates), "var_1d": var, "cvar_1d": cvar,
            "vol_annualised": vol * math.sqrt(252), "tickers": tickers}   # x sqrt(252) -> per year


# Dataclasses -> plain dicts, for storing as JSON in signal_snapshots.
def signals_to_json(signals: list[TickerSignal]) -> list[dict]:
    return [asdict(s) for s in signals]

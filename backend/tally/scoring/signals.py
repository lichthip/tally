"""Aggregate live ideas into per-ticker signals, then measure the risk of acting on them.

This is the "so what" of the leaderboard: ideas from contributors with demonstrated skill
count more; ideas from unproven contributors count for nothing.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from typing import Hashable, Iterable, Mapping


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
    by_ticker: dict[str, dict[Hashable, str]] = {}
    for ticker, direction, cid in open_ideas:
        by_ticker.setdefault(ticker, {})[cid] = direction   # later ideas overwrite earlier
    out = []
    for ticker, views in by_ticker.items():
        signed = mass = 0.0
        for cid, d in views.items():
            w = weights.get(cid, 0.0)
            signed += w * (1 if d == "LONG" else -1)
            mass += w
        out.append(TickerSignal(
            ticker=ticker, score=signed / mass if mass > 0 else 0.0, weight_mass=mass,
            n_ideas=len(views), n_long=sum(d == "LONG" for d in views.values()),
            n_short=sum(d == "SHORT" for d in views.values()), n_contributors=len(views)))
    out.sort(key=lambda s: abs(s.score * s.weight_mass), reverse=True)
    return out


def portfolio_weights(signals: list[TickerSignal]) -> dict[str, float]:
    """Signed exposure proportional to score * conviction, scaled to gross exposure 1."""
    raw = {s.ticker: s.score * s.weight_mass for s in signals if s.weight_mass > 0 and s.score != 0}
    gross = sum(abs(v) for v in raw.values())
    return {t: v / gross for t, v in raw.items()} if gross > 0 else {}


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
    common = set.intersection(*(set(returns[t]) for t in tickers))
    dates = sorted(common)[-lookback:]
    if len(dates) < 30:
        return None
    pnl = sorted(sum(weights[t] * returns[t][d] for t in tickers) for d in dates)
    k = max(1, int(math.floor((1 - level) * len(pnl))))
    var = -pnl[k - 1]
    cvar = -sum(pnl[:k]) / k
    mean = sum(pnl) / len(pnl)
    vol = math.sqrt(sum((x - mean) ** 2 for x in pnl) / (len(pnl) - 1))
    return {"level": level, "days": len(dates), "var_1d": var, "cvar_1d": cvar,
            "vol_annualised": vol * math.sqrt(252), "tickers": tickers}


def signals_to_json(signals: list[TickerSignal]) -> list[dict]:
    return [asdict(s) for s in signals]

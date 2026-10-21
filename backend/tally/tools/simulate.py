# =============================================================================
# tools/simulate.py: FAKE CONTRIBUTORS WITH KNOWN SKILL, TO TEST THE SCORER
# -----------------------------------------------------------------------------
# The question: "does the leaderboard rank people correctly?" can't be answered with
# real people, because nobody knows their true skill. So we INVENT 9 people whose skill
# we set ourselves (accuracy = how often they call the direction right), generate their
# ideas on REAL past prices, and then let the normal scoring code (which knows nothing
# about this) rank them. If the ranking matches the planted skill, the scorer works.
#
# How a persona "is right 68% of the time": for each fake idea we PEEK at what actually
# happened next (cheating on purpose, only here) and pick the right direction with
# probability = accuracy, the wrong one otherwise.
#
# simulation_report() then compares the rankings using Spearman rank correlation
# (1.0 = identical order, 0 = unrelated, -1 = reversed): raw averages vs shrunk scores.
#
# Run: `python -m tally simulate` (or the System tab button). `--report-only` = just the report.
# =============================================================================
"""Simulated contributors with KNOWN skill, to validate the scoring engine.

You can't check a skill estimator on real people: nobody knows their true skill. So we
plant it. Each persona is right about direction with a fixed probability ("accuracy"),
decided by peeking at the real future excess return (deliberate lookahead, only here,
to manufacture ground truth). Then the normal scoring job runs, blind to all this, and we
check whether it recovers the planted ordering, and whether it refuses to be fooled by
the lucky persona with 3 perfect calls.

Simulated rows are flagged (contributors.is_simulated) and use source='sim'.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..db import session_scope
from ..ingestion.service import upsert_contributor
from ..market.exchanges import exchange_for
from ..market.store import PriceStore
from ..models import Contributor, ContributorStat, RawPost, TradeIdea
from ..scoring.marking import IdeaSpec, mark_idea

log = logging.getLogger(__name__)


# One fake person: name, how often they're right, how many ideas they post.
@dataclass(frozen=True)
class Persona:
    handle: str
    accuracy: float     # P(direction matches the realised excess return)
    n_ideas: int


# The cast. The interesting ones: sharp_analyst (best, plenty of ideas), contrarian_fader
# (worse than a coin), hot_hand (mediocre but few ideas), lucky_streak (3 ideas, all right).
PERSONAS = [
    Persona("sim_sharp_analyst", 0.68, 60),
    Persona("sim_steady_pm", 0.58, 120),
    Persona("sim_solid_researcher", 0.60, 45),
    Persona("sim_coin_flipper", 0.50, 80),
    Persona("sim_index_hugger", 0.50, 30),
    Persona("sim_contrarian_fader", 0.38, 60),
    Persona("sim_hot_hand", 0.55, 8),
    Persona("sim_lucky_streak", 1.00, 3),     # 3 for 3. Raw stats love this account.
    Persona("sim_newbie", 0.50, 2),
]

# 40 real tickers the fake ideas are about; horizons in trading days.
UNIVERSE = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD", "INTC", "AVGO",
            "ORCL", "CRM", "NFLX", "PLTR", "UBER", "JPM", "GS", "BAC", "V", "MA", "XOM", "CVX",
            "LLY", "UNH", "PFE", "WMT", "COST", "HD", "NKE", "DIS", "BA", "CAT", "KO", "PEP",
            "SE", "GRAB", "BABA", "TSM", "MU", "QCOM"]
HORIZONS = [5, 10, 20, 40, 60]
# Post texts, so simulated posts look like real ones in the UI. {t}=ticker, {h}=horizon.
TEMPLATES = {
    "LONG": ["Long ${t}. Setup looks great, giving it ~{h} trading days.",
             "Buying ${t} here, I think the market is underpricing the next {h} days.",
             "Adding to ${t}, bullish into the next few weeks (~{h} sessions)."],
    "SHORT": ["Short ${t}. Expecting it to underperform over ~{h} trading days.",
              "Bought ${t} puts, this looks toppy for the next {h} sessions.",
              "Bearish ${t}, fading the rally (~{h} days)."],
}


# Create all the fake people, posts and ideas (over the last `days` days). seed=7 makes
# the "random" choices the same every run (reproducible).
def simulate(sf: sessionmaker[Session], store: PriceStore, *, days: int = 420, seed: int = 7,
             now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    # Only run once: if any simulated contributor exists, do nothing.
    with sf() as s:
        if s.execute(select(Contributor.id).where(Contributor.source == "sim").limit(1)).first():
            return {"skipped": True, "reason": "simulated contributors already exist"}

    rng = random.Random(seed)
    start = (now - dt.timedelta(days=days)).date()
    # Real prices for every ticker (through the cache) and for the benchmark SPY.
    bars = {t: store.get_bars(t, start - dt.timedelta(days=10)) for t in UNIVERSE}
    bars = {t: b for t, b in bars.items() if len(b) > 100}     # drop tickers with too little history
    bench = store.get_bars("SPY", start - dt.timedelta(days=10))
    if not bars or not bench:
        raise RuntimeError("no price data available for simulation (check market data provider)")

    created = 0
    for p in PERSONAS:
        with session_scope(sf) as s:
            # true_skill stored as accuracy - 0.5, so a coin flipper = 0.
            cid = upsert_contributor(s, "sim", p.handle, is_simulated=True, true_skill=p.accuracy - 0.5)
            for k in range(p.n_ideas):
                ticker = rng.choice(list(bars))
                h = rng.choice(HORIZONS)
                tb = bars[ticker]
                # Pick a day for this idea.
                # Most ideas old enough to have closed; ~10% recent and still open.
                if rng.random() < 0.1:
                    idx = rng.randint(max(0, len(tb) - h), len(tb) - 2)
                else:
                    idx = rng.randint(0, max(0, len(tb) - h - 3))
                day = tb[idx].date
                when = dt.datetime.combine(day, dt.time(12, 0), dt.timezone.utc)  # before NY open
                direction = _choose_direction(rng, p.accuracy, ticker, h, when, tb, bench, now)
                text = rng.choice(TEMPLATES[direction]).format(t=ticker, h=h)
                key = hashlib.sha256(f"sim|id|{p.handle}-{k}".encode()).hexdigest()
                # Insert the post directly as "extracted" (no LLM needed for fake posts).
                # received_at is backdated to `when`: allowed here because WE generate it
                # (the API never lets anyone backdate).
                post_id = s.execute(pg_insert(RawPost).values(
                    source="sim", external_id=f"{p.handle}-{k}", dedupe_key=key, contributor_id=cid,
                    text=text, posted_at=when, received_at=when, status="extracted",
                    extractor="simulator", processed_at=when,
                    extraction={"status": "extracted", "notes": ["simulated"]},
                ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(RawPost.id)).scalar()
                if post_id is None:
                    continue
                s.execute(pg_insert(TradeIdea).values(
                    post_id=post_id, contributor_id=cid, ticker=ticker, direction=direction,
                    horizon_days=h, horizon_inferred=False, conviction="medium", confidence=1.0,
                    rationale="simulated", received_at=when, effective_at=when, extractor="simulator"))
                created += 1
    return {"skipped": False, "ideas_created": created, "personas": len(PERSONAS)}


# The "peek": mark a hypothetical LONG with the real marking function. If it beat the
# market, LONG was the right call; otherwise SHORT. Then be right with probability = accuracy.
def _choose_direction(rng: random.Random, accuracy: float, ticker: str, h: int, when: dt.datetime,
                      tb, bench, now: dt.datetime) -> str:
    ex = exchange_for(ticker)
    m = mark_idea(IdeaSpec(ticker, "LONG", h, when), tb, bench, "SPY", ex, now)
    if m.status != "closed" or m.excess_return is None:
        # The horizon hasn't finished yet, so the answer isn't known: a coin flip.
        # (This is why sim_lucky_streak came out 2/3 instead of 3/3 in your run.)
        return rng.choice(["LONG", "SHORT"])  # future unknown: no peeking possible
    right = "LONG" if m.excess_return > 0 else "SHORT"
    wrong = "SHORT" if right == "LONG" else "LONG"
    return right if rng.random() < accuracy else wrong


# Compare what the scorer found with the truth we planted.
def simulation_report(sf: sessionmaker[Session]) -> dict:
    """How well did the scorer recover planted skill? Compares rank correlation of
    (a) raw mean excess and (b) shrunk posterior mean against the true accuracy."""
    with sf() as s:
        rows = s.execute(
            select(Contributor.handle, Contributor.true_skill, ContributorStat.n_closed,
                   ContributorStat.hit_rate, ContributorStat.mean_excess,
                   ContributorStat.posterior_mean, ContributorStat.prob_positive)
            .join(ContributorStat, ContributorStat.contributor_id == Contributor.id)
            .where(Contributor.is_simulated.is_(True))).all()
    rows = [r for r in rows if r.mean_excess is not None]       # only people with closed ideas
    table = [{"handle": r.handle, "true_accuracy": round(r.true_skill + 0.5, 2), "n_closed": r.n_closed,
              "hit_rate": r.hit_rate, "raw_mean": r.mean_excess, "shrunk": r.posterior_mean,
              "p_skill": r.prob_positive} for r in rows]
    table.sort(key=lambda x: x["shrunk"], reverse=True)
    truth = [r.true_skill for r in rows]
    return {"table": table,
            "spearman_raw_vs_truth": _spearman([r.mean_excess for r in rows], truth),
            "spearman_shrunk_vs_truth": _spearman([r.posterior_mean for r in rows], truth),
            "raw_rank_of_lucky": _rank_of(table, "raw_mean", "sim_lucky_streak"),
            "shrunk_rank_of_lucky": _rank_of(table, "shrunk", "sim_lucky_streak")}


# Turn values into ranks: [0.3, 0.1, 0.2] -> [3, 1, 2]. Ties share the average rank.
def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])    # positions, smallest value first
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1                                       # extend over a run of equal values
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1                # the average rank of the tied group
        i = j + 1
    return r


# Spearman rank correlation = the ordinary (Pearson) correlation of the RANKS.
# It only cares whether the ORDER matches, not the exact values.
def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 3) if va and vb else None


# Where does `handle` rank (1 = top) when the table is sorted by `key`?
def _rank_of(table: list[dict], key: str, handle: str) -> int | None:
    ordered = sorted(table, key=lambda x: x[key], reverse=True)
    for i, r in enumerate(ordered, 1):          # enumerate(..., 1): count from 1, not 0
        if r["handle"] == handle:
            return i
    return None

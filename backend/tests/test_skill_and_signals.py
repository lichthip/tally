"""Empirical-Bayes skill estimates and signal aggregation. Pure functions."""
import datetime as dt
import random

import pytest

from tally.scoring.signals import build_signals, historical_risk, portfolio_weights
from tally.scoring.skill import contributor_weight, estimate_skill


def noisy(rng, n, mean, sd=0.08):
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_lucky_small_sample_is_shrunk_below_proven_skill():
    rng = random.Random(1)
    data = {
        "lucky": [0.06, 0.05, 0.07],                 # 3 for 3, big returns
        "proven": noisy(rng, 150, 0.015),            # small edge, lots of evidence
        **{f"noise{i}": noisy(rng, 60, 0.0) for i in range(6)},
    }
    est, model = estimate_skill(data)
    assert est["lucky"].mean > est["proven"].mean                     # raw says lucky is best
    assert est["proven"].posterior_mean > est["lucky"].posterior_mean  # shrinkage disagrees
    assert est["proven"].prob_positive > 0.8
    assert model.tau2 > 0 and model.sigma2 > 0


def test_shrinkage_grows_weaker_with_more_evidence():
    rng = random.Random(2)
    base = {f"c{i}": noisy(rng, 80, rng.gauss(0, 0.02)) for i in range(10)}
    est_small, _ = estimate_skill({**base, "x": [0.03] * 5})
    est_big, _ = estimate_skill({**base, "x": [0.03] * 200})
    ratio_small = est_small["x"].posterior_mean / 0.03
    ratio_big = est_big["x"].posterior_mean / 0.03
    assert 0 < ratio_small < ratio_big < 1


def test_population_without_skill_stays_near_zero():
    rng = random.Random(3)
    est, model = estimate_skill({f"c{i}": noisy(rng, 40, 0.0) for i in range(12)})
    for e in est.values():
        assert abs(e.posterior_mean) < 0.01
        assert 0.3 < e.prob_positive < 0.7


def test_contributor_without_closed_ideas():
    est, _ = estimate_skill({"a": [0.01, 0.02, -0.01], "empty": []})
    assert est["empty"].n == 0 and est["empty"].posterior_mean is None


def test_contributor_weight():
    assert contributor_weight(None) == 0
    assert contributor_weight(0.5) == 0
    assert contributor_weight(0.3) == 0           # negative skill: ignored, not faded
    assert contributor_weight(0.9) == pytest.approx(0.8)


def test_signals_weight_skilled_contributors_and_keep_latest_view():
    ideas = [("NVDA", "LONG", "skilled"), ("NVDA", "SHORT", "unproven"),
             ("TSLA", "LONG", "skilled"), ("TSLA", "SHORT", "skilled")]   # changed mind -> SHORT
    weights = {"skilled": 0.8, "unproven": 0.0}
    sig = {s.ticker: s for s in build_signals(ideas, weights)}
    assert sig["NVDA"].score == 1.0 and sig["NVDA"].n_ideas == 2
    assert sig["TSLA"].score == -1.0 and sig["TSLA"].n_ideas == 1


def test_portfolio_weights_gross_one():
    sig = build_signals([("A", "LONG", 1), ("B", "SHORT", 2)], {1: 0.6, 2: 0.2})
    w = portfolio_weights(sig)
    assert sum(abs(v) for v in w.values()) == pytest.approx(1)
    assert w["A"] > 0 > w["B"]


def test_historical_var_cvar_ordering():
    rng = random.Random(4)
    days = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(300)]
    rets = {"A": {d: rng.gauss(0, 0.02) for d in days}, "B": {d: rng.gauss(0, 0.01) for d in days}}
    r = historical_risk({"A": 0.5, "B": -0.5}, rets)
    assert r["days"] == 250
    assert r["cvar_1d"] >= r["var_1d"] > 0          # expected shortfall is beyond VaR

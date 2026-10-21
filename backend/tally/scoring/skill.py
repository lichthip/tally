# =============================================================================
# scoring/skill.py: STEP 6: HOW SKILLED IS EACH CONTRIBUTOR? (shrinkage)
# -----------------------------------------------------------------------------
# The problem in one line: 3 lucky calls should not beat 100 decent calls.
#
# Intuition before the maths: imagine a "trust slider" per person, from 0 to 1.
#   skill estimate = (their average excess return) x (trust)
#   trust is LOW when they have few ideas, or when single ideas are very noisy.
#   trust is HIGH when they have many ideas.
#   With 3 ideas, trust ~ 0.1, so even a +20% average becomes ~ +2%.
#   With 100 ideas, trust ~ 0.8, so the average mostly stands.
# This pulling-towards-zero is called SHRINKAGE. The exact trust value comes from a
# Bayesian formula, and its two inputs are estimated from everyone's data (that's the
# "empirical" in "empirical Bayes"):
#   sigma^2 = how noisy ONE idea's return is (spread of returns around a person's average)
#   tau^2   = how much TRUE skill really differs between people
#   trust (shrink) = n*tau^2 / (n*tau^2 + sigma^2)
#
# Output per person: posterior mean (shrunk skill), posterior sd (uncertainty), and
# P(skill > 0): "how sure are we this person beats the market at all?". That probability
# becomes their weight in the Signals page (contributor_weight, bottom of the file).
#
# Pure functions again: lists of numbers in, numbers out (tests/test_skill_and_signals.py).
# =============================================================================
"""Contributor skill via empirical-Bayes shrinkage.

Problem: someone who is 3-for-3 has a 100% hit rate, but three coin flips come up heads
12.5% of the time. Raw averages reward luck and small samples.

Model (normal-normal):
    true skill of contributor c:     alpha_c ~ Normal(0, tau^2)        <- prior: "no skill"
    each idea's normalised excess:   x_ci    ~ Normal(alpha_c, sigma^2)

Posterior for alpha_c after n ideas with mean m:
    precision = 1/tau^2 + n/sigma^2
    mean      = (n/sigma^2 * m) / precision      = m * shrink,  shrink = n*tau^2 / (n*tau^2 + sigma^2)
    sd        = sqrt(1 / precision)

So with few ideas, `shrink` is near 0 and the estimate stays near zero; with many ideas it
approaches the raw mean. "Empirical" Bayes: sigma^2 and tau^2 are estimated from the data
itself rather than picked by hand:
    sigma^2 = pooled within-contributor variance of idea returns (how noisy one idea is)
    tau^2   = method of moments: E[m_c^2] = tau^2 + sigma^2/n_c  =>  tau^2 = mean(m_c^2 - sigma^2/n_c)

The prior is centred on ZERO deliberately (not on the crowd average): skill has to be
demonstrated; it isn't assumed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist      # the bell curve; .cdf(z) = P(Normal < z)
from typing import Hashable, Mapping, Sequence

_N = NormalDist()                       # standard normal (mean 0, sd 1)


# Result for one contributor.
@dataclass
class SkillEstimate:
    n: int                            # number of CLOSED ideas used
    mean: float | None                # raw average normalised excess return
    hit_rate: float | None            # share of ideas with a positive excess
    posterior_mean: float | None      # shrunk skill estimate (the leaderboard score)
    posterior_sd: float | None        # uncertainty around it
    prob_positive: float | None       # P(true skill > 0 | data)


# The two global numbers the model estimated (shown in the scoring summary).
@dataclass
class SkillModel:
    sigma2: float
    tau2: float
    n_contributors: int
    n_ideas: int


# returns: {contributor_id: [normalised excess of each CLOSED idea]}.
# Returns ({contributor_id: SkillEstimate}, SkillModel).
def estimate_skill(returns: Mapping[Hashable, Sequence[float]], tau2_floor: float = 1e-6,
                   default_sigma2: float = 0.01) -> tuple[dict[Hashable, SkillEstimate], SkillModel]:
    groups = {k: list(v) for k, v in returns.items()}
    scored = {k: v for k, v in groups.items() if v}       # people with at least 1 closed idea
    N = sum(len(v) for v in scored.values())              # total closed ideas
    C = len(scored)                                       # number of people with data

    means = {k: sum(v) / len(v) for k, v in scored.items()}   # each person's raw average

    # --- sigma^2: noise of a single idea ---------------------------------------
    # Pooled within-person variance: how far each idea is from ITS AUTHOR's average,
    # squared, summed over everyone, divided by (N - C). (Like a normal variance, but each
    # person "uses up" one degree of freedom for their own average.)
    if N - C >= 2:
        ss = sum((x - means[k]) ** 2 for k, v in scored.items() for x in v)
        sigma2 = ss / (N - C)
    elif N >= 2:
        # Too little data for that (e.g. everyone has exactly 1 idea): plain variance of all ideas.
        allx = [x for v in scored.values() for x in v]
        mu = sum(allx) / N
        sigma2 = sum((x - mu) ** 2 for x in allx) / (N - 1)
    else:
        sigma2 = default_sigma2                           # basically no data: a sensible default
    sigma2 = max(sigma2, 1e-8)                            # never exactly 0 (we divide by it)

    # --- tau^2: how much true skill varies between people ----------------------
    # A person's average m wobbles for two reasons: real skill (variance tau^2) and luck
    # (variance sigma^2/n). So on average  n*m^2 ~ n*tau^2 + sigma^2. Summing over people
    # and solving for tau^2 gives this line. Weighting by n means tiny accounts can't drag
    # the estimate to zero (the bug the simulation caught).
    if C:
        tau2 = (sum(len(v) * means[k] ** 2 for k, v in scored.items()) - C * sigma2) / N
    else:
        tau2 = 0.0
    tau2 = max(tau2, tau2_floor)                          # can come out negative from noise; floor it

    # --- per-person posterior ---------------------------------------------------
    out: dict[Hashable, SkillEstimate] = {}
    for k, v in groups.items():
        n = len(v)
        if n == 0:
            out[k] = SkillEstimate(0, None, None, None, None, None)   # nothing closed yet
            continue
        m = means[k]
        precision = 1 / tau2 + n / sigma2     # precision = 1/variance; information from prior + data
        pm = (n / sigma2) * m / precision     # = m * shrink   (the shrunk estimate)
        psd = math.sqrt(1 / precision)        # posterior standard deviation
        out[k] = SkillEstimate(
            n=n, mean=m, hit_rate=sum(1 for x in v if x > 0) / n,
            # P(skill > 0) = P(Normal(pm, psd) > 0) = cdf(pm / psd)
            posterior_mean=pm, posterior_sd=psd, prob_positive=_N.cdf(pm / psd))
    return out, SkillModel(sigma2=sigma2, tau2=tau2, n_contributors=C, n_ideas=N)


# Turn P(skill > 0) into a weight for the Signals page:
#   P = 0.5 (coin flip, can't tell) -> weight 0
#   P = 0.9                         -> weight 0.8
#   P = 1.0                         -> weight 1
#   P < 0.5 (probably bad)          -> weight 0 (not negative; see the docstring)
def contributor_weight(prob_positive: float | None) -> float:
    """Weight for signal aggregation: 0 if we can't tell skill from luck, up to 1.
    2p-1 maps P(skill>0)=0.5 -> 0 and P=1 -> 1; negative-skill contributors get 0
    (we could fade them, but that's a separate, riskier decision)."""
    if prob_positive is None:
        return 0.0
    return max(0.0, 2 * prob_positive - 1)

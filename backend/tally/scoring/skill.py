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
from statistics import NormalDist
from typing import Hashable, Mapping, Sequence

_N = NormalDist()


@dataclass
class SkillEstimate:
    n: int
    mean: float | None
    hit_rate: float | None
    posterior_mean: float | None
    posterior_sd: float | None
    prob_positive: float | None       # P(true skill > 0 | data)


@dataclass
class SkillModel:
    sigma2: float
    tau2: float
    n_contributors: int
    n_ideas: int


def estimate_skill(returns: Mapping[Hashable, Sequence[float]], tau2_floor: float = 1e-6,
                   default_sigma2: float = 0.01) -> tuple[dict[Hashable, SkillEstimate], SkillModel]:
    groups = {k: list(v) for k, v in returns.items()}
    scored = {k: v for k, v in groups.items() if v}
    N = sum(len(v) for v in scored.values())
    C = len(scored)

    means = {k: sum(v) / len(v) for k, v in scored.items()}
    if N - C >= 2:
        ss = sum((x - means[k]) ** 2 for k, v in scored.items() for x in v)
        sigma2 = ss / (N - C)
    elif N >= 2:
        allx = [x for v in scored.values() for x in v]
        mu = sum(allx) / N
        sigma2 = sum((x - mu) ** 2 for x in allx) / (N - 1)
    else:
        sigma2 = default_sigma2
    sigma2 = max(sigma2, 1e-8)

    if C:
        tau2 = (sum(len(v) * means[k] ** 2 for k, v in scored.items()) - C * sigma2) / N
    else:
        tau2 = 0.0
    tau2 = max(tau2, tau2_floor)

    out: dict[Hashable, SkillEstimate] = {}
    for k, v in groups.items():
        n = len(v)
        if n == 0:
            out[k] = SkillEstimate(0, None, None, None, None, None)
            continue
        m = means[k]
        precision = 1 / tau2 + n / sigma2
        pm = (n / sigma2) * m / precision
        psd = math.sqrt(1 / precision)
        out[k] = SkillEstimate(
            n=n, mean=m, hit_rate=sum(1 for x in v if x > 0) / n,
            posterior_mean=pm, posterior_sd=psd, prob_positive=_N.cdf(pm / psd))
    return out, SkillModel(sigma2=sigma2, tau2=tau2, n_contributors=C, n_ideas=N)


def contributor_weight(prob_positive: float | None) -> float:
    """Weight for signal aggregation: 0 if we can't tell skill from luck, up to 1.
    2p-1 maps P(skill>0)=0.5 -> 0 and P=1 -> 1; negative-skill contributors get 0
    (we could fade them, but that's a separate, riskier decision)."""
    if prob_positive is None:
        return 0.0
    return max(0.0, 2 * prob_positive - 1)

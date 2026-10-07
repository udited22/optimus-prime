"""Statistics for the validation gates: day-block bootstrap, Sharpe moments, the Deflated Sharpe Ratio, drawdown
Monte Carlo. Pure Python and seeded, so every number in a report is reproducible."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

_EULER = 0.5772156649015329
_N = statistics.NormalDist()


def percentile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 1]."""
    if not xs:
        raise ValueError("empty sample")
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def day_block_bootstrap_ci(
    days: Sequence[date], pnl: Sequence[float], *, level: float, iterations: int, seed: int
) -> tuple[float, float, float]:
    """(mean per trade, lower, upper): resample whole trading days with replacement (docs/research/validation.md V5)."""
    if len(days) != len(pnl) or not pnl:
        raise ValueError("need one day per trade and at least one trade")
    blocks: dict[date, list[float]] = {}
    for d, p in zip(days, pnl, strict=True):
        blocks.setdefault(d, []).append(p)
    keys = sorted(blocks)
    sums = [sum(blocks[k]) for k in keys]
    counts = [len(blocks[k]) for k in keys]
    rng = random.Random(seed)
    n = len(keys)
    means = []
    for _ in range(iterations):
        idx = [rng.randrange(n) for _ in range(n)]
        means.append(sum(sums[i] for i in idx) / sum(counts[i] for i in idx))
    a = (1 - level) / 2
    return sum(pnl) / len(pnl), percentile(means, a), percentile(means, 1 - a)


@dataclass(frozen=True, slots=True)
class Moments:
    n: int
    mean: float
    sd: float
    skew: float
    kurt: float  # non-excess (a normal sample is 3)

    @property
    def sharpe(self) -> float:
        return self.mean / self.sd if self.sd > 0 else 0.0


def moments(xs: Sequence[float]) -> Moments:
    n = len(xs)
    if n < 2:
        raise ValueError("need at least two observations")
    m = sum(xs) / n
    sd = statistics.pstdev(xs)
    if sd == 0:
        return Moments(n, m, 0.0, 0.0, 3.0)
    sk = sum((x - m) ** 3 for x in xs) / n / sd**3
    ku = sum((x - m) ** 4 for x in xs) / n / sd**4
    return Moments(n, m, sd, sk, ku)


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] of ``n_trials`` independent zero-skill trials (Bailey & Lopez de Prado 2014, eq. for SR0)."""
    if n_trials < 1 or sr_variance < 0:
        raise ValueError("n_trials >= 1 and sr_variance >= 0")
    if n_trials == 1:
        return 0.0
    z1 = _N.inv_cdf(1 - 1 / n_trials)
    z2 = _N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1 - _EULER) * z1 + _EULER * z2)


def deflated_sharpe(m: Moments, *, n_trials: int, sr_variance: float | None = None) -> tuple[float, float]:
    """(DSR probability, SR0). SR is per trade (not annualised). ``sr_variance``: the variance of the Sharpe ratios
    across the registry's trials; when unknown, the estimator variance of this SR is used (a lenient fallback)."""
    sr = m.sharpe
    denom = 1 - m.skew * sr + (m.kurt - 1) / 4 * sr * sr
    if denom <= 0 or m.n < 2:
        return 0.0, math.inf
    var = sr_variance if sr_variance is not None else denom / (m.n - 1)
    sr0 = expected_max_sharpe(n_trials, var)
    return _N.cdf((sr - sr0) * math.sqrt(m.n - 1) / math.sqrt(denom)), sr0


def max_drawdown_frac(pnl: Sequence[float], nav: float) -> tuple[float, float]:
    """(max drawdown as a fraction of the running peak, lowest equity as a fraction of the start)."""
    eq = peak = nav
    worst, low = 0.0, nav
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        worst = max(worst, (peak - eq) / peak if peak > 0 else 1.0)
        low = min(low, eq)
    return worst, low / nav


def mc_drawdown(
    pnl: Sequence[float], *, nav: float, iterations: int, seed: int, dd_frac: float, ruin_frac: float
) -> tuple[float, float]:
    """(P(max DD > dd_frac), P(equity <= ruin_frac x start)) over bootstrapped trade sequences
    (docs/research/validation.md V13)."""
    rng = random.Random(seed)
    n = len(pnl)
    if n == 0:
        raise ValueError("no trades")
    over = ruin = 0
    for _ in range(iterations):
        seq = [pnl[rng.randrange(n)] for _ in range(n)]
        dd, low = max_drawdown_frac(seq, nav)
        over += dd > dd_frac
        ruin += low <= ruin_frac
    return over / iterations, ruin / iterations

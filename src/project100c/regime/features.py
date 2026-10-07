"""Pure indicator maths for the regime classifier. Inputs are plain sequences of floats (converted from the Decimal
bars once, at the classifier boundary); the outputs only ever feed categorical votes, never prices or orders, so
float arithmetic is acceptable here and is deterministic for the same inputs."""

from __future__ import annotations

import math
from collections.abc import Sequence

MINUTES_PER_YEAR = 252 * 375  # NSE F&O session 09:15-15:30 = 375 one-minute bars


def wilder_adx(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int
) -> tuple[float, float, float] | None:
    """(ADX, +DI, -DI) with Wilder smoothing over the whole series, or None before 2*period bars exist."""
    n = len(close)
    if n < 2 * period + 1 or not (len(high) == len(low) == n):
        return None
    tr_s = pdm_s = mdm_s = 0.0
    dx: list[float] = []
    adx = 0.0
    pdi = mdi = 0.0
    for i in range(1, n):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm = up if up > down and up > 0 else 0.0
        mdm = down if down > up and down > 0 else 0.0
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        if i <= period:
            tr_s, pdm_s, mdm_s = tr_s + tr, pdm_s + pdm, mdm_s + mdm
        else:
            tr_s = tr_s - tr_s / period + tr
            pdm_s = pdm_s - pdm_s / period + pdm
            mdm_s = mdm_s - mdm_s / period + mdm
        if i < period:
            continue
        pdi = 100 * pdm_s / tr_s if tr_s > 0 else 0.0
        mdi = 100 * mdm_s / tr_s if tr_s > 0 else 0.0
        s = pdi + mdi
        d = 100 * abs(pdi - mdi) / s if s > 0 else 0.0
        if len(dx) < period:
            dx.append(d)
            if len(dx) == period:
                adx = sum(dx) / period
        else:
            adx = (adx * (period - 1) + d) / period
    if len(dx) < period:
        return None
    return adx, pdi, mdi


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], bars: int) -> float | None:
    """Mean true range over the last ``bars`` bars."""
    n = len(close)
    if n < bars + 1:
        return None
    trs = [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(n - bars, n)]
    return sum(trs) / bars


def ls_slope(values: Sequence[float]) -> float:
    """Least-squares slope per step of ``values`` (needs >= 2 points)."""
    n = len(values)
    mx = (n - 1) / 2
    my = sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def realised_vol_pct(close: Sequence[float], bars: int) -> float | None:
    """Annualised realised volatility (%) of 1-minute log returns over the last ``bars`` returns."""
    if len(close) < bars + 1:
        return None
    rets = [math.log(close[i] / close[i - 1]) for i in range(len(close) - bars, len(close))]
    m = sum(rets) / bars
    var = sum((r - m) ** 2 for r in rets) / (bars - 1)
    return math.sqrt(var) * math.sqrt(MINUTES_PER_YEAR) * 100


def percentile_rank(history: Sequence[float], x: float) -> float:
    """Share (0-100) of ``history`` strictly below x, plus half the ties."""
    if not history:
        raise ValueError("empty history")
    below = sum(1 for h in history if h < x)
    ties = sum(1 for h in history if h == x)
    return 100.0 * (below + 0.5 * ties) / len(history)


def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))

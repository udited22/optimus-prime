"""Black-Scholes helpers for library plug-ins that read option prices (H19 implied variance, H22 gamma).

Own calculation, no vendor Greeks: European Black-Scholes on the index spot with an ASSUMED 6.5% rate, time to
expiry in calendar years to 15:30 IST on the expiry day. Good enough to rank days and to compare implied with
realised variance; not a pricing model.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time

from project100c.sessions.model import IST

RATE = 0.065
MIN_PER_YEAR = 365 * 24 * 60
EXPIRY_CLOSE = time(15, 30)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_expiry(now: datetime, expiry: date) -> float:
    end = datetime.combine(expiry, EXPIRY_CLOSE, IST)
    return max((end - now).total_seconds() / 60 / MIN_PER_YEAR, 1.0 / MIN_PER_YEAR)


def bs_price(spot: float, strike: float, t: float, vol: float, call: bool) -> float:
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (RATE + 0.5 * vol * vol) * t) / sd
    d2 = d1 - sd
    if call:
        return spot * _ncdf(d1) - strike * math.exp(-RATE * t) * _ncdf(d2)
    return strike * math.exp(-RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float, call: bool) -> float | None:
    """Bisection on [1%, 300%]; None when the price is outside the no-arbitrage band of that interval."""
    lo, hi = 0.01, 3.0
    if not (bs_price(spot, strike, t, lo, call) <= price <= bs_price(spot, strike, t, hi, call)):
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, call) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_gamma(spot: float, strike: float, t: float, vol: float) -> float:
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (RATE + 0.5 * vol * vol) * t) / sd
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (spot * sd)


def gamma_concentration(
    legs: list[tuple[float, float, int, int]], *, spot: float, t_by_strike: dict[float, float] | None = None, t: float
) -> float:
    """H22 / F-GEX-001 partial-chain gamma concentration: sum of BS gamma x OI x lot x spot^2 x 1% over
    ``legs`` = (strike, iv as a fraction, oi in contracts, lot). ``t_by_strike`` overrides ``t`` per strike (two
    expiries). Every option is counted as dealer-short (the pre-registered India-short sign), so G >= 0."""
    g = 0.0
    for k, iv, oi, lot in legs:
        tt = t if t_by_strike is None else t_by_strike.get(k, t)
        g += bs_gamma(spot, k, tt, iv) * oi * lot * spot * spot * 0.01
    return g

"""SYNTHETIC market for the dashboard simulator: a seeded NIFTY path, India VIX, and a Black-Scholes option chain.

Nothing here is market data. The values are generated from a seed so that replays are reproducible, and every event
built from them is labelled SIMULATED."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from project100c.sessions.model import IST

TICK = Decimal("0.05")
STEP = 50
_MIN_PER_YEAR = 365 * 24 * 60
_RATE = 0.065  # assumed risk-free rate for the synthetic chain


def q_tick(x: float | Decimal, tick: Decimal = TICK) -> Decimal:
    return (Decimal(str(x)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, call: bool) -> float:
    t = max(t_years, 1.0 / _MIN_PER_YEAR)
    d1 = (math.log(spot / strike) + (_RATE + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if call:
        return spot * _ncdf(d1) - strike * math.exp(-_RATE * t) * _ncdf(d2)
    return strike * math.exp(-_RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1)


@dataclass(frozen=True, slots=True)
class MarketPoint:
    ts: datetime
    spot: Decimal
    vix: Decimal


def synthetic_day(
    day: date, seed: int, step_s: int, *, start: time = time(9, 15), end: time = time(15, 30)
) -> list[MarketPoint]:
    """One session of 'NIFTY' and 'VIX' points every ``step_s`` seconds. Regime blocks of 45 minutes, each with its own
    drift, so that some days trend and some chop. Deterministic per (day, seed)."""
    rng = random.Random(f"{day.isoformat()}|{seed}")
    t = datetime.combine(day, start, IST)
    t_end = datetime.combine(day, end, IST)
    s = 24800.0 + rng.uniform(-500, 500)
    s *= 1 + rng.gauss(0, 0.003)  # opening gap
    v = 12.5 + rng.uniform(-1.5, 2.5)
    out: list[MarketPoint] = []
    drift = 0.0
    i = 0
    steps_per_block = max(1, 45 * 60 // step_s)
    while t <= t_end:
        if i % steps_per_block == 0:
            drift = rng.gauss(0, 0.000012) * (1.8 if rng.random() < 0.35 else 0.6)
        sigma = v / 100 * math.sqrt(step_s / (252 * 6.25 * 3600))
        s *= math.exp(drift + sigma * rng.gauss(0, 1))
        v = max(9.0, v + 0.002 * (13.0 - v) + rng.gauss(0, 0.012) + (abs(drift) * 300 if drift < 0 else 0))
        out.append(MarketPoint(t, Decimal(str(round(s, 2))), Decimal(str(round(v, 2)))))
        t += timedelta(seconds=step_s)
        i += 1
    return out


def synthetic_prev_close(day: date, seed: int) -> Decimal:
    """The SIMULATED previous close behind ``synthetic_day``: the level before its opening gap (same RNG stream)."""
    rng = random.Random(f"{day.isoformat()}|{seed}")
    return Decimal(str(round(24800.0 + rng.uniform(-500, 500), 2)))


def years_to_expiry(now: datetime, expiry: date) -> float:
    close = datetime.combine(expiry, time(15, 30), IST)
    return max((close - now).total_seconds() / 60.0, 1.0) / _MIN_PER_YEAR


@dataclass(frozen=True, slots=True)
class OptionQuote:
    strike: int
    call: bool
    theo: Decimal
    bid: Decimal
    ask: Decimal
    iv: Decimal


def option_quote(spot: Decimal, strike: int, call: bool, now: datetime, expiry: date, vix: Decimal) -> OptionQuote:
    s = float(spot)
    mny = abs(strike - s) / s
    iv = float(vix) / 100 * (1 + 1.8 * mny) + (0.01 if not call else 0.0)  # simple skew/smile, SIMULATED
    theo = max(0.05, bs_price(s, strike, years_to_expiry(now, expiry), iv, call))
    half = max(float(TICK), 0.004 * theo)
    bid = max(TICK, q_tick(theo - half))
    ask = max(bid + TICK, q_tick(theo + half))
    return OptionQuote(strike, call, q_tick(theo), bid, ask, Decimal(str(round(iv * 100, 2))))


def atm(spot: Decimal) -> int:
    return int((spot / STEP).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * STEP

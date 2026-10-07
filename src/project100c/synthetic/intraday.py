"""SYNTHETIC one-minute NIFTY index, NIFTY futures and India VIX bars from a seeded, scripted day plan.

Nothing here is market data. A ``DayPlan`` scripts the shape of a session (opening gap, piecewise drift and
volatility segments, one-bar shocks, a VIX path), so tests can build the exact situations a strategy or the regime
classifier must handle (a trend day, a range day, a compression then a breakout, a gap that fades, a VIX spike)
and get the same bars every time. Every series is keyed ``SYNTH|...`` so it can never be mistaken for a real feed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from project100c.market_types import Bar
from project100c.sessions.model import IST

INDEX_KEY = "SYNTH|NIFTY_INDEX"
FUT_KEY = "SYNTH|NIFTY_FUT"
VIX_KEY = "SYNTH|INDIA_VIX"
SESSION_OPEN = time(9, 15)
SESSION_MINUTES = 375  # 09:15-15:30
_TICK = Decimal("0.05")
_MIN_PER_YEAR = 252 * 375


def _q(x: float, tick: Decimal = _TICK) -> Decimal:
    return (Decimal(repr(x)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


@dataclass(frozen=True, slots=True)
class Segment:
    """``minutes`` bars with a drift (% per minute) and an annualised volatility (%)."""

    minutes: int
    drift_pct_per_min: float = 0.0
    vol_pct: float = 12.0
    # mean reversion toward the segment's starting price (0 = none, 0.2 = strong): range days
    revert: float = 0.0


@dataclass(frozen=True, slots=True)
class DayPlan:
    day: date
    seed: int
    prev_close: float = 25000.0
    gap_pct: float = 0.0
    segments: tuple[Segment, ...] = (Segment(SESSION_MINUTES),)
    shocks: tuple[tuple[int, float], ...] = ()  # (minute index, % move added to that bar)
    vix_open: float = 13.0
    vix_moves: tuple[tuple[int, float], ...] = ()  # (minute index, VIX points added; the new level persists)
    fut_premium: float = 40.0  # futures basis over the index (points), SYNTHETIC
    fut_volume_base: int = 6000  # futures contracts per minute before the U-shaped intraday profile
    volume_bursts: tuple[tuple[int, int, float], ...] = ()  # (from minute, to minute, multiplier)
    minutes: int = SESSION_MINUTES


@dataclass(frozen=True, slots=True)
class SyntheticDay:
    plan: DayPlan
    index: tuple[Bar, ...]
    fut: tuple[Bar, ...]
    vix: tuple[Bar, ...]
    notes: tuple[str, ...] = field(default=("SYNTHETIC: generated from a seeded plan; not market data",))

    @property
    def close(self) -> Decimal:
        return self.index[-1].close

    def vix_at(self) -> dict[datetime, Decimal]:
        return {b.start: b.close for b in self.vix}


def _volume_profile(m: int, n: int) -> float:
    """U-shaped intraday volume profile (heavier at the open and the close)."""
    x = m / max(1, n - 1)
    return 0.6 + 1.6 * (x - 0.5) ** 2 * 4 / 2


def generate_day(plan: DayPlan) -> SyntheticDay:
    if sum(s.minutes for s in plan.segments) < plan.minutes:
        segs = (*plan.segments, Segment(plan.minutes - sum(s.minutes for s in plan.segments)))
    else:
        segs = plan.segments
    rng = random.Random(f"synthetic|{plan.day.isoformat()}|{plan.seed}")
    shocks = dict(plan.shocks)
    t0 = datetime.combine(plan.day, SESSION_OPEN, IST)
    px = plan.prev_close * (1 + plan.gap_pct / 100)
    vix = plan.vix_open
    vix_anchor = plan.vix_open  # a scripted VIX move shifts the level the noise reverts to
    vix_add = dict(plan.vix_moves)
    idx: list[Bar] = []
    fut: list[Bar] = []
    vixb: list[Bar] = []
    m = 0
    for seg in segs:
        anchor = px
        for _ in range(seg.minutes):
            if m >= plan.minutes:
                break
            start = t0 + timedelta(minutes=m)
            sigma = seg.vol_pct / 100 / math.sqrt(_MIN_PER_YEAR)
            o = px
            path = [o]
            sub = 4
            for _k in range(sub):
                pull = -seg.revert * (path[-1] - anchor) / anchor / sub
                step = seg.drift_pct_per_min / 100 / sub + pull + sigma / math.sqrt(sub) * rng.gauss(0, 1)
                path.append(path[-1] * math.exp(step))
            if m in shocks:
                path[-1] *= 1 + shocks[m] / 100
                path.append(path[-1])
            c = path[-1]
            hi, lo = max(path), min(path)
            idx.append(Bar(INDEX_KEY, start, _q(o), _q(hi), _q(lo), _q(c), 0))
            burst = 1.0
            for a, b, mult in plan.volume_bursts:
                if a <= m < b:
                    burst *= mult
            vol = int(plan.fut_volume_base * _volume_profile(m, plan.minutes) * burst * (0.8 + 0.4 * rng.random()))
            fp = plan.fut_premium
            fut.append(Bar(FUT_KEY, start, _q(o + fp), _q(hi + fp), _q(lo + fp), _q(c + fp), vol))
            v0 = vix
            vix_anchor += vix_add.get(m, 0.0)
            vix = max(8.0, vix + vix_add.get(m, 0.0) + 0.01 * (vix_anchor - vix) + rng.gauss(0, 0.015))
            vixb.append(
                Bar(VIX_KEY, start, _q(v0, Decimal("0.01")), _q(max(v0, vix), Decimal("0.01")),
                    _q(min(v0, vix), Decimal("0.01")), _q(vix, Decimal("0.01")), 0)
            )  # fmt: skip
            px = c
            m += 1
    return SyntheticDay(plan, tuple(idx), tuple(fut), tuple(vixb))


def generate_days(plans: Sequence[DayPlan]) -> list[SyntheticDay]:
    """Generate consecutive days, chaining each day's previous close to the prior day's close."""
    out: list[SyntheticDay] = []
    prev: float | None = None
    for p in plans:
        if prev is not None:
            p = DayPlan(**{**{f: getattr(p, f) for f in p.__dataclass_fields__}, "prev_close": prev})
        d = generate_day(p)
        out.append(d)
        prev = float(d.close)
    return out

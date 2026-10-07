"""Offline one-minute option simulator for the OD-019 round (H42-H47). Research only.

Decisions use bar CLOSES up to the decision bar; fills are at the NEXT minute's open with a fixed tick slippage
(buy + ticks, sell - ticks); stops/targets/time exits are decided on a bar's close and filled at the next open
(pre-registered before any run). Minute m = bar start
minutes after 09:15 (m = 0 is 09:15; the bar m closes at 09:16 + m).
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from project100c.costs import CostModel
from project100c.costs.model import Side
from project100c.errors import CostModelError
from project100c.sessions import IST
from project100c.structures import fill_price

OP, H, L, C, V, IVF = range(6)
PANEL = Path(os.environ.get("P100C_PANEL", "lake/runs/sell/panel"))


def m_of(t: str | time) -> int:
    """Minute index of the bar STARTING at clock time t."""
    tt = time.fromisoformat(t) if isinstance(t, str) else t
    return tt.hour * 60 + tt.minute - (9 * 60 + 15)


def clock(d: date, m: int) -> datetime:
    """Start of bar m on day d (IST)."""
    return datetime.combine(d, time(9, 15), IST) + timedelta(minutes=m)


class Panel:
    """Day access to the monthly panel files (scripts/option_panel.py), two months cached."""

    def __init__(self, root: Path = PANEL) -> None:
        self.files = sorted(root.glob("*.pkl.gz"))
        self.starts = [date.fromisoformat(f.name[:10]) for f in self.files]
        self._cache: dict[int, dict[date, Any]] = {}

    def _load(self, i: int) -> dict[date, Any]:
        import gzip
        import pickle

        if i not in self._cache:
            if len(self._cache) >= 2:
                self._cache.pop(min(self._cache))
            with gzip.open(self.files[i], "rb") as fh:
                self._cache[i] = pickle.load(fh)  # our own research output
        return self._cache[i]

    def days(self) -> list[date]:
        out: list[date] = []
        for i in range(len(self.files)):
            out += sorted(self._load(i))
        self._cache.clear()
        return out

    def get(self, d: date) -> Day | None:
        i = max((k for k, s in enumerate(self.starts) if s <= d), default=None)
        if i is None:
            return None
        raw = self._load(i).get(d)
        return None if raw is None else Day(d, raw)


@dataclass
class Day:
    d: date
    raw: dict[str, Any]
    expiry: date | None = None
    by_key: dict[tuple[float, str], np.ndarray] = field(default_factory=dict)
    lot: int | None = None

    def __post_init__(self) -> None:
        exps = sorted({date.fromisoformat(k[0]) for k in self.raw["opt"]} - {e for e in () if e})
        exps = [e for e in exps if e >= self.d]
        self.expiry = exps[0] if exps else None
        if self.expiry is not None:
            ei = self.expiry.isoformat()
            self.by_key = {(k[1], k[2]): v for k, v in self.raw["opt"].items() if k[0] == ei}
            lots = [n for k, n in self.raw.get("lots", {}).items() if k[0] == ei]
            self.lot = lots[0] if lots else None

    @property
    def idx(self) -> np.ndarray:
        arr: np.ndarray = self.raw["idx"]
        return arr

    @property
    def is_expiry(self) -> bool:
        return self.expiry == self.d

    def spot(self, m: int, f: int = C) -> float | None:
        v = float(self.idx[m, f]) if 0 <= m < 375 else math.nan
        return None if math.isnan(v) else v

    def atm(self, m: int, step: int) -> float | None:
        s = self.spot(m)
        return None if s is None else round(s / step) * step

    def px(self, strike: float, right: str, m: int, f: int = C, back: int = 0) -> float | None:
        a = self.by_key.get((strike, right))
        if a is None:
            return None
        for k in range(m, max(m - back, 0) - 1, -1):
            if 0 <= k < 375:
                v = float(a[k, f])
                if not math.isnan(v):
                    return v
        return None

    def vol_near(self, m: int, step: int) -> float:
        """Summed volume of the CE and PE at the three strikes nearest spot (ATM-1..ATM+1) at bar m."""
        k0 = self.atm(m, step)
        if k0 is None:
            return math.nan
        tot, seen = 0.0, False
        for k in (k0 - step, k0, k0 + step):
            for r in ("CE", "PE"):
                a = self.by_key.get((k, r))
                if a is not None and not math.isnan(float(a[m, V])):
                    tot += float(a[m, V])
                    seen = True
        return tot if seen else math.nan


def rv5_idx(idx: np.ndarray, upto_m: int | None = None) -> float:
    """Open-to-close variance from 5-minute log returns (bars ending on a 5-minute mark), as H19's rv5."""
    last = 374 if upto_m is None else upto_m
    px = [float(idx[0, OP])]
    px += [float(idx[m, C]) for m in range(last + 1) if (m + 1) % 5 == 0 and not math.isnan(float(idx[m, C]))]
    return sum(math.log(b / a) ** 2 for a, b in pairwise(px) if a > 0 and b > 0)


class Costs:
    def __init__(self, model: CostModel, plan: str, tick: float) -> None:
        self.model, self.plan, self.tick = model, plan, tick

    @lru_cache(maxsize=200_000)  # noqa: B019 - one instance per run
    def order(self, side: str, price: float, qty: int, d: date) -> float:
        p = Decimal(str(round(max(price, self.tick), 2)))
        return float(self.model.order_charges(Side(side), p, qty, d, self.plan).total)

    def exercise(self, intrinsic: float, qty: int, d: date) -> float:
        if intrinsic <= 0:
            return 0.0
        try:
            return float(self.model.exercise_charges(Decimal(str(round(intrinsic, 2))), qty, d, self.plan).total)
        except CostModelError:
            # The plan's exercise brokerage is unknown (UNVERIFIED). Settlement view is descriptive only: STT on the
            # settlement value from the dated schedule, plus an ASSUMED Rs 20 + 18% GST exercise fee.
            sched = self.model.schedule(d)
            return float(sched.stt_exercise_rate) * intrinsic * qty + 20.0 * 1.18


def fill(price: float, buy: bool, ticks: int, tick: float, half_spread: float = 0.0) -> float:
    """Next-open fill with slippage ticks and (where a run declares it) the synthetic half-spread
    (``project100c.structures.fills``). SR-2026-10-03.1 ran with half_spread = 0 and is not re-scored."""
    return fill_price(price, buy, ticks, tick, half_spread)


@dataclass
class LegFill:
    sign: int  # +1 long, -1 short
    right: str
    strike: float
    entry: float
    exit: float = math.nan
    stale_exit: bool = False


def iter_bars(days: list[Day], m0: int, end_m: int) -> Iterator[tuple[int, Day, int]]:
    """(day index, day, m) from (days[0], m0) through (days[-1], end_m - 1): bars to check before the exit bar."""
    for i, dy in enumerate(days):
        a = m0 if i == 0 else 0
        b = end_m if i == len(days) - 1 else 375
        for m in range(a, b):
            yield i, dy, m

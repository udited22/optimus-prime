"""Minimum viable capital (MVC) and a per-candidate economic scoreboard (research plane only).

A strategy is researched at the capital it needs, not at the live canary's. MVC is the smallest NAV at which ONE lot
of the strategy fits every capital constraint at once; the binding constraint is reported with it:

* ``cash``: the cash or margin one lot ties up, plus a buffer (premium for a bought option, the exchange margin for a
  short or hedged structure);
* ``per_trade``: the worst per-lot loss of one trade (a defined max loss, or the worst observed loss when there is no
  stop) must be at most ``risk_frac`` of NAV;
* ``stress``: the worst per-lot loss under the stress scenario (an overnight gap, an IV shock) must be at most
  ``stress_frac`` of NAV;
* ``drawdown``: the per-lot maximum drawdown of the trade history, inflated by ``dd_multiplier`` (a backtest drawdown
  understates the future one, and more so for a best-of-N selection), must be at most ``dd_budget`` of NAV.

Nothing here can place an order or change a live limit: it reads trade histories and prints numbers.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class CapitalPolicy:
    risk_frac: float = 0.02  # The owner rule today (max loss of one trade <= 2% of NAV)
    stress_frac: float = 0.10  # one stress event costs at most 10% of NAV
    dd_budget: float = 0.20  # the drawdown the book may plausibly see
    dd_multiplier: float = 1.5  # forward drawdown vs backtest drawdown
    cash_buffer: float = 0.5  # cash/margin x (1 + buffer): margin calls, intraday marks, a second position


@dataclass(frozen=True)
class CapitalInputs:
    cash_per_lot: float
    worst_trade_loss_per_lot: float
    stress_loss_per_lot: float | None
    max_drawdown_per_lot: float


@dataclass(frozen=True)
class MVC:
    capital: float
    binding: str
    components: dict[str, float] = field(default_factory=dict)


DEFAULT_POLICY = CapitalPolicy()


def minimum_viable_capital(x: CapitalInputs, p: CapitalPolicy = DEFAULT_POLICY) -> MVC:
    comp = {
        "cash": x.cash_per_lot * (1 + p.cash_buffer),
        "per_trade": x.worst_trade_loss_per_lot / p.risk_frac,
        "drawdown": x.max_drawdown_per_lot * p.dd_multiplier / p.dd_budget,
    }
    if x.stress_loss_per_lot is not None:
        comp["stress"] = x.stress_loss_per_lot / p.stress_frac
    binding = max(comp, key=lambda k: comp[k])
    return MVC(round(comp[binding], 0), binding, {k: round(v, 0) for k, v in comp.items()})


@dataclass(frozen=True)
class TradeStats:
    n: int
    first: date
    last: date
    years: float
    trades_per_year: float
    mean: float
    ci: tuple[float, float] | None
    hit_rate: float
    profit_factor: float | None
    worst_trade: float
    max_drawdown: float
    annual_net: float
    positive_month_share: float


def max_drawdown(pnls: Sequence[float]) -> float:
    peak = cum = dd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def bootstrap_mean_ci(
    xs: Sequence[float], level: float = 0.90, iters: int = 4000, seed: int = 7
) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(iters))
    return means[int((1 - level) / 2 * iters)], means[min(iters - 1, int((1 + level) / 2 * iters))]


def trade_stats(trades: Sequence[tuple[date, float]], level: float = 0.90) -> TradeStats:
    """Per-lot statistics of a trade history [(day, net P&L per lot after costs)], in day order."""
    if not trades:
        raise ValueError("no trades")
    ts = sorted(trades)
    xs = [p for _, p in ts]
    first, last = ts[0][0], ts[-1][0]
    years = max((last - first).days / 365.25, 1 / 12)
    wins, losses = sum(x for x in xs if x > 0), -sum(x for x in xs if x < 0)
    months: dict[str, float] = {}
    for d, p in ts:
        months[d.strftime("%Y-%m")] = months.get(d.strftime("%Y-%m"), 0.0) + p
    return TradeStats(
        n=len(xs),
        first=first,
        last=last,
        years=round(years, 2),
        trades_per_year=round(len(xs) / years, 1),
        mean=statistics.fmean(xs),
        ci=bootstrap_mean_ci(xs, level) if len(xs) >= 2 else None,
        hit_rate=sum(1 for x in xs if x > 0) / len(xs),
        profit_factor=None if losses == 0 else wins / losses,
        worst_trade=min(xs),
        max_drawdown=max_drawdown(xs),
        annual_net=sum(xs) / years,
        positive_month_share=sum(1 for v in months.values() if v > 0) / len(months),
    )


def suggest_decision(s: TradeStats, min_trades: int = 100) -> str:
    """A mechanical first read (the owner decides): KILL when the CI is below 0, ITERATE/DEFER otherwise."""
    if s.ci is None or s.n < 20:
        return "DEFER (sample too small)"
    if s.ci[1] < 0:
        return "KILL (CI below 0)"
    if s.ci[0] > 0 and s.n >= min_trades:
        return "PROMOTE to forward/shadow (CI above 0)"
    if s.ci[0] > 0:
        return "ITERATE (CI above 0, sample short)"
    return "FORWARD-TEST only (CI spans 0: no edge shown yet)"


def fmt_inr(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"₹{x:,.0f}" if x >= 0 else f"-₹{-x:,.0f}"

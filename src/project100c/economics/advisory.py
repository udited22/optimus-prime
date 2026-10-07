"""Cost-justification check (OD-012): is the setup earning enough to justify what it costs to run?

ADVISORY ONLY. This module never stops, blocks or limits trading: it has no import path to the Risk Governor, the
kill switches, the runtime or a broker (a test enforces this), and every Advisory carries blocks_trading=False.
Its output is shown in the owner's daily/monthly report and on the dashboard, with optimisation suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from project100c.economics.config import EconomicsConfig, JustificationRule
from project100c.economics.fmt import inr
from project100c.economics.model import MonthEconomics, Statement, nav_for_fixed_cost_threshold
from project100c.errors import EconomicsError


class JustificationStatus(StrEnum):
    OK = "OK"  # at least one month in the window met the threshold
    BELOW_THRESHOLD = "BELOW_THRESHOLD"  # every month in the window was below: advisory raised
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # fewer months than the window


@dataclass(frozen=True, slots=True)
class Advisory:
    status: JustificationStatus
    threshold: Decimal  # min monthly net return on NAV after all costs
    threshold_status: str  # ASSUMED / VERIFIED label of the threshold itself
    window_months: int
    months: tuple[str, ...]
    monthly_net_returns: tuple[Decimal, ...]
    fixed_cost_frac: Decimal  # latest month: fixed costs / opening NAV
    fixed_cost_threshold: Decimal
    structural: bool  # fixed costs alone exceed the fixed-cost threshold at the current NAV
    nav_for_fixed_threshold: Decimal
    headline: str
    recommendations: tuple[str, ...]
    simulated: bool
    blocks_trading: bool = False

    def __post_init__(self) -> None:
        if self.blocks_trading:
            raise EconomicsError("the cost-justification advisory can never block trading (OD-012)")

    @property
    def raised(self) -> bool:
        return self.status is JustificationStatus.BELOW_THRESHOLD or self.structural


def _pct(x: Decimal, dp: int = 2) -> str:
    return f"{x * 100:.{dp}f}%"


def _inr(x: Decimal) -> str:
    return inr(x, 0)


def _trading_drag(m: MonthEconomics) -> Decimal | None:
    if m.trading_charges == 0:
        return None
    return m.trading_charges / m.gross_pnl if m.gross_pnl > 0 else Decimal(1)


def cost_justification(statement: Statement, cfg: EconomicsConfig) -> Advisory:
    cj = cfg.cost_justification
    if cj.rule is not JustificationRule.ALL_MONTHS_BELOW:  # pragma: no cover - the config model allows only this
        raise EconomicsError(f"unsupported rule {cj.rule}")
    window = statement.months[-cj.window_months :]
    rets = tuple(m.net_return_on_nav for m in window)
    latest = statement.latest
    if len(window) < cj.window_months:
        status = JustificationStatus.INSUFFICIENT_DATA
    elif all(r < cj.min_net_return_over_costs for r in rets):
        status = JustificationStatus.BELOW_THRESHOLD
    else:
        status = JustificationStatus.OK
    ffrac = latest.fixed_cost_frac
    structural = ffrac > cj.fixed_cost_nav_threshold
    nav_needed = nav_for_fixed_cost_threshold(latest.fixed_total, cj.fixed_cost_nav_threshold)

    recs: list[str] = []
    if status is JustificationStatus.BELOW_THRESHOLD or structural:
        if structural:
            recs.append(
                f"Fixed running costs are {_inr(latest.fixed_total)} a month, {_pct(ffrac)} of NAV "
                f"({_inr(latest.opening_nav)}). They fall below {_pct(cj.fixed_cost_nav_threshold, 1)} of NAV only "
                f"at NAV ≥ {_inr(nav_needed)}; until then the setup cannot pay for itself."
            )
        for x in sorted((x for x in latest.fixed if x.enabled), key=lambda x: -x.total_inr):
            if x.total_inr > 0 or x.brokerage_plan_if_enabled:
                recs.append(f"{x.label} ({_inr(x.total_inr)}/month, {x.status}): {x.optimisation}")
        drags = [(m.month, d) for m in window if (d := _trading_drag(m)) is not None]
        heavy = [(mo, d) for mo, d in drags if d >= cj.trading_charge_drag_threshold]
        if heavy:
            worst = max(heavy, key=lambda t: t[1])
            recs.append(
                f"Trading charges took {_pct(worst[1], 0)} of gross P&L in {worst[0]}: fewer, higher-conviction "
                "trades (each round trip costs two brokerage fees plus STT and exchange charges)."
            )
    recs.append("Advisory only: this check never stops, blocks or limits trading (it is not a kill switch).")

    if status is JustificationStatus.BELOW_THRESHOLD:
        headline = (
            f"ADVISORY: net return after all costs stayed below {_pct(cj.min_net_return_over_costs)}/month "
            f"for {cj.window_months} months"
        )
    elif structural:
        headline = (
            f"ADVISORY: fixed costs are {_pct(ffrac, 1)} of NAV a month "
            f"(> {_pct(cj.fixed_cost_nav_threshold, 1)}); justified from NAV ≥ {_inr(nav_needed)}"
        )
    elif status is JustificationStatus.INSUFFICIENT_DATA:
        headline = f"Cost justification: {len(window)} of {cj.window_months} months of data so far"
    else:
        headline = f"Cost justification OK over the last {cj.window_months} months"
    return Advisory(
        status,
        cj.min_net_return_over_costs,
        str(cj.min_net_return_status),
        cj.window_months,
        tuple(m.month for m in window),
        rets,
        ffrac,
        cj.fixed_cost_nav_threshold,
        structural,
        nav_needed,
        headline,
        tuple(recs),
        statement.simulated,
    )

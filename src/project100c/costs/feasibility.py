"""Feasibility arithmetic built on the cost model (reproduces docs/architecture/architecture.md §A2-A3 exactly).

These are *design* checks, not the Risk Governor. The Governor (K-02) will use the same CostModel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from project100c.costs.model import CostModel, Number, to_decimal
from project100c.errors import CostModelError


def loss_at_stop(
    model: CostModel,
    *,
    entry: Number,
    stop_exit: Number,
    lot_size: int,
    slippage_per_side: Number,
    trade_date: date,
    plan_id: str,
) -> Decimal:
    """Rupee loss of one long lot stopped out: premium loss + round-trip charges + 2 sides of slippage."""
    e = to_decimal(entry, "entry")
    x = to_decimal(stop_exit, "stop_exit")
    slip = to_decimal(slippage_per_side, "slippage_per_side")
    if x >= e:
        raise CostModelError("stop_exit must be below entry for a long option")
    if slip < 0:
        raise CostModelError("slippage must be >= 0")
    charges = model.round_trip(e, x, lot_size, trade_date, plan_id).total
    return (e - x) * lot_size + charges + 2 * slip * lot_size


def stop_room_points(
    model: CostModel,
    *,
    nav: Number,
    max_loss_fraction: Number,
    premium: Number,
    lot_size: int,
    slippage_per_side: Number,
    trade_date: date,
    plan_id: str,
) -> Decimal:
    """Premium points of stop left after charges and slippage within the per-trade loss budget.

    Uses the design-pack convention: charges computed as buy and sell at the same premium.
    A negative result means the budget is exhausted before any stop room exists.
    """
    budget = to_decimal(nav, "nav") * to_decimal(max_loss_fraction, "max_loss_fraction")
    p = to_decimal(premium, "premium")
    slip = to_decimal(slippage_per_side, "slippage_per_side")
    charges = model.round_trip(p, p, lot_size, trade_date, plan_id).total
    return (budget - charges - 2 * slip * lot_size) / lot_size


@dataclass(frozen=True, slots=True)
class MinNavResult:
    premium: Decimal
    stop_points: Decimal
    loss_at_stop: Decimal
    nav_by_loss_rule: Decimal
    nav_by_friction_rule: Decimal
    nav_by_outlay_rule: Decimal

    @property
    def binding_min_nav(self) -> Decimal:
        return max(self.nav_by_loss_rule, self.nav_by_friction_rule, self.nav_by_outlay_rule)


def min_nav_for_premium(
    model: CostModel,
    *,
    premium: Number,
    lot_size: int,
    trade_date: date,
    plan_id: str,
    stop_fraction: Number = "0.25",
    slippage_per_side: Number = "0.5",
    max_loss_fraction: Number = "0.02",
    friction_share_of_budget: Number = "0.25",
    outlay_share_of_nav: Number = "0.25",
) -> MinNavResult:
    """Minimum NAV for one lot under the 01 §A3 criteria (all three must hold; the max binds)."""
    p = to_decimal(premium, "premium")
    sf = to_decimal(stop_fraction, "stop_fraction")
    slip = to_decimal(slippage_per_side, "slippage_per_side")
    mlf = to_decimal(max_loss_fraction, "max_loss_fraction")
    fsb = to_decimal(friction_share_of_budget, "friction_share_of_budget")
    osn = to_decimal(outlay_share_of_nav, "outlay_share_of_nav")
    for name, v in (("stop_fraction", sf), ("max_loss_fraction", mlf), ("friction_share", fsb), ("outlay", osn)):
        if not (Decimal(0) < v <= Decimal(1)):
            raise CostModelError(f"{name}={v} must be in (0, 1]")
    stop = sf * p
    friction = model.round_trip(p, p - stop, lot_size, trade_date, plan_id).total + 2 * slip * lot_size
    loss = stop * lot_size + friction
    return MinNavResult(
        premium=p,
        stop_points=stop,
        loss_at_stop=loss,
        nav_by_loss_rule=loss / mlf,
        nav_by_friction_rule=friction / (fsb * mlf),
        nav_by_outlay_rule=p * lot_size / osn,
    )

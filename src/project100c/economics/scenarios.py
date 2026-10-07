"""The worked example (docs/risk/system-economics.md §19.4): what the fixed running costs mean at different NAV levels.

No trading results are involved; these are planning numbers from the versioned config. Every scenario lists the
ASSUMED inputs it depends on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from project100c.economics.config import EconomicsConfig
from project100c.economics.model import (
    FixedCostMonthly,
    fixed_cost_frac,
    fixed_total,
    monthly_fixed_costs,
    nav_for_fixed_cost_threshold,
    required_gross_monthly_return,
)

DEFAULT_NAVS: tuple[Decimal, ...] = tuple(
    Decimal(x) for x in ("10000", "25000", "50000", "100000", "200000", "500000", "1000000", "2500000")
)


@dataclass(frozen=True, slots=True)
class NavRow:
    nav: Decimal
    fixed_monthly: Decimal
    fixed_frac: Decimal  # = break-even gross monthly return for fixed costs alone
    break_even_annualised: Decimal  # (1 + monthly)^12 - 1
    required_gross_for_target: Decimal  # gross monthly return for the min-net target after fixed costs and tax
    justified: bool  # fixed_frac <= fixed-cost threshold


@dataclass(frozen=True, slots=True)
class CostScenario:
    name: str
    lines: tuple[FixedCostMonthly, ...]
    fixed_monthly: Decimal
    nav_for_threshold: Decimal
    rows: tuple[NavRow, ...]


def nav_ladder(
    cfg: EconomicsConfig, lines: Sequence[FixedCostMonthly], navs: Sequence[Decimal] = DEFAULT_NAVS
) -> tuple[NavRow, ...]:
    fx = fixed_total(lines)
    cj = cfg.cost_justification
    rows = []
    for nav in navs:
        frac = fixed_cost_frac(fx, nav)
        rows.append(
            NavRow(
                nav,
                fx,
                frac,
                (1 + frac) ** 12 - 1,
                required_gross_monthly_return(
                    nav, fx, Decimal(0), cj.min_net_return_over_costs, cfg.tax.effective_rate
                ),
                frac <= cj.fixed_cost_nav_threshold,
            )
        )
    return tuple(rows)


def scenario(
    cfg: EconomicsConfig,
    name: str,
    *,
    amounts: Mapping[str, Decimal] | None = None,
    enable: Sequence[str] = (),
    navs: Sequence[Decimal] = DEFAULT_NAVS,
) -> CostScenario:
    lines = monthly_fixed_costs(cfg, amounts=amounts, enable=enable)
    fx = fixed_total(lines)
    return CostScenario(
        name,
        lines,
        fx,
        nav_for_fixed_cost_threshold(fx, cfg.cost_justification.fixed_cost_nav_threshold),
        nav_ladder(cfg, lines, navs),
    )


def standard_scenarios(cfg: EconomicsConfig) -> tuple[CostScenario, ...]:
    """Base case (configured), server at the high end of its ASSUMED range, and high end plus Rs-equivalent
    USD 10/month of LLM/API spend. Uses the configured server range."""
    srv = cfg.line("cloud-server-mumbai")
    hi = srv.range_high if srv.range_high is not None else srv.amount
    return (
        scenario(cfg, f"Base: Dhan + server USD {srv.amount} (ASSUMED)"),
        scenario(cfg, f"Server USD {hi} (ASSUMED high end)", amounts={"cloud-server-mumbai": hi}),
        scenario(
            cfg,
            f"Server USD {hi} + LLM/API USD 10 (ASSUMED)",
            amounts={"cloud-server-mumbai": hi, "llm-api": Decimal(10)},
        ),
    )

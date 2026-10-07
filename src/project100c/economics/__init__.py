"""Whole-system economics (docs/risk/system-economics.md, OD-012): returns net of brokerage, statutory charges, fixed
infrastructure
and income tax, plus an ADVISORY-ONLY cost-justification check. Nothing here can stop or limit trading."""

from project100c.economics.advisory import Advisory, JustificationStatus, cost_justification
from project100c.economics.config import EconomicsConfig, FixedCostLine, Status, load_economics_config
from project100c.economics.journal import trading_days_from_journal
from project100c.economics.model import (
    ChargeComponents,
    FixedCostMonthly,
    MonthEconomics,
    Statement,
    TradingDay,
    build_statement,
    fixed_cost_frac,
    fixed_total,
    monthly_fixed_costs,
    nav_for_fixed_cost_threshold,
    required_gross_monthly_return,
)
from project100c.economics.report import render_markdown, snapshot
from project100c.economics.scenarios import CostScenario, NavRow, nav_ladder, scenario, standard_scenarios

__all__ = [
    "Advisory",
    "ChargeComponents",
    "CostScenario",
    "EconomicsConfig",
    "FixedCostLine",
    "FixedCostMonthly",
    "JustificationStatus",
    "MonthEconomics",
    "NavRow",
    "Statement",
    "Status",
    "TradingDay",
    "build_statement",
    "cost_justification",
    "fixed_cost_frac",
    "fixed_total",
    "load_economics_config",
    "monthly_fixed_costs",
    "nav_for_fixed_cost_threshold",
    "nav_ladder",
    "render_markdown",
    "required_gross_monthly_return",
    "scenario",
    "snapshot",
    "standard_scenarios",
    "trading_days_from_journal",
]

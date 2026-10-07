"""Print the whole-system economics worked example (docs/risk/system-economics.md §19.4) from
configs/economics/economics.toml.

    .venv/bin/python scripts/economics_report.py

Fixed monthly running costs per scenario and, for a ladder of NAVs, fixed costs as % of NAV (= break-even gross
monthly return), its annualised equivalent, and the gross monthly return needed for the configured minimum
net return after fixed costs and the ASSUMED income-tax rate. No trading data is read.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from project100c.economics import load_economics_config, standard_scenarios
from project100c.economics.fmt import inr

ROOT = Path(__file__).resolve().parents[1]


def _pct(x: Decimal, dp: int = 2) -> str:
    return f"{x * 100:.{dp}f}%"


def main() -> None:
    cfg = load_economics_config(ROOT / "configs" / "economics" / "economics.toml")
    cj = cfg.cost_justification
    print(f"# Whole-system economics worked example ({cfg.config_version}; USD/INR {cfg.fx.usd_inr} ASSUMED)")
    print(
        f"Target: net {_pct(cj.min_net_return_over_costs)}/month after all costs and tax at "
        f"{_pct(cfg.tax.effective_rate, 1)} (ASSUMED); fixed costs justified below "
        f"{_pct(cj.fixed_cost_nav_threshold, 1)} of NAV a month. Advisory only."
    )
    for sc in standard_scenarios(cfg):
        print(f"\n## {sc.name}: {inr(sc.fixed_monthly)}/month; fixed < 1% of NAV from {inr(sc.nav_for_threshold, 0)}")
        for x in sc.lines:
            if x.enabled:
                print(f"- {x.label} [{x.status}]: {inr(x.total_inr)}")
        print("\n| NAV | Fixed % of NAV/month | Annualised | Gross/month for target | Justified |")
        print("|---:|---:|---:|---:|:---:|")
        for r in sc.rows:
            print(
                f"| {inr(r.nav, 0)} | {_pct(r.fixed_frac)} | {_pct(r.break_even_annualised, 0)} | "
                f"{_pct(r.required_gross_for_target)} | {'yes' if r.justified else 'no'} |"
            )


if __name__ == "__main__":
    main()

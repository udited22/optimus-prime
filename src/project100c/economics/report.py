"""Rendering for the owner's daily/monthly report (K-14) and a JSON-able snapshot for the dashboard.

The report section is self-contained markdown so the K-14 report generator can include it verbatim. Labels
(SIMULATED, ASSUMED, UNVERIFIED) are always printed next to the numbers they qualify.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from project100c.economics.advisory import Advisory
from project100c.economics.fmt import inr
from project100c.economics.model import MonthEconomics, Statement

_P = Decimal("0.01")


def _r(x: Decimal) -> Decimal:
    return x.quantize(_P, rounding=ROUND_HALF_UP)


def _inr(x: Decimal) -> str:
    return inr(x)


def _pct(x: Decimal | None, dp: int = 2) -> str:
    return "n/a (gross ≤ 0)" if x is None else f"{x * 100:.{dp}f}%"


def month_table(m: MonthEconomics) -> list[str]:
    rows = [
        ("Gross P&L (before any charge)", _inr(m.gross_pnl)),
        ("- Brokerage", _inr(m.charges.brokerage)),
        ("- Statutory and exchange (STT, txn, SEBI, stamp, GST)", _inr(m.charges.statutory)),
        *((f"- {x.label} [{x.status}]", _inr(x.total_inr)) for x in m.fixed if x.enabled),
        ("= Pre-tax net", _inr(m.pre_tax_net)),
        ("- Income tax accrual [ASSUMED; OD-017]", _inr(m.tax)),
        ("= **Net after everything**", f"**{_inr(m.net_after_all)}**"),
        ("Net return on opening NAV", f"**{_pct(m.net_return_on_nav)}**"),
        ("Cost drag (total costs ÷ gross P&L)", _pct(m.cost_drag, 1)),
        ("Break-even gross monthly return (fixed costs ÷ NAV)", _pct(m.break_even_gross_return)),
    ]
    out = [
        f"**{m.month}** · opening NAV {_inr(m.opening_nav)} · {m.trading_days} trading days · "
        f"{m.executed_orders} executed orders · {m.round_trips} round trips",
        "",
        "| Line | ₹ |",
        "|---|---:|",
    ]
    out += [f"| {a} | {b} |" for a, b in rows]
    return out


def render_markdown(statement: Statement, advisory: Advisory, *, months: int = 1) -> str:
    sim = " (SIMULATED)" if statement.simulated else ""
    lines = [f"### Economics: net of everything{sim}", ""]
    if statement.labels:
        lines += ["Labels: " + "; ".join(statement.labels) + ".", ""]
    for m in statement.months[-months:]:
        lines += [*month_table(m), ""]
    flag = "⚠ " if advisory.raised else ""
    lines += [f"**{flag}{advisory.headline}** (status {advisory.status}; threshold {advisory.threshold_status})", ""]
    lines += [f"- {r}" for r in advisory.recommendations]
    lines += ["", *[f"> {n}" for n in statement.notes]]
    return "\n".join(lines) + "\n"


def snapshot(statement: Statement, advisory: Advisory) -> dict[str, Any]:
    """Compact JSON-able view of the latest month for the dashboard (Decimals as strings, 2 dp)."""
    m = statement.latest
    return {
        "month": m.month,
        "simulated": statement.simulated,
        "opening_nav": str(_r(m.opening_nav)),
        "gross": str(_r(m.gross_pnl)),
        "brokerage": str(_r(m.charges.brokerage)),
        "statutory": str(_r(m.charges.statutory)),
        "fixed_monthly": str(_r(m.fixed_total)),
        "fixed_lines": [
            {"id": x.line_id, "label": x.label, "inr": str(_r(x.total_inr)), "status": str(x.status)}
            for x in m.fixed
            if x.enabled
        ],
        "tax": str(_r(m.tax)),
        "net": str(_r(m.net_after_all)),
        "net_return": str(m.net_return_on_nav.quantize(Decimal("0.0001"))),
        "cost_drag": None if m.cost_drag is None else str(m.cost_drag.quantize(Decimal("0.0001"))),
        "fixed_frac": str(m.fixed_cost_frac.quantize(Decimal("0.0001"))),
        "break_even": str(m.break_even_gross_return.quantize(Decimal("0.0001"))),
        "nav_for_fixed_threshold": str(advisory.nav_for_fixed_threshold.quantize(Decimal(1))),
        "fixed_threshold": str(advisory.fixed_cost_threshold),
        "status": str(advisory.status),
        "raised": advisory.raised,
        "headline": advisory.headline,
        "recommendations": list(advisory.recommendations),
        "labels": list(statement.labels),
        "advisory_only": True,
        "blocks_trading": advisory.blocks_trading,
    }

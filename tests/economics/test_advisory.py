"""OD-012: the cost-justification check is advisory only, with optimisation suggestions; it never blocks trading."""

from __future__ import annotations

import ast
import dataclasses
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.economics import (
    Advisory,
    ChargeComponents,
    EconomicsConfig,
    JustificationStatus,
    TradingDay,
    build_statement,
    cost_justification,
    render_markdown,
    snapshot,
    standard_scenarios,
)
from project100c.errors import EconomicsError

SRC = Path(__file__).resolve().parents[2] / "src" / "project100c"


def td(d: date, gross: str, nav: str, brokerage: str = "40", sim: bool = True) -> TradingDay:
    return TradingDay(d, D(nav), D(gross), ChargeComponents(brokerage=D(brokerage)), 2, 1, abs(D(gross)), simulated=sim)


def test_canary_at_10k_is_structurally_unjustified_but_never_blocked(cfg: EconomicsConfig) -> None:
    st = build_statement([td(date(2026, 10, 5), "300", "10000")], cfg)
    a = cost_justification(st, cfg)
    assert a.status is JustificationStatus.INSUFFICIENT_DATA and a.structural and a.raised
    assert a.blocks_trading is False and a.simulated
    assert a.nav_for_fixed_threshold.quantize(D(1)) == D(116381)
    assert "ADVISORY" in a.headline and "₹1,16,381" in a.headline
    assert any("Fixed running costs" in r for r in a.recommendations)
    assert any(r.startswith("Dhan") or "Dhan" in r for r in a.recommendations)
    assert a.recommendations[-1].startswith("Advisory only") and "not a kill switch" in a.recommendations[-1]


def test_three_months_below_threshold_raise_the_advisory(cfg: EconomicsConfig) -> None:
    days = [td(date(2026, m, 5), "2000", "500000", brokerage="1500") for m in (10, 11, 12)]
    a = cost_justification(build_statement(days, cfg), cfg)
    assert not a.structural  # fixed costs are 0.23% of Rs 5 lakh
    assert a.status is JustificationStatus.BELOW_THRESHOLD and a.raised
    assert a.months == ("2026-10", "2026-11", "2026-12") and all(r < D("0.005") for r in a.monthly_net_returns)
    assert any("Trading charges took" in r for r in a.recommendations)  # 1500 / 2000 = 75% >= 50%


def test_one_good_month_in_the_window_is_ok(cfg: EconomicsConfig) -> None:
    days = [td(date(2026, 10, 5), "2000", "500000"), td(date(2026, 11, 5), "20000", "500000")]
    days.append(td(date(2026, 12, 5), "2000", "500000"))
    a = cost_justification(build_statement(days, cfg), cfg)
    assert a.status is JustificationStatus.OK and not a.raised
    assert a.headline.startswith("Cost justification OK")
    assert a.recommendations == (
        "Advisory only: this check never stops, blocks or limits trading (it is not a kill switch).",
    )


def test_an_advisory_that_blocks_trading_cannot_exist(cfg: EconomicsConfig) -> None:
    a = cost_justification(build_statement([td(date(2026, 10, 5), "1", "10000")], cfg), cfg)
    with pytest.raises(EconomicsError, match="never block"):
        dataclasses.replace(a, blocks_trading=True)
    assert isinstance(a, Advisory) and a.blocks_trading is False


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_economics_has_no_path_to_the_kernel_or_a_broker_and_vice_versa() -> None:
    banned = ("project100c.kernel", "project100c.broker", "project100c.observability", "project100c.agents")
    for f in (SRC / "economics").rglob("*.py"):
        bad = {m for m in _imports(f) if m.startswith(banned)}
        assert not bad, f"{f.name} imports {bad}"
    for pkg in ("kernel", "broker"):
        for f in (SRC / pkg).rglob("*.py"):
            assert not any(m.startswith("project100c.economics") for m in _imports(f)), f


def test_report_section_and_snapshot_carry_the_labels(cfg: EconomicsConfig) -> None:
    st = build_statement([td(date(2026, 10, 5), "300", "10000")], cfg)
    a = cost_justification(st, cfg)
    md = render_markdown(st, a)
    assert md.startswith("### Economics: net of everything (SIMULATED)")
    for s in (
        "ASSUMED",
        "ASSUMED; OD-017",
        "Net after everything",
        "not a kill switch",
        "₹300.00",
        "| - Brokerage | ₹40.00 |",
    ):
        assert s in md, s
    assert "₹597.41" in md and "₹1,16,381" in md
    snap = snapshot(st, a)
    assert snap["simulated"] is True and snap["advisory_only"] is True and snap["blocks_trading"] is False
    assert snap["fixed_monthly"] == "1163.81" and snap["gross"] == "300.00" and snap["fixed_frac"] == "0.1164"
    assert snap["status"] == "INSUFFICIENT_DATA" and snap["raised"] is True
    assert {x["id"] for x in snap["fixed_lines"]} == {"dhan-data-api", "cloud-server-mumbai", "llm-api"}
    assert snap["cost_drag"] == "4.0127"  # (40 + 1,163.81 fixed + 0 tax) / 300 gross
    assert snap["nav_for_fixed_threshold"] == "116381"


def test_standard_scenarios_worked_example(cfg: EconomicsConfig) -> None:
    base, hi, llm = standard_scenarios(cfg)
    assert [s.fixed_monthly.quantize(D("0.01")) for s in (base, hi, llm)] == [
        D("1163.81"),
        D("2863.01"),
        D("3995.81"),
    ]
    assert [s.nav_for_threshold.quantize(D(1)) for s in (base, hi, llm)] == [D(116381), D(286301), D(399581)]
    rows = {r.nav: r for r in base.rows}
    assert rows[D(10000)].fixed_frac.quantize(D("0.0001")) == D("0.1164") and not rows[D(10000)].justified
    assert rows[D(10000)].required_gross_for_target.quantize(D("0.0001")) == D("0.1236")
    assert rows[D(200000)].justified and not rows[D(100000)].justified
    assert rows[D(10000)].break_even_annualised > D("2.7")


@pytest.mark.parametrize(
    ("x", "dp", "want"),
    [
        ("0", 2, "₹0.00"),
        ("999.995", 2, "₹1,000.00"),
        ("116381.4", 0, "₹1,16,381"),
        ("-12345678.9", 2, "-₹1,23,45,678.90"),
        ("2500000", 0, "₹25,00,000"),
    ],
)
def test_rupees_use_indian_grouping(x: str, dp: int, want: str) -> None:
    from project100c.economics.fmt import inr

    assert inr(D(x), dp) == want

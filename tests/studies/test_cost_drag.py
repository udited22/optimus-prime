"""Cost-drag study (SIMULATED, zero-edge baseline): the mechanics and the break-even arithmetic, on a tiny grid."""

from __future__ import annotations

import runpy
from datetime import date, time
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from project100c.studies.cost_drag import (
    LABELS,
    CellResult,
    Economics,
    StudyConfig,
    day_plan,
    load_economics,
    needed_gross_per_trade,
    per_trade_frac,
    recommend,
    render_markdown,
    required_win_rate,
    run_study,
    slot_times,
    zero_edge_spec,
)
from tests.data.dhan_fakes import CONFIGS, REPO

SCRIPT = REPO / "scripts" / "cost_drag_study.py"
NAVS = (Decimal(10_000), Decimal(100_000), Decimal(200_000))


def tiny() -> StudyConfig:
    return StudyConfig(CONFIGS, REPO / "specs", navs=NAVS, ks=(1, 10), seeds=(1,), start=date(2026, 11, 2), sessions=1)


@cache
def cells() -> tuple[CellResult, ...]:
    return tuple(run_study(tiny()))


def cell(nav: int, k: int) -> CellResult:
    return next(c for c in cells() if c.nav == nav and c.k == k)


def test_slots_are_evenly_spaced_inside_the_entry_window() -> None:
    assert slot_times(1) == (time(11, 40),)
    s = slot_times(10)
    assert len(s) == 10 and s[0] >= time(9, 25) and s[-1] <= time(13, 55) and list(s) == sorted(set(s))
    with pytest.raises(ValueError):
        slot_times(0)


def test_sizing_fits_k_stops_inside_the_daily_risk_budget_and_never_above_2pct() -> None:
    assert per_trade_frac(1) == Decimal("0.02") and per_trade_frac(2) == Decimal("0.02")
    assert per_trade_frac(10) == Decimal("0.004") and per_trade_frac(5) == Decimal("0.008")


def test_the_study_spec_is_a_valid_strategyspec_and_the_market_is_seeded() -> None:
    s = zero_edge_spec(REPO / "specs", 10, 3)
    assert s.entry.max_entries_per_day == 10 and s.exit.stop.value == 30 and s.exit.max_holding_minutes == 20
    assert day_plan(date(2026, 11, 2), 1) == day_plan(date(2026, 11, 2), 1) != day_plan(date(2026, 11, 2), 2)


def test_a_tiny_grid_runs_through_the_real_stack() -> None:
    # at 10k nothing fits a 1-lot budget of 200 (or 40 at 10 a day): no trades, no charges
    for k in (1, 10):
        c = cell(10_000, k)
        assert c.round_trips == 0 and c.charges == 0 and c.mean_risk is None and c.refusals
    c1, c10 = cell(100_000, 1), cell(100_000, 10)
    assert c1.round_trips > 0 and c10.round_trips > c1.round_trips
    for c in (c1, c10, cell(200_000, 10)):
        assert c.mean_risk is not None and c.mean_risk <= per_trade_frac(c.k) * c.nav  # the Governor's ceiling held
        assert c.per_trip_charges is not None and Decimal(40) <= c.per_trip_charges < Decimal(80)  # Rs 20/order x 2
        assert c.brokerage == 40 * c.round_trips  # flat Rs 20 an order, buy and sell
        assert c.gross_loss_at_stop is not None and c.gross_loss_at_stop > 0
    # more entries a day under the same daily budget: cheaper, further-OTM premiums
    assert c10.mean_premium is not None and c1.mean_premium is not None and c10.mean_premium < c1.mean_premium


def test_break_even_arithmetic() -> None:
    assert required_win_rate(Decimal(0), Decimal(100), Decimal(1)) == Decimal("0.5")
    assert required_win_rate(Decimal(50), Decimal(100), Decimal(2)) == Decimal("0.5")
    with pytest.raises(ValueError):
        required_win_rate(Decimal(1), Decimal(0), Decimal(1))
    nav, tax = Decimal(100_000), Decimal("0.312")
    assert needed_gross_per_trade(nav, Decimal(0), Decimal(0), Decimal(0), Decimal(0), tax) is None
    assert needed_gross_per_trade(nav, Decimal(10), Decimal(500), Decimal(500), Decimal(0), tax) == 100
    # +5% net after 31.2% tax needs 5000 / 0.688 pre-tax on top of costs
    v = needed_gross_per_trade(nav, Decimal(10), Decimal(0), Decimal(0), Decimal("0.05"), tax)
    assert v is not None and abs(v - Decimal(5000) / Decimal("0.688") / 10) < Decimal("0.01")


def test_the_report_is_labelled_and_never_calls_the_pnl_a_forecast() -> None:
    econ = load_economics(CONFIGS)
    md = render_markdown(cells(), tiny(), econ)
    for lab in LABELS:
        assert lab in md
    assert "ZERO-EDGE BASELINE, NOT A FORECAST" in md and "never an expected return" in md
    assert "### NAV ₹1,00,000" in md and "### NAV ₹2,00,000" in md and "scalper (H17)" in md
    assert "| ₹10,000 | 1 |" in md and "Recommendation" in md
    assert recommend(cells(), econ, Decimal(10_000), Decimal("0.02")) is None  # costs swamp the canary NAV
    hi = Economics(Decimal(10**9), econ.tax_rate, "x")
    assert all(recommend(cells(), hi, n, Decimal("0.05")) is None for n in NAVS)  # absurd fixed costs: nothing


def test_the_script_quick_mode_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "r.md"
    mod = runpy.run_path(str(SCRIPT))
    md = mod["main"](["--quick", "--out", str(out)])
    assert out.read_text(encoding="utf-8") == md and "ZERO-EDGE" in capsys.readouterr().out

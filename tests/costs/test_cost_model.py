"""Cost model tests. Expected numbers are the design-pack feasibility tables (docs/architecture/architecture.md §A2-A3,
the tools/feasibility_calc.py output), reproduced to Rs 0.01 by an independent Decimal implementation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from project100c.costs import CostModel, Side, load_brokerage_plans, load_charge_book
from project100c.costs.config import default_live_plan_id
from project100c.costs.feasibility import loss_at_stop, min_nav_for_premium, stop_room_points
from project100c.errors import (
    ConfigError,
    CostModelError,
    NoScheduleForDateError,
    UnverifiedConfigError,
)

LOT = 65
D = date(2026, 9, 30)
PAISA = Decimal("0.005")  # "to Rs 0.01": |diff| < half a paisa after 2-dp rounding of the reference


@pytest.fixture(scope="module")
def model(configs_dir: Path) -> CostModel:
    book = load_charge_book(configs_dir / "costs" / "nse_fo_index_options.toml")
    plans = load_brokerage_plans(configs_dir / "costs" / "brokerage_plans.toml")
    return CostModel(book, plans)


# ---- docs/architecture/architecture.md §A2 table B: round trip, buy and sell at the same premium, one lot ----
TABLE_B = [
    (10, "design-flat-20", "48.74"),
    (10, "design-zero", "1.54"),
    (30, "design-flat-20", "51.82"),
    (30, "design-zero", "4.62"),
    (60, "design-flat-20", "56.45"),
    (60, "design-zero", "9.25"),
    (100, "design-flat-20", "62.61"),
    (100, "design-zero", "15.41"),
    (150, "design-flat-20", "70.32"),
    (150, "design-zero", "23.12"),
]


@pytest.mark.parametrize(("premium", "plan", "expected"), TABLE_B)
def test_reproduces_feasibility_table_b(model: CostModel, premium: int, plan: str, expected: str) -> None:
    rt = model.round_trip(premium, premium, LOT, D, plan)
    assert abs(rt.total - Decimal(expected)) < PAISA
    assert rt.total_rounded() == Decimal(expected)


def test_component_breakdown_10pt_flat20(model: CostModel) -> None:
    rt = model.round_trip(10, 10, LOT, D, "design-flat-20")
    assert rt.brokerage == Decimal("40")
    assert rt.stt == Decimal("0.9750")  # 0.15% of 650 sell turnover
    assert rt.exchange_txn == Decimal("0.0003553") * 1300
    assert rt.sebi_fee == Decimal("0.0013")
    assert rt.stamp_duty == Decimal("0.01950")  # 0.003% of 650 buy turnover
    assert rt.gst == Decimal("0.18") * (rt.brokerage + rt.exchange_txn + rt.sebi_fee)
    assert rt.schedule_versions == ("CM-2026-04-01",)
    assert rt.uses_unverified is False


def test_upstox_is_default_live_plan_and_equals_flat20(model: CostModel, configs_dir: Path) -> None:
    assert default_live_plan_id(configs_dir / "costs" / "brokerage_plans.toml") == "upstox-options"
    for premium, plan, expected in TABLE_B:
        if plan == "design-flat-20":
            assert model.round_trip(premium, premium, LOT, D, "upstox-options").total_rounded() == Decimal(expected)


def test_stop_room_points_at_10k(model: CostModel) -> None:
    kw = dict(nav=10_000, max_loss_fraction="0.02", premium=10, lot_size=LOT, slippage_per_side="0.5", trade_date=D)
    upstox = stop_room_points(model, plan_id="upstox-options", **kw)  # type: ignore[arg-type]
    zero = stop_room_points(model, plan_id="design-zero", **kw)  # type: ignore[arg-type]
    assert upstox.quantize(Decimal("0.01")) == Decimal("1.33")
    assert zero.quantize(Decimal("0.01")) == Decimal("2.05")
    # Max stop width before any cost: 200/65 = 3.08 points
    assert (Decimal(200) / LOT).quantize(Decimal("0.01")) == Decimal("3.08")


# ---- docs/architecture/architecture.md §A3 / appendix section D: minimum NAV (one lot) ----
TABLE_D = [(10, 22_686), (20, 22_932), (30, 30_169), (60, 54_729), (100, 87_474), (150, 128_407)]


@pytest.mark.parametrize(("premium", "expected_nav"), TABLE_D)
def test_reproduces_min_nav_table(model: CostModel, premium: int, expected_nav: int) -> None:
    r = min_nav_for_premium(model, premium=premium, lot_size=LOT, trade_date=D, plan_id="design-flat-20")
    assert r.binding_min_nav.quantize(Decimal("1")) == Decimal(expected_nav)


def test_h07_example_and_plumbing_trade(model: CostModel) -> None:
    common = dict(lot_size=LOT, slippage_per_side="0.25", trade_date=D)
    h07_upstox = loss_at_stop(model, entry=8, stop_exit=6, plan_id="upstox-options", **common)  # type: ignore[arg-type]
    h07_zero = loss_at_stop(model, entry=8, stop_exit=6, plan_id="design-zero", **common)  # type: ignore[arg-type]
    plumbing = loss_at_stop(model, entry=5, stop_exit=4, plan_id="upstox-options", **common)  # type: ignore[arg-type]
    assert h07_upstox.quantize(Decimal("0.01")) == Decimal("210.68")  # > Rs 200: ineligible at Rs 10k
    assert h07_upstox > 200
    assert h07_zero.quantize(Decimal("0.01")) == Decimal("163.48")
    assert plumbing.quantize(Decimal("0.01")) == Decimal("145.35")  # fits OD-001 plumbing budget
    assert plumbing <= 200


# ---- dated schedules ----
def test_march_2026_uses_old_stt_verified(model: CostModel) -> None:
    sell = model.order_charges(Side.SELL, 100, LOT, date(2026, 3, 15), "upstox-options")
    assert sell.stt == Decimal("0.0010") * 6500
    assert sell.schedule_versions == ("CM-2026-03-01",)
    assert not sell.uses_unverified


def test_stt_switch_on_1_april_2026(model: CostModel) -> None:
    before = model.order_charges(Side.SELL, 100, LOT, date(2026, 3, 31), "upstox-options")
    after = model.order_charges(Side.SELL, 100, LOT, date(2026, 4, 1), "upstox-options")
    assert before.stt == Decimal("6.5") and after.stt == Decimal("9.75")


def test_unverified_schedule_refused_unless_opted_in(configs_dir: Path, tmp_path: Path) -> None:
    text = (configs_dir / "costs" / "nse_fo_index_options.toml").read_text()
    head, sep, tail = text.partition('version = "CM-2024-10-01"')
    path = tmp_path / "book.toml"
    path.write_text(head + sep + tail.replace("verified = true", "verified = false", 1))
    book = load_charge_book(path)
    plans = load_brokerage_plans(configs_dir / "costs" / "brokerage_plans.toml")
    strict = CostModel(book, plans)
    with pytest.raises(UnverifiedConfigError):
        strict.order_charges(Side.SELL, 100, LOT, date(2025, 6, 2), "upstox-options")
    lax = CostModel(book, plans, allow_unverified=True)
    c = lax.order_charges(Side.SELL, 100, LOT, date(2025, 6, 2), "upstox-options")
    assert c.uses_unverified is True
    assert c.stt == Decimal("6.5")


def test_no_schedule_before_coverage(model: CostModel) -> None:
    with pytest.raises(NoScheduleForDateError):
        model.order_charges(Side.BUY, 100, LOT, date(2021, 9, 30), "upstox-options")


@pytest.mark.parametrize(
    ("day", "stt_rate", "txn_rate", "version"),
    [
        # official change dates (NSE/FATAX/56235, NSE/FATAX/63809, NSE/FA/46730, NSE/FA/56129, NSE/FA/64232)
        (date(2021, 10, 1), "0.0005", "0.00053", "CM-2021-10-01"),
        (date(2023, 3, 31), "0.0005", "0.00053", "CM-2021-10-01"),
        (date(2023, 4, 3), "0.000625", "0.0005", "CM-2023-04-01"),
        (date(2024, 9, 30), "0.000625", "0.0005", "CM-2023-04-01"),
        (date(2024, 10, 1), "0.0010", "0.0003553", "CM-2024-10-01"),
        (date(2026, 2, 27), "0.0010", "0.0003553", "CM-2024-10-01"),
    ],
)
def test_historical_schedules_are_verified_at_each_change_date(
    model: CostModel, day: date, stt_rate: str, txn_rate: str, version: str
) -> None:
    sell = model.order_charges(Side.SELL, 100, LOT, day, "upstox-options")
    assert sell.schedule_versions == (version,) and not sell.uses_unverified
    assert sell.stt == Decimal(stt_rate) * 100 * LOT
    assert sell.exchange_txn == Decimal(txn_rate) * 100 * LOT


def test_every_schedule_is_verified_with_official_sources(configs_dir: Path) -> None:
    book = load_charge_book(configs_dir / "costs" / "nse_fo_index_options.toml")
    assert all(s.verified for s in book.schedule)
    for s in book.schedule:
        if s.effective_from < date(2026, 3, 1):
            assert s.verified_on == date(2026, 10, 2)
            assert any("nseindia.com" in x for x in s.sources) and any("sebi.gov.in" in x for x in s.sources)
            assert any("cbic-gst.gov.in" in x for x in s.sources)


# ---- no silent failures on bad input ----
@pytest.mark.parametrize("bad_price", [10.0, 0, -1, "abc", True])
def test_bad_price_raises(model: CostModel, bad_price: object) -> None:
    with pytest.raises(CostModelError):
        model.order_charges(Side.BUY, bad_price, LOT, D, "upstox-options")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_qty", [0, -65, 65.0, True])
def test_bad_quantity_raises(model: CostModel, bad_qty: object) -> None:
    with pytest.raises(CostModelError):
        model.order_charges(Side.BUY, 10, bad_qty, D, "upstox-options")  # type: ignore[arg-type]


def test_unknown_plan_raises(model: CostModel) -> None:
    with pytest.raises(CostModelError):
        model.order_charges(Side.BUY, 10, LOT, D, "dhan-options")


def test_exercise_without_configured_brokerage_raises(model: CostModel) -> None:
    with pytest.raises(CostModelError):
        model.exercise_charges(12, LOT, D, "upstox-options")


def test_side_must_be_enum(model: CostModel) -> None:
    with pytest.raises(CostModelError):
        model.order_charges("BUY", 10, LOT, D, "upstox-options")  # type: ignore[arg-type]


def test_stop_above_entry_raises(model: CostModel) -> None:
    with pytest.raises(CostModelError):
        loss_at_stop(
            model, entry=8, stop_exit=9, lot_size=LOT, slippage_per_side=0, trade_date=D, plan_id="upstox-options"
        )


# ---- config validation ----
def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(text)
    return p


SCHED = """
[[schedule]]
version = "{v}"
effective_from = {f}
{to}
stt_sell_rate = {stt}
stt_exercise_rate = "0.0015"
exchange_txn_rate = "0.0003553"
sebi_fee_per_crore = "10"
stamp_buy_rate = "0.00003"
gst_rate = "0.18"
verified = true
"""


def _book(*scheds: str) -> str:
    return 'book_id = "X"\ncurrency = "INR"\nconfig_version = "t"\n' + "".join(scheds)


def test_config_rejects_float_rates(tmp_path: Path) -> None:
    text = _book(SCHED.format(v="A", f="2026-04-01", to="", stt="0.0015"))
    with pytest.raises(ConfigError):
        load_charge_book(_write(tmp_path, text))


def test_config_rejects_overlap(tmp_path: Path) -> None:
    text = _book(
        SCHED.format(v="A", f="2026-01-01", to="effective_to = 2026-05-01", stt='"0.001"'),
        SCHED.format(v="B", f="2026-04-01", to="", stt='"0.0015"'),
    )
    with pytest.raises(ConfigError):
        load_charge_book(_write(tmp_path, text))


def test_config_rejects_open_ended_not_last(tmp_path: Path) -> None:
    text = _book(
        SCHED.format(v="A", f="2026-01-01", to="", stt='"0.001"'),
        SCHED.format(v="B", f="2026-04-01", to="", stt='"0.0015"'),
    )
    with pytest.raises(ConfigError):
        load_charge_book(_write(tmp_path, text))


def test_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_charge_book(tmp_path / "nope.toml")


def test_plan_config_rejects_duplicates_and_bad_default(tmp_path: Path) -> None:
    one = '[[plan]]\nplan_id="a"\nkind="zero"\nverified=true\n'
    dup = 'config_version="t"\n' + one + one
    with pytest.raises(ConfigError):
        load_brokerage_plans(_write(tmp_path, dup))
    bad_default = 'config_version="t"\ndefault_live_plan="zz"\n[[plan]]\nplan_id="a"\nkind="zero"\nverified=true\n'
    with pytest.raises(ConfigError):
        load_brokerage_plans(_write(tmp_path, bad_default))


def test_pct_with_cap_plan(tmp_path: Path, configs_dir: Path) -> None:
    text = 'config_version="t"\n[[plan]]\nplan_id="p"\nkind="pct_with_cap"\nrate="0.0003"\ncap="20"\nverified=true\n'
    plans = load_brokerage_plans(_write(tmp_path, text))
    book = load_charge_book(configs_dir / "costs" / "nse_fo_index_options.toml")
    m = CostModel(book, plans)
    small = m.order_charges(Side.BUY, 10, LOT, D, "p")
    big = m.order_charges(Side.BUY, 1000, LOT * 10, D, "p")
    assert small.brokerage == Decimal("0.0003") * 650
    assert big.brokerage == Decimal("20")


# ---- properties ----
prices = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("2000"), places=2, allow_nan=False)
lots = st.integers(min_value=1, max_value=27)


@given(p=prices, n=lots)
def test_property_non_negative_and_sidedness(model: CostModel, p: Decimal, n: int) -> None:
    buy = model.order_charges(Side.BUY, p, n * LOT, D, "upstox-options")
    sell = model.order_charges(Side.SELL, p, n * LOT, D, "upstox-options")
    assert buy.total > 0 and sell.total > 0
    assert buy.stt == 0 and sell.stamp_duty == 0
    assert sell.stt > 0 and buy.stamp_duty > 0


@given(p1=prices, p2=prices)
def test_property_monotone_in_price(model: CostModel, p1: Decimal, p2: Decimal) -> None:
    lo, hi = sorted((p1, p2))
    a = model.round_trip(lo, lo, LOT, D, "upstox-options").total
    b = model.round_trip(hi, hi, LOT, D, "upstox-options").total
    assert a <= b


@given(p1=prices, p2=prices)
def test_property_round_trip_is_sum_of_legs(model: CostModel, p1: Decimal, p2: Decimal) -> None:
    rt = model.round_trip(p1, p2, LOT, D, "upstox-options")
    buy = model.order_charges(Side.BUY, p1, LOT, D, "upstox-options")
    sell = model.order_charges(Side.SELL, p2, LOT, D, "upstox-options")
    assert rt.total == buy.total + sell.total


def test_upstox_plus_plan_is_30_per_executed_order(model: CostModel) -> None:
    """S73/S74 (1-Oct-2026): Upstox Plus has no subscription fee today but charges Rs 30 per options order."""
    plus = model.order_charges(Side.BUY, 100, LOT, D, "upstox-plus-options")
    basic = model.order_charges(Side.BUY, 100, LOT, D, "upstox-options")
    assert plus.brokerage == Decimal(30) and basic.brokerage == Decimal(20)
    assert plus.total - basic.total == Decimal(10) * (1 + model.schedule(D).gst_rate)

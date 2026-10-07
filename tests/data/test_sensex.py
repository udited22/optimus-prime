"""SENSEX (BSE) data support: backfill plan, BSE_FNO specs, BSE calendar, SENSEX expiry rules, lot history and the
BSE cost book (all offline; the cited notices are restated in each config)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.calendar.model import ExpiryKind
from project100c.costs import load_charge_book
from project100c.data.dhan import CandleJobSpec, RollingOptionJobSpec
from project100c.data.dhan.backfill import (
    SENSEX_INDEX_LABEL,
    SENSEX_OPTIONS_FROM,
    plan_backfill,
    plan_sensex_backfill,
)
from project100c.data.dhan.config import load_dhan_config
from project100c.data.dhan.jobs import job_id_for
from project100c.errors import CalendarError
from project100c.instruments.lot_history import load_lot_history
from tests.data.dhan_fakes import REPO

CFG = REPO / "configs"
W, M = ExpiryKind.WEEKLY, ExpiryKind.MONTHLY


@pytest.fixture(scope="module")
def exp() -> ExpiryCalendar:
    cal = TradingCalendar(load_holiday_book(CFG / "calendar" / "bse_fo_holidays.toml"))
    return ExpiryCalendar(cal, load_expiry_rules(CFG / "calendar" / "sensex_expiry_rules.toml"))


# ------------------------------------------------------------------ plan
def test_sensex_plan_shape_and_determinism() -> None:
    items = plan_sensex_backfill(anchor=date(2026, 10, 3))
    assert items == plan_sensex_backfill(anchor=date(2026, 10, 3))
    kinds = {(i.tier, i.series) for i in items}
    assert kinds == {(t, k) for t in "AB" for k in ("index", "options_near", "options_next")}
    assert len(items) == 105
    opts = [i.spec for i in items if isinstance(i.spec, RollingOptionJobSpec)]
    assert all(s.exchange_segment == "BSE_FNO" and s.underlying_security_id == 51 for s in opts)
    assert min(s.from_date for s in opts) == SENSEX_OPTIONS_FROM == date(2023, 5, 15)
    near = [s for s in opts if s.expiry_codes == (1,)]
    assert {len(s.strike_offsets) for s in near} == {21}
    idx = [i.spec for i in items if i.series == "index"]
    assert all(isinstance(s, CandleJobSpec) and s.label == SENSEX_INDEX_LABEL for s in idx)
    assert len(idx) == 21


def test_sensex_job_ids_never_collide_with_nifty() -> None:
    a = date(2026, 10, 2)
    nifty = {job_id_for(i.spec) for i in plan_backfill(anchor=a, futures=[])}
    sensex = {job_id_for(i.spec) for i in plan_sensex_backfill(anchor=a)}
    assert nifty and sensex and not nifty & sensex


def test_specs_accept_bse_fno_and_reject_other_segments() -> None:
    RollingOptionJobSpec(
        underlying="SENSEX",
        underlying_security_id=51,
        exchange_segment="BSE_FNO",
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 30),
        window_days=30,
        expiry_codes=(1,),
        strike_offsets=(0,),
    )
    with pytest.raises(ValueError):
        RollingOptionJobSpec(
            underlying="SENSEX",
            underlying_security_id=51,
            exchange_segment="MCX_COMM",
            from_date=date(2026, 9, 1),
            to_date=date(2026, 9, 30),
            window_days=30,
            expiry_codes=(1,),
            strike_offsets=(0,),
        )


def test_dhan_config_carries_sensex_ids() -> None:
    cfg = load_dhan_config(CFG / "data" / "dhan.toml")
    assert cfg.sensex_index_security_id == "51"
    assert cfg.sensex_strike_step == Decimal("100")


# ------------------------------------------------------------------ calendar
def test_bse_calendar_matches_the_nse_book_day_for_day() -> None:
    bse = load_holiday_book(CFG / "calendar" / "bse_fo_holidays.toml")
    nse = load_holiday_book(CFG / "calendar" / "nse_fo_holidays.toml")
    assert bse.covered_years == nse.covered_years
    assert [(d.date, d.kind) for d in bse.days] == [(d.date, d.kind) for d in nse.days]
    assert bse.version.startswith("BSE-")


EXPIRY_CASES = [
    (date(2023, 5, 15), W, date(2023, 5, 19), "relaunch: Friday weekly (BSE 20230327-65)"),
    (date(2024, 3, 25), M, date(2024, 3, 28), "Good Friday 29-Mar-2024: moved to Thursday"),
    (date(2025, 1, 1), W, date(2025, 1, 3), "last Friday weekly"),
    (date(2025, 1, 6), W, date(2025, 1, 7), "Tuesday from 6-Jan-2025 (BSE 20241128-70)"),
    (date(2025, 9, 1), W, date(2025, 9, 4), "Thursday from 1-Sep-2025 (BSE 20250623-59)"),
    (date(2025, 9, 29), W, date(2025, 10, 1), "2-Oct-2025 holiday: moved to Wednesday"),
    (date(2026, 9, 28), W, date(2026, 10, 1), "current Thursday rule"),
]


@pytest.mark.parametrize(("day", "kind", "expected", "why"), EXPIRY_CASES)
def test_sensex_expiry_golden(exp: ExpiryCalendar, day: date, kind: ExpiryKind, expected: date, why: str) -> None:
    e = exp.weekly_for_week(day)
    assert (e.date, e.kind) == (expected, kind), why


def test_sensex_monthlies_and_no_rule_before_relaunch(exp: ExpiryCalendar) -> None:
    got = [exp.monthly(y, m).date for y, m in ((2023, 5), (2025, 1), (2025, 8), (2025, 9), (2026, 9))]
    assert got == [date(2023, 5, 26), date(2025, 1, 28), date(2025, 8, 26), date(2025, 9, 25), date(2026, 9, 24)]
    with pytest.raises(CalendarError):
        exp.weekly_for_week(date(2023, 5, 8))


# ------------------------------------------------------------------ lots
LOT_CASES = [
    (date(2023, 5, 19), W, date(2023, 5, 15), 10, "relaunch lot 10 (BSE 20230327-65)"),
    (date(2025, 1, 3), W, date(2024, 12, 30), 10, "BSE-20241021-13: weeklies up to 3-Jan-2025 keep 10"),
    (date(2025, 1, 7), W, date(2024, 12, 30), 20, "first weekly with lot 20"),
    (date(2025, 1, 28), M, date(2025, 1, 20), 10, "January-2025 monthly keeps 10"),
    (date(2025, 2, 25), M, date(2025, 1, 20), 20, "first monthly with lot 20"),
    (date(2026, 10, 1), W, date(2026, 9, 28), 20, "current lot 20 (Dhan master 2-Oct-2026)"),
]


@pytest.mark.parametrize(("expiry", "kind", "trade", "lot", "why"), LOT_CASES)
def test_sensex_lot_history(expiry: date, kind: ExpiryKind, trade: date, lot: int, why: str) -> None:
    hist = load_lot_history(CFG / "instruments" / "sensex_lot_sizes.toml")
    assert hist.lot_size(expiry, kind, trade) == lot, why


def test_sensex_revision_boundaries_are_consecutive_expiries(exp: ExpiryCalendar) -> None:
    hist = load_lot_history(CFG / "instruments" / "sensex_lot_sizes.toml")
    for r in hist.revisions:
        for kind in (W, M):
            lo, hi = r.last_old(kind), r.first_new(kind)
            between = [e for e in exp.expiries_between(lo, hi) if e.date not in (lo, hi) and e.kind is kind]
            assert between == [], (r.id, kind, between)


# ------------------------------------------------------------------ costs
def test_bse_cost_book_dates_and_verification_flags() -> None:
    book = load_charge_book(CFG / "costs" / "bse_fo_index_options.toml")
    by_from = {s.effective_from: s for s in book.schedule}
    assert by_from[date(2023, 5, 15)].exchange_txn_rate == Decimal("0.00005")
    assert by_from[date(2024, 10, 1)].exchange_txn_rate == Decimal("0.000325")
    assert by_from[date(2026, 4, 1)].stt_sell_rate == Decimal("0.0015")
    unverified = {s.effective_from for s in book.schedule if not s.verified}
    assert unverified == {date(2023, 11, 1), date(2024, 5, 13)}  # member-slab periods: honest about pass-through


def test_sensex_configs_live_next_to_nifty() -> None:
    for p in (
        "calendar/bse_fo_holidays.toml",
        "calendar/sensex_expiry_rules.toml",
        "instruments/sensex_lot_sizes.toml",
        "costs/bse_fo_index_options.toml",
    ):
        assert (CFG / p).is_file(), p

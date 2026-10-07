"""Trading calendar + NIFTY expiry rules (D-03): holidays vs NSE circulars, golden expiry table, market clock."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import pytest

from project100c.calendar import (
    DayKind,
    ExpiryCalendar,
    ExpiryKind,
    MarketClock,
    TradingCalendar,
    load_expiry_rules,
    load_holiday_book,
)
from project100c.errors import CalendarCoverageError, CalendarError, ConfigError
from project100c.sessions import IST, SessionCalendar, WindowPhase, load_exchange_sessions, load_trading_windows

# Second, independent source: the NSE circular lists (transcribed; see configs/calendar/README.md).
NSE_2021 = [
    "2021-01-26", "2021-03-11", "2021-03-29", "2021-04-02", "2021-04-14", "2021-04-21", "2021-05-13", "2021-07-21",
    "2021-08-19", "2021-09-10", "2021-10-15", "2021-11-04", "2021-11-05", "2021-11-19",
]  # fmt: skip
NSE_2022 = [
    "2022-01-26", "2022-03-01", "2022-03-18", "2022-04-14", "2022-04-15", "2022-05-03", "2022-08-09", "2022-08-15",
    "2022-08-31", "2022-10-05", "2022-10-24", "2022-10-26", "2022-11-08",
]  # fmt: skip
NSE_2023 = [  # NSE/FAOP/54759 with Bakri Id moved from 28-Jun to 29-Jun-2023
    "2023-01-26", "2023-03-07", "2023-03-30", "2023-04-04", "2023-04-07", "2023-04-14", "2023-05-01", "2023-06-29",
    "2023-08-15", "2023-09-19", "2023-10-02", "2023-10-24", "2023-11-14", "2023-11-27", "2023-12-25",
]  # fmt: skip
NSE_2024 = [
    "2024-01-22", "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29", "2024-04-11", "2024-04-17", "2024-05-01",
    "2024-05-20", "2024-06-17", "2024-07-17", "2024-08-15", "2024-10-02", "2024-11-01", "2024-11-15", "2024-11-20",
    "2024-12-25",
]  # fmt: skip
NSE_2025 = [
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15",
    "2025-08-27", "2025-10-02", "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
]  # fmt: skip
NSE_2026 = [
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
]  # fmt: skip
MUHURAT_WEEKDAY = {
    "2021-11-04",
    "2022-10-24",
    "2024-11-01",
    "2025-10-21",
}  # listed by NSE as holidays with a Muhurat session


@pytest.fixture(scope="module")
def cal(configs_dir: Path) -> TradingCalendar:
    return TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml"))


@pytest.fixture(scope="module")
def exp(cal: TradingCalendar, configs_dir: Path) -> ExpiryCalendar:
    return ExpiryCalendar(cal, load_expiry_rules(configs_dir / "calendar" / "nifty_expiry_rules.toml"))


@pytest.mark.parametrize("year_list", [NSE_2021, NSE_2022, NSE_2023, NSE_2024, NSE_2025, NSE_2026])
def test_weekday_closures_match_nse_circulars(cal: TradingCalendar, year_list: list[str]) -> None:
    year = int(year_list[0][:4])
    closed = {
        d.isoformat()
        for d in (
            date(year, 1, 1).fromordinal(o)
            for o in range(date(year, 1, 1).toordinal(), date(year, 12, 31).toordinal() + 1)
        )
        if d.weekday() < 5 and not cal.is_trading_day(d)
    }
    assert closed == set(year_list)


def test_muhurat_days_are_special_sessions_not_trading_days(cal: TradingCalendar) -> None:
    for s in MUHURAT_WEEKDAY:
        e = cal.entry(date.fromisoformat(s))
        assert e is not None and e.kind is DayKind.SPECIAL_SESSION and e.verified
        assert not cal.is_trading_day(date.fromisoformat(s))


def test_settlement_holiday_is_a_trading_day(cal: TradingCalendar) -> None:
    d = date(2026, 8, 26)  # Id-E-Milad: settlement holiday, F&O open
    e = cal.entry(d)
    assert e is not None and e.kind is DayKind.SETTLEMENT_HOLIDAY and not e.verified
    assert cal.is_trading_day(d)


def test_every_trading_holiday_is_verified(cal: TradingCalendar, configs_dir: Path) -> None:
    book = load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml")
    th = [d for d in book.days if d.kind is DayKind.TRADING_HOLIDAY]
    assert len(th) == 85 and all(d.verified and len(d.sources) >= 2 for d in th)


def test_outside_coverage_raises_and_provisional_uses_fixed_dates(cal: TradingCalendar) -> None:
    with pytest.raises(CalendarCoverageError):
        cal.is_trading_day(date(2027, 1, 4))
    with pytest.raises(CalendarCoverageError):
        cal.is_trading_day(date(2020, 12, 31))
    assert not cal.is_trading_day(date(2029, 12, 25), provisional=True)
    assert cal.is_trading_day(date(2029, 12, 24), provisional=True)


def test_unverified_closure_needs_opt_in(configs_dir: Path, tmp_path: Path) -> None:
    text = (configs_dir / "calendar" / "nse_fo_holidays.toml").read_text()
    text += (
        '\n[[day]]\ndate = 2026-12-30\nkind = "TRADING_HOLIDAY"\ndescription = "rumoured"\n'
        'sources = ["blog"]\nverified = false\n'
    )
    p = tmp_path / "h.toml"
    p.write_text(text)
    strict = TradingCalendar(load_holiday_book(p))
    with pytest.raises(CalendarError, match="UNVERIFIED"):
        strict.is_trading_day(date(2026, 12, 30))
    assert not TradingCalendar(load_holiday_book(p), allow_unverified=True).is_trading_day(date(2026, 12, 30))


def test_book_validation(tmp_path: Path) -> None:
    bad = tmp_path / "b.toml"
    bad.write_text(
        'version="x"\ncovered_years=[2026]\nfixed_date_holidays=[]\n'
        '[[day]]\ndate=2026-03-03\nkind="TRADING_HOLIDAY"\ndescription="Holi"\nsources=["one"]\nverified=true\n'
    )
    with pytest.raises(ConfigError, match="2 independent sources"):
        load_holiday_book(bad)
    bad.write_text(
        'version="x"\ncovered_years=[2026]\nfixed_date_holidays=[]\n'
        '[[day]]\ndate=2025-03-03\nkind="TRADING_HOLIDAY"\ndescription="x"\nsources=["a","b"]\nverified=true\n'
    )
    with pytest.raises(ConfigError, match="outside covered_years"):
        load_holiday_book(bad)
    with pytest.raises(ConfigError, match="not found"):
        load_holiday_book(tmp_path / "missing.toml")


def _golden(configs_dir: Path) -> list[dict[str, str]]:
    with (configs_dir / "calendar" / "nifty_expiry_golden.csv").open() as fh:
        return list(csv.DictReader(fh))


def test_golden_expiry_table(exp: ExpiryCalendar, configs_dir: Path) -> None:
    rows = _golden(configs_dir)
    assert len(rows) == 65
    for r in rows:
        d = date.fromisoformat(r["expiry_date"])
        e = exp.weekly_for_week(d, provisional=True)
        if e.date != d:  # a holiday shift can move the expiry into the previous week
            e = exp.weekly_for_week(d + (date(2000, 1, 8) - date(2000, 1, 1)), provisional=True)
        assert e.date == d, f"{r['expiry_date']}: computed {e.date}"
        assert e.kind.value == r["kind"], f"{d}: kind {e.kind} vs {r['kind']}"
        assert e.provisional == (d.year > 2026)


def test_golden_monthlies_via_monthly_api(exp: ExpiryCalendar, configs_dir: Path) -> None:
    for r in _golden(configs_dir):
        if r["kind"] == "MONTHLY":
            d = date.fromisoformat(r["expiry_date"])
            m = exp.monthly(d.year, d.month, provisional=True)
            assert m.date == d


def test_bakri_id_2023_move_is_modelled(cal: TradingCalendar, exp: ExpiryCalendar) -> None:
    """NSE/FAOP/54759 listed 28-Jun-2023; the holiday moved to 29-Jun-2023, so the June expiry became 28-Jun."""
    assert cal.is_trading_day(date(2023, 6, 28)) and not cal.is_trading_day(date(2023, 6, 29))
    m = exp.monthly(2023, 6)
    assert (m.date, m.nominal, m.shifted) == (date(2023, 6, 28), date(2023, 6, 29), True)


def test_2021_2023_expiries_all_thursday_rule(exp: ExpiryCalendar) -> None:
    es = exp.expiries_between(date(2021, 1, 11), date(2023, 12, 31))  # coverage starts 2021: skip week 1
    assert all(e.rule_version == "NIFTY-EXP-THU" for e in es)
    assert all(e.nominal.weekday() == 3 for e in es)
    assert {e.date.isoformat() for e in es if e.shifted} == {
        "2021-03-10",
        "2021-05-12",
        "2021-08-18",
        "2021-11-03",
        "2022-04-13",
        "2023-01-25",
        "2023-03-29",
        "2023-06-28",
    }  # the eight Thursday holidays of 2021-2023, each moved to Wednesday
    assert sum(e.kind.value == "MONTHLY" for e in es) == 36


def test_holiday_shift_details(exp: ExpiryCalendar) -> None:
    e = exp.weekly_for_week(date(2026, 10, 20))
    assert (e.date, e.nominal, e.shifted, e.rule_version) == (
        date(2026, 10, 19),
        date(2026, 10, 20),
        True,
        "NIFTY-EXP-TUE",
    )
    e = exp.weekly_for_week(date(2025, 10, 21))  # Muhurat day Tuesday -> Monday
    assert e.date == date(2025, 10, 20) and e.shifted
    e = exp.weekly_for_week(date(2024, 8, 15))  # Independence Day Thursday -> Wednesday
    assert e.date == date(2024, 8, 14) and e.rule_version == "NIFTY-EXP-THU"
    e = exp.weekly_for_week(date(2026, 11, 10))  # Diwali Balipratipada Tuesday -> Monday
    assert e.date == date(2026, 11, 9)


def test_rule_transition_week(exp: ExpiryCalendar) -> None:
    assert exp.weekly_for_week(date(2025, 8, 28)).date == date(2025, 8, 28)
    assert exp.weekly_for_week(date(2025, 9, 1)).date == date(2025, 9, 2)


def test_expiries_between_and_is_expiry_day(exp: ExpiryCalendar) -> None:
    got = [e.date for e in exp.expiries_between(date(2026, 10, 1), date(2026, 11, 30))]
    assert got == [
        date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 19), date(2026, 10, 27),
        date(2026, 11, 3), date(2026, 11, 9), date(2026, 11, 17), date(2026, 11, 23),
    ]  # fmt: skip
    assert exp.is_expiry_day(date(2026, 10, 19)) and not exp.is_expiry_day(date(2026, 10, 20))
    with pytest.raises(CalendarError):
        exp.expiries_between(date(2026, 11, 1), date(2026, 10, 1))


def test_trading_days_count_2025(cal: TradingCalendar) -> None:
    days = cal.trading_days(date(2025, 1, 1), date(2025, 12, 31))
    weekdays = sum(
        1
        for o in range(date(2025, 1, 1).toordinal(), date(2025, 12, 31).toordinal() + 1)
        if date.fromordinal(o).weekday() < 5
    )
    assert len(days) == weekdays - len(NSE_2025)
    assert cal.previous_trading_day(date(2025, 4, 10)) == date(2025, 4, 9)
    assert cal.next_trading_day(date(2025, 10, 20)) == date(2025, 10, 23)


# ---- market clock ----
@pytest.fixture(scope="module")
def clock(cal: TradingCalendar, configs_dir: Path) -> MarketClock:
    s = SessionCalendar(
        load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(configs_dir / "sessions" / "trading_window.toml"),
    )
    return MarketClock(cal, s)


def test_clock_holiday_is_closed(clock: MarketClock) -> None:
    ts = datetime(2026, 10, 2, 10, 0, tzinfo=IST)  # Gandhi Jayanti
    assert clock.phase(ts) is WindowPhase.CLOSED
    assert not clock.order_activity_allowed(ts) and not clock.entry_allowed(ts) and clock.must_be_flat(ts)


def test_clock_trading_day_phases(clock: MarketClock) -> None:
    d = (2026, 10, 1)
    assert clock.phase(datetime(*d, 9, 16, tzinfo=IST)) is WindowPhase.OPENING_NO_ENTRY
    assert clock.entry_allowed(datetime(*d, 10, 0, tzinfo=IST))
    assert clock.phase(datetime(*d, 14, 0, tzinfo=IST)) is WindowPhase.EXIT_ONLY  # OD-008 cutoff
    assert clock.phase(datetime(*d, 14, 55, tzinfo=IST)) is WindowPhase.FLATTENING
    assert clock.phase(datetime(*d, 15, 0, tzinfo=IST)) is WindowPhase.CLOSED
    assert not clock.must_be_flat(datetime(*d, 14, 59, tzinfo=IST))


def test_clock_rejects_naive_and_uncovered(clock: MarketClock) -> None:
    from project100c.errors import SessionError

    with pytest.raises(SessionError):
        clock.phase(datetime(2026, 10, 1, 10, 0))
    with pytest.raises(CalendarCoverageError):
        clock.phase(datetime(2027, 1, 4, 10, 0, tzinfo=IST))


def test_expiry_kind_enum_values() -> None:
    assert {k.value for k in ExpiryKind} == {"WEEKLY", "MONTHLY"}


def test_planned_date_superseded_by_later_holiday(exp: ExpiryCalendar) -> None:
    """NSE/FAOP/68747 (Jun-2025) planned the Mar-2026 quarterly for 31-Mar-2026, 'subject to change on account of any
    holiday'. The 2026 holiday circular then made 31-Mar-2026 a holiday (Mahavir Jayanti), so the rule moves it to
    Monday 30-Mar-2026. That derived date is NOT in the golden table: it has no independent exchange listing here."""
    m = exp.monthly(2026, 3)
    assert (m.nominal, m.date, m.shifted) == (date(2026, 3, 31), date(2026, 3, 30), True)

"""OD-015: float-noise tolerance on OHLC consistency and the opening vendor-gap rule."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.dq import DQCode, DQSeverity, DQThresholds, check_bars, load_thresholds
from project100c.dq.checks import ohlc_verdict
from project100c.market_types import Bar
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows

D = Decimal
DAY = date(2026, 3, 27)  # the real case: 09:15-09:18 missing for almost every option strike
KEY = "OPT"


@pytest.fixture(scope="module")
def th(configs_dir: Path) -> DQThresholds:
    return load_thresholds(configs_dir / "dq" / "thresholds.toml")


@pytest.fixture(scope="module")
def cal(configs_dir: Path) -> SessionCalendar:
    return SessionCalendar(
        load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(configs_dir / "sessions" / "trading_window.toml"),
    )


def _bars(skip_first: int, n: int = 375) -> list[Bar]:
    t0 = datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=IST)
    return [Bar(KEY, t0 + timedelta(minutes=i), D(100), D(101), D(99), D(100), 65, 1) for i in range(skip_first, n)]


def _index_minutes(n: int = 375) -> frozenset[datetime]:
    t0 = datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=IST)
    return frozenset(t0 + timedelta(minutes=i) for i in range(n))


def test_thresholds_carry_the_owner_values(th: DQThresholds) -> None:
    assert th.ohlc_rounding_tolerance == D("0.001") and th.max_opening_vendor_gap_minutes == 5


def test_opening_gap_of_4_minutes_with_index_present_is_a_warning(cal: SessionCalendar, th: DQThresholds) -> None:
    without = check_bars(_bars(4), trading_date=DAY, calendar=cal, thresholds=th)
    assert [i.severity for i in without.issues if i.code is DQCode.MISSING_CANDLE] == [DQSeverity.BLOCKING]
    rep = check_bars(_bars(4), trading_date=DAY, calendar=cal, thresholds=th, reference_starts=_index_minutes())
    assert rep.trustworthy
    (gap,) = [i for i in rep.issues if i.code is DQCode.OPENING_VENDOR_GAP]
    assert gap.severity is DQSeverity.WARNING and "4 bar(s)" in gap.detail
    assert not [i for i in rep.issues if i.code is DQCode.MISSING_CANDLE]


def test_opening_gap_rule_limits(cal: SessionCalendar, th: DQThresholds) -> None:
    # 6 minutes is more than 5: unchanged (missing candles, BLOCKING above 1%)
    r6 = check_bars(_bars(6), trading_date=DAY, calendar=cal, thresholds=th, reference_starts=_index_minutes())
    assert not r6.trustworthy and DQCode.OPENING_VENDOR_GAP not in r6.codes()
    # the index lacks one of those minutes: not a vendor gap
    ref = _index_minutes() - {datetime(2026, 3, 27, 9, 16, tzinfo=IST)}
    r_ref = check_bars(_bars(4), trading_date=DAY, calendar=cal, thresholds=th, reference_starts=ref)
    assert not r_ref.trustworthy and DQCode.OPENING_VENDOR_GAP not in r_ref.codes()
    # a gap that is not at the open is not covered
    bars = _bars(0)
    mid = bars[:100] + bars[104:]
    r_mid = check_bars(mid, trading_date=DAY, calendar=cal, thresholds=th, reference_starts=_index_minutes())
    assert not r_mid.trustworthy and DQCode.OPENING_VENDOR_GAP not in r_mid.codes()
    # the opening gap is excused but later missing bars still count
    rest = _bars(4)
    rest = rest[:200] + rest[202:]
    r_both = check_bars(rest, trading_date=DAY, calendar=cal, thresholds=th, reference_starts=_index_minutes())
    assert r_both.trustworthy and r_both.codes()[DQCode.MISSING_CANDLE] == 1
    # the setting 0 switches the rule off
    off = th.model_copy(update={"max_opening_vendor_gap_minutes": 0})
    r_off = check_bars(_bars(4), trading_date=DAY, calendar=cal, thresholds=off, reference_starts=_index_minutes())
    assert not r_off.trustworthy


def test_ohlc_float_noise_is_tolerated_but_real_inconsistency_is_not(cal: SessionCalendar, th: DQThresholds) -> None:
    t = datetime(2026, 9, 29, 9, 15, tzinfo=IST)
    real = Bar("IDX", t, D("24559.85"), D("24574.5996"), D("24559.25"), D("24574.6000"), 0, None)  # seen on Dhan
    assert ohlc_verdict(real, th.ohlc_rounding_tolerance) == "TOLERATED"
    assert ohlc_verdict(real, D(0)) == "BAD"
    bad = Bar("IDX", t, D("100"), D("99.99"), D("99"), D("100"), 0, None)
    assert ohlc_verdict(bad, th.ohlc_rounding_tolerance) == "BAD"
    low_bad = Bar("IDX", t, D("100"), D("101"), D("100.0005"), D("100.5"), 0, None)
    assert ohlc_verdict(low_bad, th.ohlc_rounding_tolerance) == "TOLERATED"
    assert ohlc_verdict(Bar("IDX", t, D("0.05"), D("0.05"), D("0"), D("0.05"), 0, None), D(1)) == "BAD"  # low <= 0
    day = [Bar("IDX", t + timedelta(minutes=i), D(100), D(101), D(99), D(100), 0, None) for i in range(385)]
    day[3] = Bar("IDX", day[3].start, D("100"), D("100.9996"), D("99"), D("101"), 0, None)
    rep = check_bars(day, trading_date=date(2026, 9, 29), calendar=cal, thresholds=th)
    assert rep.trustworthy and [i.severity for i in rep.issues] == [DQSeverity.INFO]
    assert rep.issues[0].code is DQCode.OHLC_WITHIN_TOLERANCE

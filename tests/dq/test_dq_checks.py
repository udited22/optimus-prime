"""Data-quality suite: a clean baseline produces no issues; every injected defect type is detected (100% recall
on the synthetic suite, backlog D-12 AT). Instruments come from the real Upstox fixture."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.dq import (
    DQCode,
    DQSeverity,
    DQThresholds,
    check_bars,
    check_instrument_master_change,
    check_quotes,
    load_thresholds,
)
from project100c.errors import ConfigError, DataQualityInputError
from project100c.instruments import InstrumentMaster, parse_upstox_json
from project100c.market_types import Bar, Quote
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows

AS_OF = datetime(2026, 9, 30, 23, 27, tzinfo=IST)
NOW = datetime(2026, 10, 1, 10, 0, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def th(configs_dir: Path) -> DQThresholds:
    return load_thresholds(configs_dir / "dq" / "thresholds.toml")


@pytest.fixture(scope="module")
def master(fixtures_dir: Path) -> InstrumentMaster:
    m, _ = parse_upstox_json(
        fixtures_dir / "instruments" / "upstox_NSE_subset_20260930.json", as_of=AS_OF, underlyings=frozenset({"NIFTY"})
    )
    return m


@pytest.fixture(scope="module")
def cal(configs_dir: Path) -> SessionCalendar:
    return SessionCalendar(
        load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(configs_dir / "sessions" / "trading_window.toml"),
    )


def _keys(master: InstrumentMaster) -> list[str]:
    return [c.instrument_key for c in master.contracts if c.expiry == date(2026, 10, 6)][:4]


def clean_quotes(master: InstrumentMaster) -> list[Quote]:
    out = []
    for i, k in enumerate(_keys(master)):
        ex = NOW - timedelta(seconds=1)
        out.append(
            Quote(
                instrument_key=k,
                exchange_ts=ex,
                receive_ts=ex + timedelta(milliseconds=150),
                bid=Decimal("100.00") + i,
                ask=Decimal("100.50") + i,
                bid_qty=650,
                ask_qty=325,
                ltp=Decimal("100.25") + i,
                oi=1_000_000,
            )
        )
    return out


def test_clean_quotes_have_no_issues(master: InstrumentMaster, th: DQThresholds) -> None:
    qs = clean_quotes(master)
    rep = check_quotes(qs, now=NOW, thresholds=th, master=master, required_keys=frozenset(q.instrument_key for q in qs))
    assert rep.issues == () and rep.trustworthy and rep.checked == 4


Mut = Callable[[list[Quote]], list[Quote]]


def _first(f: Callable[[Quote], Quote]) -> Mut:
    return lambda qs: [f(qs[0]), *qs[1:]]


D = Decimal
QUOTE_DEFECTS: list[tuple[str, Mut, DQCode, DQSeverity]] = [
    (
        "stale",
        _first(
            lambda q: dataclasses.replace(
                q, receive_ts=NOW - timedelta(seconds=9), exchange_ts=NOW - timedelta(seconds=9, milliseconds=100)
            )
        ),
        DQCode.STALE_QUOTE,
        DQSeverity.BLOCKING,
    ),
    (
        "stale exchange ts",
        _first(
            lambda q: dataclasses.replace(
                q, exchange_ts=NOW - timedelta(seconds=40), receive_ts=NOW - timedelta(seconds=1)
            )
        ),
        DQCode.STALE_EXCHANGE_TS,
        DQSeverity.BLOCKING,
    ),
    (
        "crossed",
        _first(lambda q: dataclasses.replace(q, bid=D("101.00"), ask=D("100.50"))),
        DQCode.CROSSED_MARKET,
        DQSeverity.BLOCKING,
    ),
    (
        "locked",
        _first(lambda q: dataclasses.replace(q, bid=D("100.50"), ask=D("100.50"))),
        DQCode.LOCKED_MARKET,
        DQSeverity.WARNING,
    ),
    (
        "zero price",
        _first(lambda q: dataclasses.replace(q, bid=D("0"))),
        DQCode.ZERO_OR_NEGATIVE_PRICE,
        DQSeverity.BLOCKING,
    ),
    (
        "negative ltp",
        _first(lambda q: dataclasses.replace(q, ltp=D("-1"))),
        DQCode.ZERO_OR_NEGATIVE_PRICE,
        DQSeverity.BLOCKING,
    ),
    ("one-sided", _first(lambda q: dataclasses.replace(q, ask=None)), DQCode.ONE_SIDED_MARKET, DQSeverity.BLOCKING),
    ("negative size", _first(lambda q: dataclasses.replace(q, bid_qty=-65)), DQCode.NEGATIVE_SIZE, DQSeverity.BLOCKING),
    ("missing OI", _first(lambda q: dataclasses.replace(q, oi=None)), DQCode.MISSING_OI, DQSeverity.BLOCKING),
    (
        "timestamp drift",
        _first(lambda q: dataclasses.replace(q, exchange_ts=q.receive_ts - timedelta(seconds=3))),
        DQCode.TIMESTAMP_DRIFT,
        DQSeverity.BLOCKING,
    ),
    (
        "future timestamp",
        _first(lambda q: dataclasses.replace(q, exchange_ts=q.receive_ts + timedelta(seconds=5))),
        DQCode.FUTURE_TIMESTAMP,
        DQSeverity.BLOCKING,
    ),
    ("duplicate", lambda qs: [*qs, qs[0]], DQCode.DUPLICATE_TICK, DQSeverity.INFO),
    (
        "out of order",
        lambda qs: [
            *qs,
            dataclasses.replace(qs[0], exchange_ts=qs[0].exchange_ts - timedelta(seconds=1), bid=D("99.00")),
        ],
        DQCode.OUT_OF_ORDER,
        DQSeverity.BLOCKING,
    ),
    (
        "abnormal spread",
        _first(lambda q: dataclasses.replace(q, bid=D("60.00"), ask=D("100.00"))),
        DQCode.ABNORMAL_SPREAD,
        DQSeverity.BLOCKING,
    ),
    ("off tick", _first(lambda q: dataclasses.replace(q, bid=D("100.02"))), DQCode.OFF_TICK_PRICE, DQSeverity.BLOCKING),
    (
        "unknown instrument",
        _first(lambda q: dataclasses.replace(q, instrument_key="NSE_FO|999999")),
        DQCode.UNKNOWN_INSTRUMENT,
        DQSeverity.BLOCKING,
    ),
    ("missing required", lambda qs: qs[1:], DQCode.MISSING_REQUIRED_QUOTE, DQSeverity.BLOCKING),
]


@pytest.mark.parametrize(("name", "mutate", "code", "severity"), QUOTE_DEFECTS, ids=[d[0] for d in QUOTE_DEFECTS])
def test_each_quote_defect_detected(
    master: InstrumentMaster, th: DQThresholds, name: str, mutate: Mut, code: DQCode, severity: DQSeverity
) -> None:
    base = clean_quotes(master)
    required = frozenset(q.instrument_key for q in base)
    rep = check_quotes(mutate(base), now=NOW, thresholds=th, master=master, required_keys=required)
    hits = [i for i in rep.issues if i.code is code]
    assert hits, f"{name}: {code} not detected; got {rep.codes()}"
    assert hits[0].severity is severity
    if severity is DQSeverity.BLOCKING:
        assert not rep.trustworthy


def test_non_required_instrument_defects_are_warnings(master: InstrumentMaster, th: DQThresholds) -> None:
    qs = clean_quotes(master)
    qs[0] = dataclasses.replace(qs[0], oi=None)
    rep = check_quotes(qs, now=NOW, thresholds=th, master=master)
    assert rep.codes()[DQCode.MISSING_OI] == 1 and rep.trustworthy


def test_expired_contract_quote_is_blocking(master: InstrumentMaster, th: DQThresholds) -> None:
    later = datetime(2026, 10, 7, 10, 0, tzinfo=IST)
    q = dataclasses.replace(
        clean_quotes(master)[0],
        exchange_ts=later - timedelta(seconds=1),
        receive_ts=later - timedelta(milliseconds=800),
    )
    rep = check_quotes([q], now=later, thresholds=th, master=master)
    assert DQCode.EXPIRED_CONTRACT in rep.codes() and not rep.trustworthy


def test_quote_input_errors() -> None:
    with pytest.raises(DataQualityInputError):
        Quote("k", datetime(2026, 10, 1, 10, 0), NOW, None, None, None, None, None, None)
    with pytest.raises(DataQualityInputError):
        Quote("k", NOW, NOW, 100.5, None, None, None, None, None)  # type: ignore[arg-type]


def test_now_must_be_aware(master: InstrumentMaster, th: DQThresholds) -> None:
    with pytest.raises(DataQualityInputError):
        check_quotes(clean_quotes(master), now=datetime(2026, 10, 1, 10, 0), thresholds=th)


# ---- bars ----
KEY = "NSE_FO|FUT"


def clean_bars(d: date, n: int) -> list[Bar]:
    start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
    return [
        Bar(KEY, start + timedelta(minutes=i), D("100"), D("101"), D("99.5"), D("100.5"), 650, 1000) for i in range(n)
    ]


def test_clean_day_after_aug_2026_has_385_bars(cal: SessionCalendar, th: DQThresholds) -> None:
    rep = check_bars(clean_bars(date(2026, 9, 29), 385), trading_date=date(2026, 9, 29), calendar=cal, thresholds=th)
    assert rep.issues == ()


def test_clean_day_before_aug_2026_has_375_bars(cal: SessionCalendar, th: DQThresholds) -> None:
    rep = check_bars(clean_bars(date(2026, 7, 31), 375), trading_date=date(2026, 7, 31), calendar=cal, thresholds=th)
    assert rep.issues == ()
    # the same 385-bar day would put 10 bars outside the older session
    rep2 = check_bars(clean_bars(date(2026, 7, 31), 385), trading_date=date(2026, 7, 31), calendar=cal, thresholds=th)
    assert rep2.codes()[DQCode.OUT_OF_SESSION_BAR] == 10


def test_missing_candles_small_gap_warns_large_gap_blocks(cal: SessionCalendar, th: DQThresholds) -> None:
    d = date(2026, 9, 29)
    bars = clean_bars(d, 385)
    one_gap = bars[:100] + bars[102:]  # 2/385 = 0.52% < 1%
    r1 = check_bars(one_gap, trading_date=d, calendar=cal, thresholds=th)
    miss = [i for i in r1.issues if i.code is DQCode.MISSING_CANDLE]
    assert len(miss) == 1 and miss[0].severity is DQSeverity.WARNING and "2 missing" in miss[0].detail
    big_gap = bars[:100] + bars[110:]  # 10/385 = 2.6% > 1%
    r2 = check_bars(big_gap, trading_date=d, calendar=cal, thresholds=th)
    assert not r2.trustworthy


def test_bar_defects(cal: SessionCalendar, th: DQThresholds) -> None:
    d = date(2026, 9, 29)
    bars = clean_bars(d, 385)
    dup = [*bars, bars[5]]
    conflict = [*bars, dataclasses.replace(bars[5], close=D("100.75"))]
    bad = [dataclasses.replace(bars[0], high=D("99")), *bars[1:]]
    misaligned = [*bars, dataclasses.replace(bars[0], start=bars[0].start + timedelta(seconds=30))]
    assert check_bars(dup, trading_date=d, calendar=cal, thresholds=th).codes()[DQCode.DUPLICATE_BAR] == 1
    rc = check_bars(conflict, trading_date=d, calendar=cal, thresholds=th)
    assert rc.codes()[DQCode.CONFLICTING_DUPLICATE_BAR] == 1 and not rc.trustworthy
    assert not check_bars(bad, trading_date=d, calendar=cal, thresholds=th).trustworthy
    assert DQCode.MISALIGNED_BAR in check_bars(misaligned, trading_date=d, calendar=cal, thresholds=th).codes()


def test_bars_must_be_single_instrument(cal: SessionCalendar, th: DQThresholds) -> None:
    d = date(2026, 9, 29)
    bars = [*clean_bars(d, 3), dataclasses.replace(clean_bars(d, 1)[0], instrument_key="other")]
    with pytest.raises(DataQualityInputError):
        check_bars(bars, trading_date=d, calendar=cal, thresholds=th)


def test_empty_bars_all_missing_is_blocking(cal: SessionCalendar, th: DQThresholds) -> None:
    rep = check_bars([], trading_date=date(2026, 9, 29), calendar=cal, thresholds=th)
    assert not rep.trustworthy


# ---- lot-size change via instrument master ----
def test_lot_size_change_is_blocking_dq_issue(master: InstrumentMaster, th: DQThresholds) -> None:
    old = InstrumentMaster(
        [dataclasses.replace(c, lot_size=75) for c in master.contracts],
        as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST),
        source="old",
        source_sha256="x",
    )
    rep = check_instrument_master_change(old, master, "NIFTY", thresholds=th)
    assert not rep.trustworthy
    assert any("LOT_SIZE_CHANGED" in i.detail for i in rep.issues)


def test_thresholds_config_rejects_floats(tmp_path: Path) -> None:
    p = tmp_path / "t.toml"
    p.write_text(
        'version="x"\nstale_quote_seconds=5.0\nstale_exchange_ts_seconds="30"\nmax_clock_drift_seconds="2"\n'
        'future_tolerance_seconds="1"\nabnormal_spread_min_ticks=20\nabnormal_spread_pct_of_mid="0.25"\n'
        'max_missing_candle_fraction="0.01"\n'
    )
    with pytest.raises(ConfigError):
        load_thresholds(p)

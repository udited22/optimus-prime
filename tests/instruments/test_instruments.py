"""Instrument master tests on REAL public fixture data (see tests/fixtures/instruments/README.md)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.core_types import OptionRight
from project100c.errors import (
    InstrumentMasterError,
    LotSizeAmbiguityError,
    StaleInstrumentMasterError,
)
from project100c.instruments import (
    ChangeKind,
    Contract,
    ExpiryClass,
    InstrumentKind,
    InstrumentMaster,
    Severity,
    cross_check,
    diff_masters,
    next_expiry_check,
    parse_kite_csv,
    parse_upstox_json,
)
from project100c.sessions import IST

AS_OF = datetime(2026, 9, 30, 23, 27, tzinfo=IST)
NIFTY = frozenset({"NIFTY"})


@pytest.fixture(scope="module")
def upstox_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "instruments" / "upstox_NSE_subset_20260930.json"


@pytest.fixture(scope="module")
def kite_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "instruments" / "kite_NFO_subset_20260930.csv"


@pytest.fixture(scope="module")
def up(upstox_path: Path) -> InstrumentMaster:
    m, _ = parse_upstox_json(upstox_path, as_of=AS_OF, underlyings=NIFTY)
    return m


def test_parse_report_counts_every_skip(upstox_path: Path) -> None:
    m, rep = parse_upstox_json(upstox_path, as_of=AS_OF, underlyings=NIFTY)
    assert rep.rows_total == 82 and rep.rows_used == 77 == len(m)
    assert rep.skipped == {"segment_not_NSE_FO": 3, "underlying_filtered": 2}
    assert rep.rows_used + sum(rep.skipped.values()) == rep.rows_total


def test_lot_size_derived_not_hardcoded(up: InstrumentMaster) -> None:
    assert up.lot_size("NIFTY") == 65
    assert set(up.lot_sizes_by_expiry("NIFTY").values()) == {frozenset({65})}


def test_expiries_and_classification(up: InstrumentMaster) -> None:
    assert up.option_expiries("NIFTY") == (
        date(2026, 10, 6),
        date(2026, 10, 13),
        date(2026, 10, 27),
        date(2026, 11, 23),
        date(2026, 12, 29),
        date(2027, 3, 30),
    )
    assert up.future_expiries("NIFTY") == (date(2026, 10, 27), date(2026, 11, 23), date(2026, 12, 29))
    assert up.classify_expiry("NIFTY", date(2026, 10, 6)) is ExpiryClass.WEEKLY
    assert up.classify_expiry("NIFTY", date(2026, 10, 27)) is ExpiryClass.MONTHLY
    assert up.classify_expiry("NIFTY", date(2026, 11, 23)) is ExpiryClass.MONTHLY  # Monday: holiday-shifted
    assert up.classify_expiry("NIFTY", date(2027, 3, 30)) is ExpiryClass.LONG_DATED
    assert up.weekly_expiries("NIFTY") == (date(2026, 10, 6), date(2026, 10, 13))
    # Our derivation agrees with Upstox's own 'weekly' flag on every contract.
    assert up.weekly_flag_disagreements("NIFTY") == ()


def test_weekly_expiry_weekday_is_tuesday_in_fixture(up: InstrumentMaster) -> None:
    # Tuesday weekly expiry (since 1-Sep-2025). Derived from data; no weekday is hard-coded in the module.
    assert {e.weekday() for e in up.weekly_expiries("NIFTY")} == {1}


def test_nearest_expiry(up: InstrumentMaster) -> None:
    assert up.nearest_option_expiry("NIFTY", date(2026, 9, 30)) == date(2026, 10, 6)
    assert up.nearest_option_expiry("NIFTY", date(2026, 9, 30), min_days=7) == date(2026, 10, 13)
    assert up.nearest_option_expiry("NIFTY", date(2026, 10, 6)) == date(2026, 10, 6)  # 0DTE


def test_ticks_strikes(up: InstrumentMaster) -> None:
    assert up.tick_size("NIFTY", InstrumentKind.OPTION) == Decimal("0.05")
    assert up.tick_size("NIFTY", InstrumentKind.FUTURE) == Decimal("0.1")
    assert up.min_strike_step("NIFTY", date(2026, 10, 6)) == Decimal(50)
    ks = up.strikes("NIFTY", date(2026, 10, 6), OptionRight.CE)
    assert ks[0] == Decimal(22500) and ks[-1] == Decimal(22950) and len(ks) == 10
    c = up.option("NIFTY", date(2026, 10, 6), Decimal(22700), OptionRight.CE)
    assert c.lot_size == 65 and c.freeze_qty == 1755 and c.instrument_key.startswith("NSE_FO|")


def test_cross_check_upstox_vs_kite_confirms_paise_tick_units(up: InstrumentMaster, kite_path: Path) -> None:
    kite, rep = parse_kite_csv(kite_path, as_of=AS_OF, underlyings=NIFTY)
    assert rep.rows_used == 77
    mismatches = cross_check(up, kite, "NIFTY", strict=True)  # raises on any attribute mismatch
    assert mismatches == []


def test_stale_master_rejected(upstox_path: Path) -> None:
    with pytest.raises(StaleInstrumentMasterError):
        parse_upstox_json(upstox_path, as_of=datetime(2026, 10, 7, 9, 0, tzinfo=IST), underlyings=NIFTY)


def test_naive_as_of_rejected(upstox_path: Path) -> None:
    with pytest.raises(InstrumentMasterError):
        parse_upstox_json(upstox_path, as_of=datetime(2026, 9, 30, 23, 0), underlyings=NIFTY)


# ---- corrupt input: explicit errors, never silent ----
def _mutated(tmp_path: Path, upstox_path: Path, fn: object) -> Path:
    rows = json.loads(upstox_path.read_text())
    fn(rows)  # type: ignore[operator]
    p = tmp_path / "u.json"
    p.write_text(json.dumps(rows))
    return p


def _first_nifty(rows: list[dict[str, object]]) -> dict[str, object]:
    return next(r for r in rows if r.get("underlying_symbol") == "NIFTY" and r.get("segment") == "NSE_FO")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: _first_nifty(rows).pop("lot_size"),
        lambda rows: _first_nifty(rows).__setitem__("lot_size", 65.5),
        lambda rows: _first_nifty(rows).__setitem__("tick_size", 0),
        lambda rows: _first_nifty(rows).__setitem__("expiry", "soon"),
        lambda rows: rows.append(dict(_first_nifty(rows))),  # duplicate instrument key
    ],
)
def test_corrupt_rows_raise(tmp_path: Path, upstox_path: Path, mutation: object) -> None:
    with pytest.raises(InstrumentMasterError):
        parse_upstox_json(_mutated(tmp_path, upstox_path, mutation), as_of=AS_OF, underlyings=NIFTY)


def test_bad_json_and_bad_kite_header(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("{not json")
    with pytest.raises(InstrumentMasterError):
        parse_upstox_json(p, as_of=AS_OF)
    k = tmp_path / "k.csv"
    k.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(InstrumentMasterError):
        parse_kite_csv(k, as_of=AS_OF)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InstrumentMasterError):
        parse_upstox_json(tmp_path / "nope.json", as_of=AS_OF)


# ---- lot-size ambiguity & diff detector (D-04 acceptance tests) ----
def _with(master: InstrumentMaster, contracts: Iterable[Contract], as_of: datetime | None = None) -> InstrumentMaster:
    return InstrumentMaster(contracts, as_of=as_of or master.as_of, source="synthetic", source_sha256="x")


def test_mixed_lot_sizes_are_ambiguous(up: InstrumentMaster) -> None:
    mixed = [dataclasses.replace(c, lot_size=75) if c.expiry == date(2026, 10, 6) else c for c in up.contracts]
    m = _with(up, mixed)
    with pytest.raises(LotSizeAmbiguityError):
        m.lot_size("NIFTY")
    assert m.lot_size("NIFTY", date(2026, 10, 6)) == 75
    assert m.lot_size("NIFTY", date(2026, 10, 27)) == 65


def test_synthetic_lot_change_75_to_65_is_blocking(up: InstrumentMaster) -> None:
    old = _with(
        up, [dataclasses.replace(c, lot_size=75) for c in up.contracts], as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST)
    )
    changes = diff_masters(old, up, "NIFTY")
    kinds = {c.kind: c.severity for c in changes}
    assert kinds[ChangeKind.LOT_SIZE_CHANGED] is Severity.BLOCKING
    assert kinds[ChangeKind.LOT_SIZE_SET_CHANGED] is Severity.BLOCKING


def test_tick_and_freeze_changes(up: InstrumentMaster) -> None:
    old = _with(
        up,
        [dataclasses.replace(c, tick_size=Decimal("0.10"), freeze_qty=1800) for c in up.contracts],
        as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST),
    )
    kinds = {c.kind: c.severity for c in diff_masters(old, up, "NIFTY")}
    assert kinds[ChangeKind.TICK_SIZE_CHANGED] is Severity.BLOCKING
    assert kinds[ChangeKind.FREEZE_QTY_CHANGED] is Severity.WARNING


def test_missing_next_expiry_detected(up: InstrumentMaster) -> None:
    no_weeklies = [c for c in up.contracts if c.expiry not in (date(2026, 10, 6), date(2026, 10, 13))]
    m = _with(up, no_weeklies)
    chk = next_expiry_check(m, "NIFTY")
    assert chk is not None and chk.severity is Severity.BLOCKING
    assert next_expiry_check(up, "NIFTY") is None
    old = _with(up, list(up.contracts), as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST))
    kinds = {c.kind: c.severity for c in diff_masters(old, m, "NIFTY")}
    assert kinds[ChangeKind.EXPIRY_REMOVED_EARLY] is Severity.BLOCKING
    assert kinds[ChangeKind.NEXT_EXPIRY_MISSING] is Severity.BLOCKING


def test_expiry_added_and_as_of_regression(up: InstrumentMaster) -> None:
    fewer = _with(
        up, [c for c in up.contracts if c.expiry != date(2027, 3, 30)], as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST)
    )
    kinds = {c.kind: c.severity for c in diff_masters(fewer, up, "NIFTY")}
    assert kinds[ChangeKind.EXPIRY_ADDED] is Severity.INFO
    same_time = diff_masters(up, up, "NIFTY")
    assert any(c.kind is ChangeKind.AS_OF_NOT_ADVANCING and c.severity is Severity.BLOCKING for c in same_time)


def test_no_changes_between_identical_masters_except_time(up: InstrumentMaster) -> None:
    older = _with(up, list(up.contracts), as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST))
    assert diff_masters(older, up, "NIFTY") == []


def test_underlying_missing_is_blocking(up: InstrumentMaster, upstox_path: Path) -> None:
    bank, _ = parse_upstox_json(upstox_path, as_of=AS_OF, underlyings=frozenset({"BANKNIFTY"}))
    older = _with(up, list(up.contracts), as_of=datetime(2026, 9, 29, 23, 0, tzinfo=IST))
    changes = diff_masters(older, bank, "NIFTY")
    assert changes[-1].kind is ChangeKind.UNDERLYING_MISSING and changes[-1].severity is Severity.BLOCKING

"""Ingest DQ for US bars (daily and 1-minute) and Cboe index series. Nothing is filled or "fixed".

Daily (one symbol, one calendar year):
  BLOCKING: conflicting duplicate session dates, impossible OHLC (exact Decimal: high < max(open, close),
  low > min(open, close), low > high), a price <= 0, negative volume, a row with only some values null, a
  weekend session date, a bar on an NYSE holiday (years the NYSE book covers).
  WARNING: identical duplicates (collapsed), rows out of order (sorted), all-null rows (dropped and counted),
  zero volume (equities and ETFs only; index volume is not a trade count), session dates the reference symbol
  (SPY) lacks, and reference dates inside the symbol's span that it lacks (MISSING_DAY; listed).
  INFO: VALUE_ROUNDED when a value had more than 10 decimals.
1-minute (one symbol, one NYSE session; the session must be covered by the NYSE book):
  BLOCKING: a bar not on a whole minute, conflicting duplicates, impossible OHLC, price <= 0, negative volume,
  partially null rows, any bar on a day that is not an NYSE trading day.
  WARNING: identical duplicates, out of order, all-null rows (dropped), bars outside the regular session
  [09:30, close) (kept; flagged), zero-volume bars, missing minutes (gap runs listed) and COVERAGE_GAP when more
  than ``max_missing_fraction`` of the session's minutes are missing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from project100c.data.us.config import NyseSessions
from project100c.dq.checks import DQCode, DQIssue, DQReport, DQSeverity

DQ_VERSION = "us-dq-2026-10-03.1"
MAX_LISTED = 40
_PRICE_COLS = ("open", "high", "low", "close")


@dataclass(slots=True)
class CheckResult:
    table: pa.Table
    report: DQReport
    stats: dict[str, Any] = field(default_factory=dict)


class _Issues:
    def __init__(self, key: str) -> None:
        self.key = key
        self.items: list[DQIssue] = []

    def add(self, code: DQCode, sev: DQSeverity, ts: datetime | None, detail: str) -> None:
        self.items.append(DQIssue(code, sev, self.key, ts, detail))

    def report(self, checked: int, thresholds_version: str) -> DQReport:
        return DQReport(tuple(self.items), checked, f"{DQ_VERSION}+{thresholds_version}")


def _count(mask: Any) -> int:
    return int(pc.sum(pc.cast(pc.fill_null(mask, False), pa.int64())).as_py() or 0)


def _first(t: pa.Table, mask: Any, col: str = "ts") -> Any:
    idx = pc.indices_nonzero(pc.fill_null(mask, False))
    return None if len(idx) == 0 else t[col][idx[0].as_py()].as_py()


def _as_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    return None


def _structure(t: pa.Table, key_col: str, iss: _Issues) -> pa.Table:
    """Sort, collapse identical duplicates (WARNING), flag conflicting ones (BLOCKING)."""
    n = t.num_rows
    if n == 0:
        return t
    idx = pc.sort_indices(t, sort_keys=[(key_col, "ascending")])
    if not pc.all(pc.equal(idx, pa.array(range(n), pa.uint64()))).as_py():
        iss.add(DQCode.NON_MONOTONIC_TS, DQSeverity.WARNING, None, "rows out of time order (sorted)")
    t = t.take(idx)
    keys = t[key_col].to_pylist()
    rows = t.to_pylist()
    keep: list[int] = []
    dup = conflict = 0
    first_conflict: Any = None
    for i in range(n):
        if keep and keys[i] == keys[keep[-1]]:
            if rows[i] == rows[keep[-1]]:
                dup += 1
            else:
                conflict += 1
                first_conflict = first_conflict or keys[i]
            continue
        keep.append(i)
    if dup:
        iss.add(DQCode.DUPLICATE_BAR, DQSeverity.WARNING, None, f"{dup} identical duplicate row(s) collapsed")
    if conflict:
        iss.add(
            DQCode.CONFLICTING_DUPLICATE_BAR,
            DQSeverity.BLOCKING,
            _as_ts(first_conflict),
            f"{conflict} duplicate {key_col}(s) with different values",
        )
    return t.take(pa.array(keep, pa.int64())) if len(keep) != n else t


def _values(t: pa.Table, iss: _Issues, *, ohlc_required: bool, zero_volume_warn: bool) -> None:
    if t.num_rows == 0:
        return
    tcol = "ts" if "ts" in t.column_names else "session_date"
    if ohlc_required:
        o, h, lo, c = (t[k] for k in _PRICE_COLS)
        bad = pc.or_(
            pc.or_(pc.less(h, o), pc.less(h, c)),
            pc.or_(pc.or_(pc.greater(lo, o), pc.greater(lo, c)), pc.greater(lo, h)),
        )
        if (nbad := _count(bad)) > 0:
            iss.add(
                DQCode.BAD_OHLC,
                DQSeverity.BLOCKING,
                _as_ts(_first(t, bad, tcol)),
                f"{nbad} bar(s) with impossible OHLC",
            )
    cols = [k for k in _PRICE_COLS if pc.any(pc.is_valid(t[k])).as_py()]
    for k in cols:
        nonpos = pc.less_equal(t[k], pa.scalar(Decimal(0), t.schema.field(k).type))
        if (n := _count(nonpos)) > 0:
            iss.add(
                DQCode.ZERO_OR_NEGATIVE_PRICE, DQSeverity.BLOCKING, _as_ts(_first(t, nonpos, tcol)), f"{n} {k} <= 0"
            )
    if "volume" in t.column_names:
        neg = pc.less(t["volume"], 0)
        if (n := _count(neg)) > 0:
            iss.add(DQCode.NEGATIVE_SIZE, DQSeverity.BLOCKING, _as_ts(_first(t, neg, tcol)), f"{n} negative volume")
        if zero_volume_warn:
            zero = pc.equal(t["volume"], 0)
            if (n := _count(zero)) > 0:
                iss.add(
                    DQCode.ZERO_VOLUME_BAR, DQSeverity.WARNING, _as_ts(_first(t, zero, tcol)), f"{n} zero-volume bar(s)"
                )


def _list(ds: Iterable[date]) -> str:
    xs = sorted(ds)
    s = ", ".join(d.isoformat() for d in xs[:MAX_LISTED])
    return s + (f" (+{len(xs) - MAX_LISTED} more)" if len(xs) > MAX_LISTED else "")


def check_daily(
    t: pa.Table,
    *,
    key: str,
    is_index: bool,
    all_null_rows: int,
    partial_null: int,
    rounded: int,
    reference_dates: frozenset[date] | None,
    reference_span: tuple[date, date] | None,
    nyse: NyseSessions,
    thresholds_version: str,
) -> CheckResult:
    """``reference_dates``: the reference symbol's session dates (None when checking the reference itself);
    only dates inside ``reference_span`` (the reference's own first..last date) are compared."""
    iss = _Issues(key)
    n0 = t.num_rows
    if all_null_rows:
        iss.add(DQCode.NULL_VALUE, DQSeverity.WARNING, None, f"{all_null_rows} all-null row(s) dropped")
    if partial_null:
        iss.add(DQCode.NULL_VALUE, DQSeverity.BLOCKING, None, f"{partial_null} row(s) with some values null")
    if rounded:
        iss.add(DQCode.VALUE_ROUNDED, DQSeverity.INFO, None, f"{rounded} value(s) rounded to 10 decimals")
    t = _structure(t, "session_date", iss)
    _values(t, iss, ohlc_required=True, zero_volume_warn=not is_index)
    dates: list[date] = t["session_date"].to_pylist()
    weekend = [d for d in dates if d.weekday() >= 5]
    if weekend:
        iss.add(
            DQCode.NON_TRADING_DAY_BAR, DQSeverity.BLOCKING, _as_ts(weekend[0]), f"weekend bar(s): {_list(weekend)}"
        )
    holiday = [d for d in dates if nyse.covers(d) and d.weekday() < 5 and not nyse.is_trading_day(d)]
    if holiday:
        iss.add(
            DQCode.NON_TRADING_DAY_BAR,
            DQSeverity.BLOCKING,
            _as_ts(holiday[0]),
            f"NYSE holiday bar(s): {_list(holiday)}",
        )
    stats: dict[str, Any] = {"rows_in": n0, "rows_out": t.num_rows}
    if reference_dates is not None and dates:
        lo, hi = dates[0], dates[-1]
        if reference_span is not None:
            lo, hi = max(lo, reference_span[0]), min(hi, reference_span[1])
        mine = set(dates)
        ref = {d for d in reference_dates if lo <= d <= hi}
        extra = sorted(d for d in mine if lo <= d <= hi and d not in reference_dates)
        missing = sorted(ref - mine)
        if extra:
            iss.add(
                DQCode.SPECIAL_SESSION_BAR,
                DQSeverity.WARNING,
                _as_ts(extra[0]),
                f"dates the reference lacks: {_list(extra)}",
            )
        if missing:
            iss.add(
                DQCode.MISSING_DAY,
                DQSeverity.WARNING,
                _as_ts(missing[0]),
                f"{len(missing)} reference date(s) missing: {_list(missing)}",
            )
        stats |= {"missing_vs_reference": len(missing), "extra_vs_reference": len(extra)}
    return CheckResult(t, iss.report(n0, thresholds_version), stats)


def check_minute(
    t: pa.Table,
    *,
    key: str,
    day: date,
    is_index: bool,
    all_null_rows: int,
    partial_null: int,
    rounded: int,
    nyse: NyseSessions,
    max_missing_fraction: Decimal,
    thresholds_version: str,
) -> CheckResult:
    iss = _Issues(key)
    n0 = t.num_rows
    if all_null_rows:
        iss.add(DQCode.NULL_VALUE, DQSeverity.WARNING, None, f"{all_null_rows} all-null row(s) dropped")
    if partial_null:
        iss.add(DQCode.NULL_VALUE, DQSeverity.BLOCKING, None, f"{partial_null} row(s) with some values null")
    if rounded:
        iss.add(DQCode.VALUE_ROUNDED, DQSeverity.INFO, None, f"{rounded} value(s) rounded to 10 decimals")
    trading = nyse.is_trading_day(day)  # raises CalendarCoverageError outside the book
    t = _structure(t, "ts", iss)
    stats: dict[str, Any] = {"rows_in": n0, "rows_out": t.num_rows}
    if t.num_rows == 0:
        return CheckResult(t, iss.report(n0, thresholds_version), stats)
    if not trading:
        iss.add(DQCode.NON_TRADING_DAY_BAR, DQSeverity.BLOCKING, t["ts"][0].as_py(), f"{t.num_rows} bar(s) on {day}")
        return CheckResult(t, iss.report(n0, thresholds_version), stats)
    ts_i = pc.cast(t["ts"], pa.int64())
    mis = pc.not_equal(pc.multiply(pc.divide(ts_i, 60_000_000_000), 60_000_000_000), ts_i)
    if (k := _count(mis)) > 0:
        iss.add(DQCode.MISALIGNED_BAR, DQSeverity.BLOCKING, _first(t, mis), f"{k} bar(s) off the minute")
    _values(t, iss, ohlc_required=True, zero_volume_warn=not is_index)
    s_open, s_close = nyse.session(day)
    lo = int(s_open.astimezone(UTC).timestamp()) * 1_000_000_000
    hi = int(s_close.astimezone(UTC).timestamp()) * 1_000_000_000
    outside = pc.or_(pc.less(ts_i, lo), pc.greater_equal(ts_i, hi))
    if (k := _count(outside)) > 0:
        iss.add(
            DQCode.OUT_OF_SESSION_BAR,
            DQSeverity.WARNING,
            _first(t, outside),
            f"{k} bar(s) outside {s_open.time()}-{s_close.time()} ET",
        )
    step = 60_000_000_000
    present = sorted({v for v in ts_i.to_pylist() if lo <= v < hi})
    expected = (hi - lo) // step
    runs: list[tuple[int, int]] = []
    cur = lo
    for v in [*present, hi]:
        if v > cur:
            runs.append((cur, (v - cur) // step))
        cur = v + step
    for start, length in sorted(runs, key=lambda r: -r[1])[:MAX_LISTED]:
        iss.add(
            DQCode.MISSING_CANDLE,
            DQSeverity.WARNING,
            datetime.fromtimestamp(start / 1e9, UTC),
            f"{length} missing minute(s) from {datetime.fromtimestamp(start / 1e9, UTC).isoformat()}",
        )
    missing = expected - len(present)
    if expected and Decimal(missing) / Decimal(expected) > max_missing_fraction:
        iss.add(DQCode.COVERAGE_GAP, DQSeverity.WARNING, None, f"{missing}/{expected} session minutes missing")
    stats |= {
        "expected_minutes": expected,
        "present_minutes": len(present),
        "missing_minutes": missing,
        "gap_runs": len(runs),
    }
    return CheckResult(t, iss.report(n0, thresholds_version), stats)


def check_index_series(t: pa.Table, *, key: str, rounded: int, thresholds_version: str) -> CheckResult:
    """Cboe daily index history: structure, OHLC where present, positivity, weekend dates."""
    iss = _Issues(key)
    n0 = t.num_rows
    if rounded:
        iss.add(DQCode.VALUE_ROUNDED, DQSeverity.INFO, None, f"{rounded} value(s) rounded to 10 decimals")
    t = _structure(t, "session_date", iss)
    has_ohlc = t.num_rows > 0 and pc.all(pc.is_valid(t["open"])).as_py()
    if t.num_rows and not has_ohlc and pc.any(pc.is_valid(t["open"])).as_py():
        iss.add(DQCode.NULL_VALUE, DQSeverity.BLOCKING, None, "OPEN/HIGH/LOW present on some rows only")
    nulls = pc.is_null(t["close"]) if t.num_rows else None
    if nulls is not None and (k := _count(nulls)) > 0:
        iss.add(DQCode.NULL_VALUE, DQSeverity.BLOCKING, _as_ts(_first(t, nulls, "session_date")), f"{k} null close(s)")
    _values(t, iss, ohlc_required=bool(has_ohlc), zero_volume_warn=False)
    weekend = [d for d in t["session_date"].to_pylist() if d.weekday() >= 5]
    if weekend:
        iss.add(
            DQCode.NON_TRADING_DAY_BAR, DQSeverity.WARNING, _as_ts(weekend[0]), f"weekend date(s): {_list(weekend)}"
        )
    return CheckResult(t, iss.report(n0, thresholds_version), {"rows_in": n0, "rows_out": t.num_rows})


def report_status(report: DQReport) -> str:
    if not report.trustworthy:
        return "BLOCKED"
    return "WARN" if any(i.severity is DQSeverity.WARNING for i in report.issues) else "PASS"


__all__ = [
    "DQ_VERSION",
    "CheckResult",
    "check_daily",
    "check_index_series",
    "check_minute",
    "report_status",
]

"""Ingest data-quality checks for 24x7 crypto archive files. Nothing is filled, interpolated or fixed.

Per file (one month or one day of one symbol):

BLOCKING (the file goes to ``quarantine/``): a bar not on a whole minute, a bar outside the file's period,
conflicting duplicate rows (same minute, different values), a bad OHLC relation (exact Decimal comparison:
high < max(open, close), low > min(open, close) or low > high), a price <= 0, a negative volume or count,
and for rate/metric files a null or out-of-period value.

WARNING (kept in ``clean/``, recorded in the manifest): identical duplicate rows (collapsed to one), rows out of
time order (sorted), zero-volume bars, a close time that is not open + 1 minute - 1 unit, missing minutes
(each gap run is listed), and UTC days whose missing fraction exceeds ``max_missing_fraction`` (the NIFTY rule's
1%: such days are listed in ``days_over_threshold`` so readers exclude them; the bars themselves stay).

INFO: ``LISTING_BOUNDARY`` when the symbol's first (or last) archive file starts after (or ends before) the
period edge: minutes before the listing or after a delisting are not "missing" and are never counted as gaps.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from project100c.dq.checks import DQCode, DQIssue, DQReport, DQSeverity

DQ_VERSION = "crypto-dq-2026-10-03.1"
_NS_MIN = 60_000_000_000
_NS_DAY = 86_400_000_000_000
MAX_GAP_ISSUES = 50


@dataclass(slots=True)
class CheckResult:
    table: pa.Table
    report: DQReport
    stats: dict[str, Any] = field(default_factory=dict)


def _ns(dt: datetime) -> int:
    return int((dt.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1)) * 1000


def _dt(ns: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)


def _count(mask: Any) -> int:
    v = pc.sum(pc.cast(mask, pa.int64())).as_py()
    return int(v or 0)


def _first_ts(table: pa.Table, mask: Any) -> datetime | None:
    idx = pc.indices_nonzero(pc.fill_null(mask, False))
    if len(idx) == 0:
        return None
    v = table["ts"][idx[0].as_py()].as_py()
    return v if isinstance(v, datetime) else None


class _Issues:
    def __init__(self, key: str) -> None:
        self.key = key
        self.items: list[DQIssue] = []

    def add(self, code: DQCode, sev: DQSeverity, ts: datetime | None, detail: str) -> None:
        self.items.append(DQIssue(code, sev, self.key, ts, detail))


def _structure(t: pa.Table, raw_row_key: pa.Array | None, start: datetime, end: datetime, iss: _Issues) -> pa.Table:
    """Shared checks (alignment, window, order, duplicates). Returns the table sorted with identical dups collapsed."""
    n = t.num_rows
    if n == 0:
        return t
    ts_i = pc.cast(t["ts"], pa.int64())
    outside = pc.or_(pc.less(ts_i, _ns(start)), pc.greater_equal(ts_i, _ns(end)))
    if (k := _count(outside)) > 0:
        iss.add(DQCode.OUTSIDE_REQUEST_WINDOW, DQSeverity.BLOCKING, _first_ts(t, outside), f"{k} row(s) outside period")
    arr = ts_i.combine_chunks() if isinstance(ts_i, pa.ChunkedArray) else ts_i
    if n > 1:
        back = pc.less_equal(arr.slice(1), arr.slice(0, n - 1))
        if (k := _count(back)) > 0:
            iss.add(DQCode.NON_MONOTONIC_TS, DQSeverity.WARNING, None, f"{k} row(s) not after the previous (sorted)")
    t = t.append_column("_k", raw_row_key) if raw_row_key is not None else t
    t = t.sort_by("ts")
    grp = t.group_by("ts").aggregate(
        [("ts", "count")] + ([("_k", "count_distinct")] if raw_row_key is not None else [])
    )
    dup = pc.greater(grp["ts_count"], 1)
    n_dup_minutes = _count(dup)
    if n_dup_minutes:
        if raw_row_key is not None:
            conflict = pc.and_(dup, pc.greater(grp["_k_count_distinct"], 1))
            if (k := _count(conflict)) > 0:
                first = grp.filter(conflict)["ts"][0].as_py()
                iss.add(
                    DQCode.CONFLICTING_DUPLICATE_BAR, DQSeverity.BLOCKING, first, f"{k} minute(s) with differing rows"
                )
        extra = int(pc.sum(grp["ts_count"]).as_py() or 0) - grp.num_rows
        iss.add(
            DQCode.DUPLICATE_BAR, DQSeverity.WARNING, None, f"{n_dup_minutes} minute(s), {extra} extra row(s) collapsed"
        )
        keep: list[int] = []
        prev: int | None = None
        for i, v in enumerate(pc.cast(t["ts"], pa.int64()).to_pylist()):
            if v != prev:
                keep.append(i)
            prev = v
        t = t.take(pa.array(keep, pa.int64()))
    if raw_row_key is not None:
        t = t.drop_columns(["_k"])
    return t


def _gaps(
    ts_sorted: list[int],
    lo: int,
    hi: int,
    step: int,
    max_missing_fraction: Decimal,
    iss: _Issues,
    *,
    per_day_expected: int,
) -> dict[str, Any]:
    """Missing slots in [lo, hi) on a ``step`` grid; gap runs and UTC days above the threshold."""
    present = [v for v in ts_sorted if lo <= v < hi and (v - lo) % step == 0]
    expected = max(0, (hi - lo) // step)
    runs: list[tuple[int, int]] = []
    cur = lo
    for v in present:
        if v > cur:
            runs.append((cur, (v - cur) // step))
        cur = v + step
    if hi > cur:
        runs.append((cur, (hi - cur) // step))
    missing = expected - len(present)
    by_day = Counter(v // _NS_DAY for v in present)
    days_over: list[str] = []
    d = lo // _NS_DAY
    while d * _NS_DAY < hi:
        d0, d1 = max(lo, d * _NS_DAY), min(hi, (d + 1) * _NS_DAY)
        exp = (d1 - d0) // step if (d1 - d0) < _NS_DAY else per_day_expected
        miss = exp - by_day.get(d, 0)
        if exp > 0 and Decimal(miss) / Decimal(exp) > max_missing_fraction:
            days_over.append(_dt(d * _NS_DAY).date().isoformat())
        d += 1
    for start, length in sorted(runs, key=lambda r: -r[1])[:MAX_GAP_ISSUES]:
        iss.add(DQCode.MISSING_CANDLE, DQSeverity.WARNING, _dt(start), f"{length} missing slot(s) from {_dt(start)}")
    if len(runs) > MAX_GAP_ISSUES:
        iss.add(DQCode.MISSING_CANDLE, DQSeverity.WARNING, None, f"{len(runs) - MAX_GAP_ISSUES} more gap run(s)")
    if days_over:
        iss.add(
            DQCode.COVERAGE_GAP,
            DQSeverity.WARNING,
            None,
            f"{len(days_over)} UTC day(s) above {max_missing_fraction} missing: {', '.join(days_over[:31])}",
        )
    return {
        "expected_slots": expected,
        "present_slots": len(present),
        "missing_slots": missing,
        "gap_runs": len(runs),
        "longest_gap_slots": max((r[1] for r in runs), default=0),
        "days_over_threshold": days_over,
    }


def check_klines(
    t: pa.Table,
    raw: pa.Table,
    *,
    key: str,
    period_start: datetime,
    period_end: datetime,
    expect_from: datetime | None,
    expect_to: datetime | None,
    max_missing_fraction: Decimal,
    thresholds_version: str,
) -> CheckResult:
    """``expect_from``/``expect_to``: the symbol's listing / last-bar bounds when this file holds them."""
    iss = _Issues(key)
    n0 = t.num_rows
    row_key = pc.binary_join_element_wise(*[raw[c] for c in raw.column_names[1:11]], "|") if n0 else None
    if n0:
        ts_i = pc.cast(t["ts"], pa.int64())
        misaligned = pc.not_equal(ts_i, pc.multiply(pc.divide(ts_i, _NS_MIN), _NS_MIN))  # integer division
        if (k := _count(misaligned)) > 0:
            iss.add(DQCode.MISALIGNED_BAR, DQSeverity.BLOCKING, _first_ts(t, misaligned), f"{k} bar(s) off the minute")
        o, h, lo, c = t["open"], t["high"], t["low"], t["close"]
        bad = pc.or_(
            pc.or_(pc.less(h, o), pc.less(h, c)),
            pc.or_(pc.or_(pc.greater(lo, o), pc.greater(lo, c)), pc.greater(lo, h)),
        )
        if (k := _count(bad)) > 0:
            iss.add(DQCode.BAD_OHLC, DQSeverity.BLOCKING, _first_ts(t, bad), f"{k} bar(s) with an impossible OHLC")
        zero = Decimal(0)
        nonpos = pc.or_(
            pc.or_(pc.less_equal(o, zero), pc.less_equal(h, zero)),
            pc.or_(pc.less_equal(lo, zero), pc.less_equal(c, zero)),
        )
        if (k := _count(nonpos)) > 0:
            iss.add(DQCode.ZERO_OR_NEGATIVE_PRICE, DQSeverity.BLOCKING, _first_ts(t, nonpos), f"{k} bar(s) price <= 0")
        neg = pc.or_(
            pc.or_(pc.less(t["volume"], zero), pc.less(t["quote_volume"], zero)),
            pc.or_(pc.less(t["count"], 0), pc.less(t["taker_buy_volume"], zero)),
        )
        if (k := _count(neg)) > 0:
            iss.add(DQCode.NEGATIVE_SIZE, DQSeverity.BLOCKING, _first_ts(t, neg), f"{k} bar(s) with a negative size")
        zv = pc.equal(t["volume"], zero)
        if (k := _count(zv)) > 0:
            iss.add(DQCode.ZERO_VOLUME_BAR, DQSeverity.WARNING, _first_ts(t, zv), f"{k} zero-volume bar(s)")
        span = pc.subtract(pc.cast(t["close_ts"], pa.int64()), pc.cast(t["ts"], pa.int64()))
        ok = pc.or_(pc.equal(span, _NS_MIN - 1_000_000), pc.equal(span, _NS_MIN - 1_000))
        if (k := _count(pc.invert(ok))) > 0:
            iss.add(DQCode.CLOSE_TIME_MISMATCH, DQSeverity.WARNING, _first_ts(t, pc.invert(ok)), f"{k} bar(s)")
    t = _structure(t, row_key, period_start, period_end, iss)
    lo_b = max(period_start, expect_from) if expect_from else period_start
    hi_b = min(period_end, expect_to) if expect_to else period_end
    if lo_b > period_start or hi_b < period_end:
        iss.add(
            DQCode.LISTING_BOUNDARY,
            DQSeverity.INFO,
            None,
            f"expected minutes limited to [{lo_b.isoformat()}, {hi_b.isoformat()}) (listing/last bar)",
        )
    stats = _gaps(
        pc.cast(t["ts"], pa.int64()).to_pylist() if t.num_rows else [],
        _ns(lo_b),
        _ns(hi_b),
        _NS_MIN,
        max_missing_fraction,
        iss,
        per_day_expected=1440,
    )
    stats.update(
        {
            "rows_in": n0,
            "rows_out": t.num_rows,
            "zero_volume_bars": _count(pc.equal(t["volume"], Decimal(0))) if t.num_rows else 0,
        }
    )
    return CheckResult(t, DQReport(tuple(iss.items), n0, thresholds_version), stats)


def check_series(
    t: pa.Table,
    *,
    key: str,
    value_columns: tuple[str, ...],
    nullable_columns: tuple[str, ...],
    period_start: datetime,
    period_end: datetime,
    step: timedelta | None,
    max_missing_fraction: Decimal,
    thresholds_version: str,
    nonnegative: bool,
) -> CheckResult:
    """Rate / metric files: structure, nulls, signs; slot gaps when the series has a fixed ``step``."""
    iss = _Issues(key)
    n0 = t.num_rows
    for col in value_columns:
        nulls = pc.is_null(t[col])
        if (k := _count(nulls)) > 0:
            sev = DQSeverity.WARNING if col in nullable_columns else DQSeverity.BLOCKING
            iss.add(DQCode.NULL_VALUE, sev, _first_ts(t, nulls), f"{col}: {k} null(s)")
        if nonnegative and n0:
            neg = pc.less(t[col], Decimal(0))
            if (k := _count(neg)) > 0:
                iss.add(DQCode.NEGATIVE_SIZE, DQSeverity.BLOCKING, _first_ts(t, neg), f"{col}: {k} negative")
    # rate settlements are stamped a few ms after the hour; use the minute for the duplicate key
    raw_key = pc.binary_join_element_wise(*[pc.cast(t[c], pa.string()) for c in value_columns], "|") if n0 else None
    t = _structure(t, raw_key, period_start, period_end, iss)
    stats: dict[str, Any] = {"rows_in": n0, "rows_out": t.num_rows}
    if step is not None:
        st = int(step / timedelta(microseconds=1)) * 1000
        floored = [v - v % st for v in pc.cast(t["ts"], pa.int64()).to_pylist()] if t.num_rows else []
        stats.update(
            _gaps(
                sorted(set(floored)),
                _ns(period_start),
                _ns(period_end),
                st,
                max_missing_fraction,
                iss,
                per_day_expected=_NS_DAY // st,
            )
        )
    return CheckResult(t, DQReport(tuple(iss.items), n0, thresholds_version), stats)

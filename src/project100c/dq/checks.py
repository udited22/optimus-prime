"""Data-quality checks. Each check returns issues; nothing is dropped or 'fixed' silently.

A report is *trustworthy* only if it contains no BLOCKING issue. Untrustworthy state disables entries
(directive §11). Structural caller errors (naive timestamps, float prices, wrong inputs) raise
DataQualityInputError instead of producing a report.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from project100c.errors import ConfigError, DataQualityInputError
from project100c.instruments.diff import Severity as MasterSeverity
from project100c.instruments.diff import diff_masters
from project100c.instruments.master import InstrumentMaster
from project100c.market_types import Bar, Quote
from project100c.sessions.model import IST, SessionCalendar


class DQSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class DQCode(StrEnum):
    STALE_QUOTE = "STALE_QUOTE"
    STALE_EXCHANGE_TS = "STALE_EXCHANGE_TS"
    MISSING_REQUIRED_QUOTE = "MISSING_REQUIRED_QUOTE"
    CROSSED_MARKET = "CROSSED_MARKET"
    LOCKED_MARKET = "LOCKED_MARKET"
    ONE_SIDED_MARKET = "ONE_SIDED_MARKET"
    ZERO_OR_NEGATIVE_PRICE = "ZERO_OR_NEGATIVE_PRICE"
    NEGATIVE_SIZE = "NEGATIVE_SIZE"
    MISSING_OI = "MISSING_OI"
    TIMESTAMP_DRIFT = "TIMESTAMP_DRIFT"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DUPLICATE_TICK = "DUPLICATE_TICK"
    ABNORMAL_SPREAD = "ABNORMAL_SPREAD"
    OFF_TICK_PRICE = "OFF_TICK_PRICE"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    EXPIRED_CONTRACT = "EXPIRED_CONTRACT"
    MISSING_CANDLE = "MISSING_CANDLE"
    DUPLICATE_BAR = "DUPLICATE_BAR"
    CONFLICTING_DUPLICATE_BAR = "CONFLICTING_DUPLICATE_BAR"
    BAD_OHLC = "BAD_OHLC"
    OUT_OF_SESSION_BAR = "OUT_OF_SESSION_BAR"
    MISALIGNED_BAR = "MISALIGNED_BAR"
    INSTRUMENT_MASTER_CHANGE = "INSTRUMENT_MASTER_CHANGE"
    # vendor-ingest checks (D-06)
    NON_MONOTONIC_TS = "NON_MONOTONIC_TS"
    NULL_VALUE = "NULL_VALUE"
    NON_TRADING_DAY_BAR = "NON_TRADING_DAY_BAR"
    SPECIAL_SESSION_BAR = "SPECIAL_SESSION_BAR"
    MISSING_DAY = "MISSING_DAY"
    COVERAGE_GAP = "COVERAGE_GAP"
    OUTSIDE_REQUEST_WINDOW = "OUTSIDE_REQUEST_WINDOW"
    BAD_STRIKE = "BAD_STRIKE"
    STRIKE_OFFSET_MISMATCH = "STRIKE_OFFSET_MISMATCH"
    IMPLAUSIBLE_IV = "IMPLAUSIBLE_IV"
    VALUE_ROUNDED = "VALUE_ROUNDED"
    # OD-015 (2-Oct-2026)
    OHLC_WITHIN_TOLERANCE = "OHLC_WITHIN_TOLERANCE"
    OPENING_VENDOR_GAP = "OPENING_VENDOR_GAP"
    # 24x7 venues (crypto archive ingest, 3-Oct-2026)
    ZERO_VOLUME_BAR = "ZERO_VOLUME_BAR"
    CLOSE_TIME_MISMATCH = "CLOSE_TIME_MISMATCH"
    LISTING_BOUNDARY = "LISTING_BOUNDARY"


@dataclass(frozen=True, slots=True)
class DQIssue:
    code: DQCode
    severity: DQSeverity
    instrument_key: str | None
    ts: datetime | None
    detail: str


@dataclass(frozen=True, slots=True)
class DQReport:
    issues: tuple[DQIssue, ...]
    checked: int
    thresholds_version: str

    @property
    def trustworthy(self) -> bool:
        return not any(i.severity is DQSeverity.BLOCKING for i in self.issues)

    def codes(self) -> Counter[DQCode]:
        return Counter(i.code for i in self.issues)

    def blocking(self) -> tuple[DQIssue, ...]:
        return tuple(i for i in self.issues if i.severity is DQSeverity.BLOCKING)


class DQThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    stale_quote_seconds: Decimal
    stale_exchange_ts_seconds: Decimal
    max_clock_drift_seconds: Decimal
    future_tolerance_seconds: Decimal
    abnormal_spread_min_ticks: int = Field(gt=0)
    abnormal_spread_pct_of_mid: Decimal
    max_missing_candle_fraction: Decimal
    # OD-015: an OHLC inconsistency of at most this many price units is float noise, not a bad bar
    ohlc_rounding_tolerance: Decimal
    # OD-015: missing bars from the session open for at most this many minutes, while the reference index has
    # those minutes, are a vendor gap (WARNING) and do not count toward max_missing_candle_fraction
    max_opening_vendor_gap_minutes: int = Field(ge=0)

    @field_validator(
        "stale_quote_seconds",
        "stale_exchange_ts_seconds",
        "max_clock_drift_seconds",
        "future_tolerance_seconds",
        "abnormal_spread_pct_of_mid",
        "max_missing_candle_fraction",
        "ohlc_rounding_tolerance",
        mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        if isinstance(v, float | bool):
            raise ValueError("quote numbers as strings (no floats)")
        d = Decimal(str(v))
        if d < 0:
            raise ValueError("thresholds must be >= 0")
        return d


def load_thresholds(path: Path) -> DQThresholds:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"DQ thresholds not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML {path}: {e}") from e
    try:
        return DQThresholds.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid DQ thresholds {path}: {e}") from e


def _secs(d: Decimal) -> timedelta:
    return timedelta(seconds=float(d))


def _aware(ts: datetime, name: str) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise DataQualityInputError(f"{name} must be timezone-aware")


def check_quotes(
    quotes: Sequence[Quote],
    *,
    now: datetime,
    thresholds: DQThresholds,
    master: InstrumentMaster | None = None,
    required_keys: frozenset[str] = frozenset(),
) -> DQReport:
    """Checks a stream/batch of quotes (any order of instruments; per-instrument order = arrival order).

    Staleness is evaluated on the latest quote of each instrument relative to ``now``.
    ``required_keys`` are instruments the current decision depends on: their defects are BLOCKING.
    """
    _aware(now, "now")
    if not isinstance(quotes, Sequence):
        raise DataQualityInputError("quotes must be a sequence")
    issues: list[DQIssue] = []

    def add(code: DQCode, sev: DQSeverity, q: Quote | None, detail: str, key: str | None = None) -> None:
        issues.append(DQIssue(code, sev, q.instrument_key if q else key, q.exchange_ts if q else None, detail))

    def sev_for(key: str, default: DQSeverity) -> DQSeverity:
        return DQSeverity.BLOCKING if key in required_keys else default

    known: dict[str, Any] = {}
    if master is not None:
        known = {c.instrument_key: c for c in master.contracts}
    today = now.astimezone(IST).date()
    last_by_key: dict[str, Quote] = {}
    seen: set[tuple[object, ...]] = set()
    drift = _secs(thresholds.max_clock_drift_seconds)
    fut_tol = _secs(thresholds.future_tolerance_seconds)

    for q in quotes:
        if not isinstance(q, Quote):
            raise DataQualityInputError(f"expected Quote, got {type(q).__name__}")
        k = q.instrument_key
        # identity / duplicate
        ident = (k, q.exchange_ts, q.bid, q.ask, q.bid_qty, q.ask_qty, q.ltp, q.oi)
        if ident in seen:
            add(DQCode.DUPLICATE_TICK, DQSeverity.INFO, q, "identical tick repeated")
        seen.add(ident)
        prev = last_by_key.get(k)
        if prev is not None and q.exchange_ts < prev.exchange_ts:
            add(
                DQCode.OUT_OF_ORDER,
                sev_for(k, DQSeverity.WARNING),
                q,
                f"exchange_ts {q.exchange_ts.isoformat()} < previous {prev.exchange_ts.isoformat()}",
            )
        if prev is None or q.exchange_ts >= prev.exchange_ts:
            last_by_key[k] = q
        # timestamps
        if q.exchange_ts - q.receive_ts > fut_tol:
            add(DQCode.FUTURE_TIMESTAMP, DQSeverity.BLOCKING, q, "exchange_ts is ahead of receive_ts")
        elif q.receive_ts - q.exchange_ts > drift:
            add(
                DQCode.TIMESTAMP_DRIFT,
                sev_for(k, DQSeverity.WARNING),
                q,
                f"receive lag {(q.receive_ts - q.exchange_ts).total_seconds():.3f}s > {drift.total_seconds()}s",
            )
        # prices
        for name in ("bid", "ask", "ltp"):
            v: Decimal | None = getattr(q, name)
            if v is not None and v <= 0:
                add(DQCode.ZERO_OR_NEGATIVE_PRICE, DQSeverity.BLOCKING, q, f"{name}={v}")
        for name in ("bid_qty", "ask_qty"):
            n: int | None = getattr(q, name)
            if n is not None and n < 0:
                add(DQCode.NEGATIVE_SIZE, DQSeverity.BLOCKING, q, f"{name}={n}")
        if q.bid is None or q.ask is None:
            add(DQCode.ONE_SIDED_MARKET, sev_for(k, DQSeverity.WARNING), q, f"bid={q.bid} ask={q.ask}")
        elif q.bid > 0 and q.ask > 0:
            if q.bid > q.ask:
                add(DQCode.CROSSED_MARKET, DQSeverity.BLOCKING, q, f"bid {q.bid} > ask {q.ask}")
            elif q.bid == q.ask:
                add(DQCode.LOCKED_MARKET, DQSeverity.WARNING, q, f"bid == ask == {q.bid}")
            else:
                c = known.get(k)
                if c is not None:
                    spread = q.ask - q.bid
                    mid = (q.ask + q.bid) / 2
                    limit = max(
                        thresholds.abnormal_spread_min_ticks * c.tick_size, thresholds.abnormal_spread_pct_of_mid * mid
                    )
                    if spread > limit:
                        add(
                            DQCode.ABNORMAL_SPREAD,
                            sev_for(k, DQSeverity.WARNING),
                            q,
                            f"spread {spread} > limit {limit}",
                        )
        # OI
        if q.is_option and q.oi is None:
            add(DQCode.MISSING_OI, sev_for(k, DQSeverity.WARNING), q, "option quote without OI")
        # instrument master
        if master is not None:
            c = known.get(k)
            if c is None:
                add(DQCode.UNKNOWN_INSTRUMENT, DQSeverity.BLOCKING, q, "not in instrument master")
            else:
                if c.expiry < today:
                    add(DQCode.EXPIRED_CONTRACT, DQSeverity.BLOCKING, q, f"expired {c.expiry}")
                for name in ("bid", "ask", "ltp"):
                    v2: Decimal | None = getattr(q, name)
                    if v2 is not None and v2 > 0 and (v2 % c.tick_size) != 0:
                        add(DQCode.OFF_TICK_PRICE, DQSeverity.BLOCKING, q, f"{name}={v2} not on tick {c.tick_size}")

    # staleness relative to now (latest quote per instrument)
    stale = _secs(thresholds.stale_quote_seconds)
    stale_ex = _secs(thresholds.stale_exchange_ts_seconds)
    for k, q in sorted(last_by_key.items()):
        if now - q.receive_ts > stale:
            add(
                DQCode.STALE_QUOTE,
                sev_for(k, DQSeverity.WARNING),
                q,
                f"last received {(now - q.receive_ts).total_seconds():.1f}s ago",
            )
        if now - q.exchange_ts > stale_ex:
            add(
                DQCode.STALE_EXCHANGE_TS,
                sev_for(k, DQSeverity.WARNING),
                q,
                f"exchange_ts {(now - q.exchange_ts).total_seconds():.1f}s old",
            )
    for k in sorted(required_keys - set(last_by_key)):
        add(DQCode.MISSING_REQUIRED_QUOTE, DQSeverity.BLOCKING, None, "no quote received", key=k)

    return DQReport(tuple(issues), len(quotes), thresholds.version)


def check_bars(
    bars: Sequence[Bar],
    *,
    trading_date: date,
    calendar: SessionCalendar,
    thresholds: DQThresholds,
    interval: timedelta = timedelta(minutes=1),
    session: tuple[time, time] | None = None,
    reference_starts: frozenset[datetime] | None = None,
) -> DQReport:
    """Checks one instrument's intraday bars for one F&O session day (bar = START time, IST).

    ``session`` overrides the (open, close) times, e.g. the cash session for an index series.

    OD-015:
    - OHLC consistency allows ``thresholds.ohlc_rounding_tolerance`` of float noise (e.g. Dhan sends a high of
      24574.5996 with a close of 24574.6000). A violation within it is one INFO OHLC_WITHIN_TOLERANCE per day;
      beyond it the bar stays BLOCKING BAD_OHLC. ``low <= 0`` is never tolerated.
    - ``reference_starts`` (the bar starts of the reference index that day, or None): a run of missing bars that
      begins at the session open, lasts at most ``max_opening_vendor_gap_minutes`` and whose minutes the index
      has, is a vendor gap: one WARNING OPENING_VENDOR_GAP, and those bars are not counted as missing. Without a
      reference the rule does not apply.

    Expected bars run from the exchange F&O normal open to (close - interval) for that date's session
    version, e.g. 385 one-minute bars on/after 3-Aug-2026 (09:15..15:39), 375 before (09:15..15:29).
    """
    if interval <= timedelta(0) or timedelta(days=1) % interval != timedelta(0):
        raise DataQualityInputError(f"unsupported interval {interval}")
    keys = {b.instrument_key for b in bars}
    if len(keys) > 1:
        raise DataQualityInputError(f"check_bars expects one instrument, got {sorted(keys)}")
    fo = session if session is not None else calendar_fo(calendar, trading_date)
    open_dt = datetime.combine(trading_date, fo[0], IST)
    close_dt = datetime.combine(trading_date, fo[1], IST)
    expected: list[datetime] = []
    t = open_dt
    while t + interval <= close_dt:
        expected.append(t)
        t += interval
    issues: list[DQIssue] = []
    key = next(iter(keys)) if keys else None
    by_start: dict[datetime, Bar] = {}
    tolerated: list[datetime] = []
    for b in bars:
        if not isinstance(b, Bar):
            raise DataQualityInputError(f"expected Bar, got {type(b).__name__}")
        s = b.start.astimezone(IST)
        if s.date() != trading_date or s < open_dt or s >= close_dt:
            issues.append(
                DQIssue(
                    DQCode.OUT_OF_SESSION_BAR,
                    DQSeverity.WARNING,
                    b.instrument_key,
                    b.start,
                    "bar outside the F&O normal session",
                )
            )
            continue
        if (s - open_dt) % interval != timedelta(0):
            issues.append(
                DQIssue(
                    DQCode.MISALIGNED_BAR,
                    DQSeverity.BLOCKING,
                    b.instrument_key,
                    b.start,
                    f"start not on the {interval} grid",
                )
            )
            continue
        verdict = ohlc_verdict(b, thresholds.ohlc_rounding_tolerance)
        if verdict == "BAD":
            issues.append(
                DQIssue(
                    DQCode.BAD_OHLC,
                    DQSeverity.BLOCKING,
                    b.instrument_key,
                    b.start,
                    f"O={b.open} H={b.high} L={b.low} C={b.close}",
                )
            )
        elif verdict == "TOLERATED":
            tolerated.append(b.start)
        if b.volume < 0:
            issues.append(DQIssue(DQCode.BAD_OHLC, DQSeverity.BLOCKING, b.instrument_key, b.start, "negative volume"))
        prev = by_start.get(s)
        if prev is not None:
            same = (prev.open, prev.high, prev.low, prev.close, prev.volume, prev.oi) == (
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.oi,
            )
            issues.append(
                DQIssue(
                    DQCode.DUPLICATE_BAR if same else DQCode.CONFLICTING_DUPLICATE_BAR,
                    DQSeverity.WARNING if same else DQSeverity.BLOCKING,
                    b.instrument_key,
                    b.start,
                    "duplicate bar" if same else "two different bars share a start time",
                )
            )
            continue
        by_start[s] = b
    if tolerated:
        issues.append(
            DQIssue(
                DQCode.OHLC_WITHIN_TOLERANCE,
                DQSeverity.INFO,
                key,
                tolerated[0],
                f"{len(tolerated)} bar(s) with OHLC off by <= {thresholds.ohlc_rounding_tolerance} (float noise)",
            )
        )
    missing = [e for e in expected if e not in by_start]
    if missing and reference_starts is not None:
        first_start, first_len = _runs(missing, interval)[0]
        gap = [first_start + interval * k for k in range(first_len)]
        if (
            first_start == open_dt
            and interval * first_len <= timedelta(minutes=thresholds.max_opening_vendor_gap_minutes)
            and all(t in reference_starts for t in gap)
        ):
            issues.append(
                DQIssue(
                    DQCode.OPENING_VENDOR_GAP,
                    DQSeverity.WARNING,
                    key,
                    first_start,
                    f"{first_len} bar(s) missing from the open while the reference index has them (vendor gap, OD-015)",
                )
            )
            missing = missing[first_len:]
    if missing:
        frac = Decimal(len(missing)) / Decimal(len(expected))
        sev = DQSeverity.BLOCKING if frac > thresholds.max_missing_candle_fraction else DQSeverity.WARNING
        total = f"{len(missing)}/{len(expected)} total"
        for run_start, run_len in _runs(missing, interval):
            detail = f"{run_len} missing bar(s) from {run_start.time()} ({total})"
            issues.append(DQIssue(DQCode.MISSING_CANDLE, sev, key, run_start, detail))
    return DQReport(tuple(issues), len(bars), thresholds.version)


def ohlc_verdict(b: Bar, tolerance: Decimal) -> str:
    """ "OK", "TOLERATED" (inconsistent by at most ``tolerance``: float noise, OD-015) or "BAD"."""
    lo_ref, hi_ref = min(b.open, b.close, b.high), max(b.open, b.close, b.low)
    if b.low <= 0 or b.high < hi_ref - tolerance or b.low > lo_ref + tolerance:
        return "BAD"
    if b.high < hi_ref or b.low > lo_ref:
        return "TOLERATED"
    return "OK"


def calendar_fo(calendar: SessionCalendar, d: date) -> tuple[time, time]:
    """(open, close) of the exchange F&O normal market for ``d`` (not our trading window)."""
    fo = calendar.exchange.fo_for(d)
    return fo.normal_open, fo.normal_close


def _runs(ts: Iterable[datetime], step: timedelta) -> list[tuple[datetime, int]]:
    out: list[tuple[datetime, int]] = []
    for t in ts:
        if out and out[-1][0] + step * out[-1][1] == t:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((t, 1))
    return out


_SEV_MAP = {
    MasterSeverity.INFO: DQSeverity.INFO,
    MasterSeverity.WARNING: DQSeverity.WARNING,
    MasterSeverity.BLOCKING: DQSeverity.BLOCKING,
}


def check_instrument_master_change(
    old: InstrumentMaster, new: InstrumentMaster, underlying: str, *, thresholds: DQThresholds
) -> DQReport:
    """Lot-size / tick / expiry changes between two masters, as DQ issues (lot-size change = BLOCKING)."""
    changes = diff_masters(old, new, underlying)
    issues = tuple(
        DQIssue(DQCode.INSTRUMENT_MASTER_CHANGE, _SEV_MAP[c.severity], None, new.as_of, f"{c.kind}: {c.detail}")
        for c in changes
    )
    return DQReport(issues, len(new), thresholds.version)

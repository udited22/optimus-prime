"""Data-quality checks on ingest for Dhan series (backlog D-06 + D-12). Every defect becomes a DQIssue;
nothing is dropped or fixed. A chunk with any BLOCKING issue is quarantined by the ingest step.

Per IST trading day the existing D-12 ``check_bars`` runs (missing/misaligned/out-of-session/duplicate/OHLC)
with the exchange session for that date (F&O for options and futures, cash for the index). Days outside the
holiday-calendar or session-config coverage are reported as COVERAGE_GAP (WARNING) and get only the
structural checks, so data before 2024-10 is never silently treated as verified.

Bars on a day the calendar lists as a SPECIAL_SESSION (Muhurat, the Sunday Budget session, Saturday DR sessions)
are real exchange sessions, not vendor errors: they get the structural checks and one SPECIAL_SESSION_BAR WARNING
(not a regular trading day for Project 100C, so a backtest must opt in to WARN data and should skip those days).
Bars on any other non-trading day stay BLOCKING. Found on the first real pull: before this rule a single Muhurat
hour quarantined a whole 90-day index chunk.

OD-015 (2-Oct-2026): OHLC checks allow ``ohlc_rounding_tolerance`` of float noise (structural checks too), and for
options and futures a vendor gap of at most ``max_opening_vendor_gap_minutes`` at the session open is a WARNING
when the NIFTY index has those minutes (``IngestContext.reference_minutes``; without it the rule is off). Found on
27-Mar-2026: 09:15-09:18 missing for almost every option strike quarantined a whole 30-day window.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from project100c.calendar.model import DayKind, TradingCalendar
from project100c.data.dhan.parse import ParsedColumns
from project100c.dq.checks import DQCode, DQIssue, DQReport, DQSeverity, DQThresholds, check_bars, ohlc_verdict
from project100c.errors import CalendarError, SessionError
from project100c.market_types import Bar
from project100c.sessions.model import IST, SessionCalendar

IV_MAX_PLAUSIBLE = Decimal(300)


class SeriesKind(StrEnum):
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    INDEX = "INDEX"


class DQStatus(StrEnum):
    PASS = "PASS"  # no WARNING or BLOCKING issues
    WARN = "WARN"  # usable with care; issues recorded in the manifest
    BLOCKED = "BLOCKED"  # quarantined; not in clean/


@dataclass(frozen=True, slots=True)
class IngestContext:
    trading_calendar: TradingCalendar
    sessions: SessionCalendar
    thresholds: DQThresholds
    strike_step: Decimal
    # OD-015: bar starts of the NIFTY index on a date (for the opening vendor-gap rule); None = rule off
    reference_minutes: Callable[[date], frozenset[datetime]] | None = None


def dq_status(report: DQReport) -> DQStatus:
    if not report.trustworthy:
        return DQStatus.BLOCKED
    if any(i.severity is DQSeverity.WARNING for i in report.issues):
        return DQStatus.WARN
    return DQStatus.PASS


def _session(ctx: IngestContext, kind: SeriesKind, d: date) -> tuple[time, time] | str:
    """(open, close) for the date, or a reason string when the date is not covered by config."""
    try:
        if not ctx.trading_calendar.covers(d):
            return f"holiday calendar {ctx.trading_calendar.version} does not cover {d.year}"
        s = ctx.sessions.exchange.cash_for(d) if kind is SeriesKind.INDEX else ctx.sessions.exchange.fo_for(d)
    except SessionError as e:
        return f"session config does not cover {d}: {e}"
    return s.normal_open, s.normal_close


def check_ingest(
    cols: ParsedColumns,
    *,
    series_key: str,
    kind: SeriesKind,
    window_from: date,
    window_to_exclusive: date,
    interval_min: int,
    strike_offset: int | None,
    ctx: IngestContext,
) -> DQReport:
    issues: list[DQIssue] = []

    def add(code: DQCode, sev: DQSeverity, ts: datetime | None, detail: str) -> None:
        issues.append(DQIssue(code, sev, series_key, ts, detail))

    c = cols.cols
    n = cols.rows
    for name, count in sorted(cols.rounded.items()):
        add(
            DQCode.VALUE_ROUNDED,
            DQSeverity.INFO if name == "iv" else DQSeverity.WARNING,
            None,
            f"{count} {name} value(s) had more decimals than stored and were rounded half-even",
        )
    # ordering and request window
    non_increasing = [i for i in range(1, n) if cols.ts[i] <= cols.ts[i - 1]]
    if non_increasing:
        i0 = non_increasing[0]
        add(
            DQCode.NON_MONOTONIC_TS,
            DQSeverity.BLOCKING,
            cols.ts[i0],
            f"{len(non_increasing)} non-increasing timestamp(s)",
        )
    outside = [t for t in cols.ts if not (window_from <= t.astimezone(IST).date() < window_to_exclusive)]
    if outside:
        add(
            DQCode.OUTSIDE_REQUEST_WINDOW,
            DQSeverity.WARNING,
            outside[0],
            f"{len(outside)} row(s) outside the request window",
        )
    # value checks
    step = ctx.strike_step
    counts: dict[tuple[DQCode, DQSeverity, str], list[datetime]] = defaultdict(list)

    def note(code: DQCode, sev: DQSeverity, what: str, i: int) -> None:
        counts[(code, sev, what)].append(cols.ts[i])

    nones: list[Any] = [None] * n
    ohlc = [(f, c[f]) for f in ("open", "high", "low", "close") if f in c]
    sizes = [(f, c[f]) for f in ("volume", "oi", "open_interest") if f in c]
    strikes, spots = c.get("strike", nones), c.get("spot", nones)
    for i in range(n):
        for f, col in ohlc:
            if col[i] is None:
                note(DQCode.NULL_VALUE, DQSeverity.BLOCKING, f"{f} is null", i)
        for g, col in sizes:
            if col[i] is not None and col[i] < 0:
                note(DQCode.NEGATIVE_SIZE, DQSeverity.BLOCKING, f"{g} < 0", i)
        if kind is SeriesKind.OPTION:
            strike, spot = strikes[i], spots[i]
            if "strike" in c and strike is None:
                note(DQCode.NULL_VALUE, DQSeverity.BLOCKING, "strike is null", i)
            elif strike is not None and strike <= 0:
                note(DQCode.BAD_STRIKE, DQSeverity.BLOCKING, "strike <= 0", i)
            elif strike is not None and strike % step != 0:
                note(DQCode.BAD_STRIKE, DQSeverity.WARNING, f"strike not a multiple of {step}", i)
            if "spot" in c and spot is None:
                note(DQCode.NULL_VALUE, DQSeverity.BLOCKING, "spot is null", i)
            elif spot is not None and spot <= 0:
                note(DQCode.ZERO_OR_NEGATIVE_PRICE, DQSeverity.BLOCKING, "spot <= 0", i)
            if strike is not None and spot is not None and spot > 0 and strike > 0 and strike_offset is not None:
                atm = (spot / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
                if abs(strike - (atm + strike_offset * step)) > step:
                    note(
                        DQCode.STRIKE_OFFSET_MISMATCH, DQSeverity.WARNING, "strike != ATM(spot) + offset (> 1 step)", i
                    )
            if "iv" in c:
                iv = c["iv"][i]
                if iv is None:
                    note(DQCode.NULL_VALUE, DQSeverity.WARNING, "iv is null", i)
                elif iv < 0 or iv > IV_MAX_PLAUSIBLE:
                    note(DQCode.IMPLAUSIBLE_IV, DQSeverity.WARNING, f"iv outside 0..{IV_MAX_PLAUSIBLE}", i)
            if "oi" in c and c["oi"][i] is None:
                note(DQCode.MISSING_OI, DQSeverity.WARNING, "oi is null", i)
    for (code, sev, what), tss in sorted(counts.items(), key=lambda kv: (kv[0][0].value, kv[0][2])):
        add(code, sev, tss[0], f"{len(tss)} row(s): {what}")

    # per-day bar checks
    by_day: dict[date, list[Bar]] = defaultdict(list)
    opens, highs, lows, closes = (c.get(f, nones) for f in ("open", "high", "low", "close"))
    vols = c.get("volume", nones)
    ois = c.get("oi", c.get("open_interest", nones))
    for i in range(n):
        o, h, lo, cl = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or lo is None or cl is None:
            continue
        vol = vols[i]
        by_day[cols.ts[i].astimezone(IST).date()].append(
            Bar(series_key, cols.ts[i], o, h, lo, cl, 0 if vol is None else vol, ois[i])
        )
    interval = timedelta(minutes=interval_min)
    uncovered: list[str] = []
    for d in sorted(by_day):
        sess = _session(ctx, kind, d)
        if isinstance(sess, str):
            uncovered.append(d.isoformat())
            issues.extend(_structural(by_day[d], series_key, ctx.thresholds))
            continue
        try:
            trading = ctx.trading_calendar.is_trading_day(d)
        except CalendarError as e:
            uncovered.append(d.isoformat())
            add(DQCode.COVERAGE_GAP, DQSeverity.WARNING, None, f"{d}: {e}")
            issues.extend(_structural(by_day[d], series_key, ctx.thresholds))
            continue
        if not trading:
            entry = ctx.trading_calendar.entry(d)
            if entry is not None and entry.kind is DayKind.SPECIAL_SESSION:
                add(
                    DQCode.SPECIAL_SESSION_BAR,
                    DQSeverity.WARNING,
                    by_day[d][0].start,
                    f"{len(by_day[d])} bar(s) on {d}, a calendar special session ({entry.description}); "
                    "not a regular trading day for Project 100C",
                )
                issues.extend(_structural(by_day[d], series_key, ctx.thresholds))
                continue
            add(DQCode.NON_TRADING_DAY_BAR, DQSeverity.BLOCKING, by_day[d][0].start, f"{len(by_day[d])} bar(s) on {d}")
            continue
        ref = ctx.reference_minutes(d) if ctx.reference_minutes is not None and kind is not SeriesKind.INDEX else None
        rep = check_bars(
            by_day[d],
            trading_date=d,
            calendar=ctx.sessions,
            thresholds=ctx.thresholds,
            interval=interval,
            session=sess,
            reference_starts=ref,
        )
        issues.extend(rep.issues)
    # trading days with no data at all
    d = window_from
    while d < window_to_exclusive:
        if d not in by_day:
            sess = _session(ctx, kind, d)
            if isinstance(sess, str):
                if d.weekday() < 5:
                    uncovered.append(d.isoformat())
            else:
                try:
                    if ctx.trading_calendar.is_trading_day(d):
                        add(DQCode.MISSING_DAY, DQSeverity.WARNING, None, f"no bars on trading day {d}")
                except CalendarError as e:
                    uncovered.append(d.isoformat())
                    add(DQCode.COVERAGE_GAP, DQSeverity.WARNING, None, f"{d}: {e}")
        d += timedelta(days=1)
    if uncovered:
        u = sorted(set(uncovered))
        add(
            DQCode.COVERAGE_GAP,
            DQSeverity.WARNING,
            None,
            f"{len(u)} day(s) {u[0]}..{u[-1]} outside calendar/session coverage: structural checks only (UNVERIFIED)",
        )
    return DQReport(tuple(issues), n, ctx.thresholds.version)


def _structural(bars: list[Bar], key: str, thresholds: DQThresholds) -> list[DQIssue]:
    out: list[DQIssue] = []
    seen: set[datetime] = set()
    for b in bars:
        if ohlc_verdict(b, thresholds.ohlc_rounding_tolerance) == "BAD":
            out.append(
                DQIssue(
                    DQCode.BAD_OHLC, DQSeverity.BLOCKING, key, b.start, f"O={b.open} H={b.high} L={b.low} C={b.close}"
                )
            )
        if b.start in seen:
            out.append(DQIssue(DQCode.DUPLICATE_BAR, DQSeverity.WARNING, key, b.start, "duplicate bar"))
        seen.add(b.start)
    return out

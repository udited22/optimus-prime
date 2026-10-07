"""US starter-dataset downloader (no key): S&P 500 constituents snapshot, daily bars (full history, Yahoo chart),
1-minute bars (last ~30 days, Yahoo chart), Cboe index history. Resumable: every unit of work is a row in a
SQLite task table under ``lake/us/jobs/``; DONE / NO_DATA / NOT_FOUND units are never fetched again.

Units: ``constituents:<as_of>``, ``daily:<symbol>:<as_of>`` (one full-history request, written as one part per
calendar year so a bad 1987 bar quarantines 1987 only), ``1m:<symbol>:<session>`` (fetched in windows of up to
``minute_window_days``), ``cboe:<series>:<as_of>``. Raw replies are kept verbatim (gzip) in ``raw/``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from project100c.data.lake import Lake, Lineage, RawRef, Zone
from project100c.data.public_http import PublicGetter
from project100c.data.us.alpaca import BARS_PATH, AlpacaBarsClient, Feed
from project100c.data.us.config import NyseSessions, UsMarketConfig
from project100c.data.us.dq import DQ_VERSION, CheckResult, check_daily, check_index_series, check_minute, report_status
from project100c.data.us.parse import (
    PARSER_VERSION,
    ParsedChart,
    chart_url,
    lake_symbol,
    parse_cboe_csv,
    parse_chart,
    parse_constituents,
)
from project100c.dq.checks import DQReport
from project100c.errors import (
    LakeError,
    QuotaExhaustedError,
    TransportError,
    VendorRateLimitError,
    VendorRequestError,
    VendorResponseError,
    VendorServerError,
)

MANIFEST_VERSION = "US-MANIFEST-1"
SRC_YAHOO, SRC_CBOE, SRC_LIST = "yahoo", "cboe", "sp500_list"
DS_CONS = "sp500_constituents"
DS_DAILY = "us_daily_bars"
DS_ACTIONS = "us_corporate_actions"
DS_1M = "us_minute_bars"
DS_CBOE = "cboe_index_daily"
DS_ALPACA_1M = "alpaca_minute_bars"
SRC_ALPACA = "alpaca"
_EPOCH_1900 = -2_208_988_800  # period1 for "everything": Yahoo returns from each listing's first trade date
_STOP = (QuotaExhaustedError, VendorRateLimitError)
_SKIP_ERRORS = (VendorServerError, TransportError, VendorRequestError, VendorResponseError, LakeError)


class Status(StrEnum):
    DONE = "DONE"
    NO_DATA = "NO_DATA"  # a trading session with no bars from the vendor (recorded, never re-fetched)
    NOT_FOUND = "NOT_FOUND"  # HTTP 404 for the symbol
    FAILED = "FAILED"  # retried on the next run


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  key TEXT PRIMARY KEY, dataset TEXT NOT NULL, symbol TEXT NOT NULL, period_id TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, raw_path TEXT, parts_json TEXT, rows INTEGER,
  dq_status TEXT, manifest_path TEXT, updated_utc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_by_dataset ON tasks(dataset, symbol, period_id);
CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY, requests INTEGER NOT NULL);
"""


@dataclass(frozen=True, slots=True)
class TaskRow:
    key: str
    dataset: str
    symbol: str
    period_id: str
    status: Status
    rows: int | None
    dq_status: str | None
    parts: tuple[str, ...]
    manifest_path: str | None
    last_error: str | None


class UsJobStore:
    """Task state plus the persisted daily request counter (a ``ratelimit.QuotaStore``)."""

    def __init__(self, path: Path, *, wall_clock: Callable[[], datetime]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=120)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._wall = wall_clock

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def requests_on(self, day: str) -> int:
        r = self._db.execute("SELECT requests FROM quota WHERE day=?", (day,)).fetchone()
        return 0 if r is None else int(r[0])

    def add_request(self, day: str) -> int:
        with self._tx() as db:
            db.execute(
                "INSERT INTO quota(day, requests) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET requests=requests+1",
                (day,),
            )
        return self.requests_on(day)

    def mark(
        self,
        key: str,
        *,
        dataset: str,
        symbol: str,
        period_id: str,
        status: Status,
        rows: int | None = None,
        dq_status: str | None = None,
        parts: Sequence[str] = (),
        raw_path: str | None = None,
        manifest_path: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = self._wall().astimezone(UTC).isoformat()
        with self._tx() as db:
            db.execute(
                "INSERT INTO tasks(key, dataset, symbol, period_id, status, attempts, last_error, raw_path, parts_json,"
                " rows, dq_status, manifest_path, updated_utc) VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET status=excluded.status, attempts=attempts+1,"
                " last_error=excluded.last_error, raw_path=excluded.raw_path, parts_json=excluded.parts_json,"
                " rows=excluded.rows, dq_status=excluded.dq_status, manifest_path=excluded.manifest_path,"
                " updated_utc=excluded.updated_utc",
                (
                    key,
                    dataset,
                    symbol,
                    period_id,
                    status.value,
                    last_error,
                    raw_path,
                    json.dumps(list(parts)),
                    rows,
                    dq_status,
                    manifest_path,
                    now,
                ),
            )

    def get(self, key: str) -> TaskRow | None:
        r = self._db.execute(
            "SELECT key, dataset, symbol, period_id, status, rows, dq_status, parts_json, manifest_path, last_error"
            " FROM tasks WHERE key=?",
            (key,),
        ).fetchone()
        return None if r is None else _row(r)

    def tasks(self, dataset: str | None = None) -> list[TaskRow]:
        q = (
            "SELECT key, dataset, symbol, period_id, status, rows, dq_status, parts_json, manifest_path, last_error"
            " FROM tasks"
        )
        args: tuple[str, ...] = ()
        if dataset is not None:
            q += " WHERE dataset=?"
            args = (dataset,)
        return [_row(r) for r in self._db.execute(q + " ORDER BY key", args).fetchall()]

    def finished(self, key: str) -> bool:
        t = self.get(key)
        return t is not None and t.status in (Status.DONE, Status.NO_DATA, Status.NOT_FOUND)


def _row(r: tuple[Any, ...]) -> TaskRow:
    return TaskRow(r[0], r[1], r[2], r[3], Status(r[4]), r[5], r[6], tuple(json.loads(r[7] or "[]")), r[8], r[9])


@dataclass(slots=True)
class RunSummary:
    attempted: int = 0
    done: int = 0
    quarantined_parts: int = 0
    no_data: int = 0
    not_found: int = 0
    failed: int = 0
    skipped: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"attempted={self.attempted} done={self.done} skipped={self.skipped} no_data={self.no_data}"
            f" not_found={self.not_found} failed={self.failed} quarantined_parts={self.quarantined_parts}"
            f" rows={self.rows}"
        )


def _issues_json(report: DQReport) -> list[dict[str, Any]]:
    return [
        {
            "code": i.code.value,
            "severity": i.severity.value,
            "ts": None if i.ts is None else i.ts.isoformat(),
            "detail": i.detail,
        }
        for i in report.issues
    ]


def _dq_json(res: CheckResult, thresholds_version: str) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for i in res.report.issues:
        counts[f"{i.severity.value}:{i.code.value}"] += 1
    return {
        "status": report_status(res.report),
        "version": DQ_VERSION,
        "thresholds_version": thresholds_version,
        "stats": res.stats,
        "issue_counts": dict(sorted(counts.items())),
        "issues": _issues_json(res.report),
    }


def _ny_midnight(d: date, tz: Any) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


def minute_windows(days: Sequence[date], window_days: int) -> list[list[date]]:
    """Group sorted session dates into request windows spanning at most ``window_days`` calendar days."""
    out: list[list[date]] = []
    for d in sorted(days):
        if out and (d - out[-1][0]).days < window_days:
            out[-1].append(d)
        else:
            out.append([d])
    return out


class UsDownloader:
    def __init__(
        self,
        *,
        config: UsMarketConfig,
        nyse: NyseSessions,
        getter: PublicGetter,
        lake: Lake,
        store: UsJobStore,
        wall_clock: Callable[[], datetime],
        thresholds_version: str,
        max_missing_fraction: Any,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self._cfg = config
        self._nyse = nyse
        self._get = getter
        self._lake = lake
        self._store = store
        self._wall = wall_clock
        self._thr = thresholds_version
        self._mmf = max_missing_fraction
        self._log = log

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return self._wall().astimezone(UTC).isoformat()

    def _raw(self, source: str, dataset: str, day: date, stem: str, body: bytes, url: str, ext: str) -> RawRef:
        return self._lake.write_raw(
            source,
            dataset,
            day,
            stem,
            body,
            sidecar={"fetched_at_utc": self._now(), "url": url, "config_version": self._cfg.version},
            ext=ext,
        )

    def _lineage(
        self, source: str, dataset: str, raw: RawRef, status: str, request: dict[str, Any], key: str
    ) -> Lineage:
        return Lineage(
            source=source,
            dataset=dataset,
            data_version=f"{dataset}@{PARSER_VERSION}",
            parser_version=PARSER_VERSION,
            config_version=self._cfg.version,
            dq_thresholds_version=f"{DQ_VERSION}+{self._thr}",
            dq_status=status,
            raw_path=raw.path,
            raw_sha256=raw.sha256,
            fetched_at_utc=raw.fetched_at_utc,
            request=request,
            job_id=None,
            chunk_key=key,
        )

    def _put(
        self,
        dataset: str,
        partitions: list[tuple[str, str]],
        name: str,
        res: CheckResult,
        lineage: Lineage,
    ) -> tuple[str, Zone]:
        zone = Zone.QUARANTINE if lineage.dq_status == "BLOCKED" else Zone.CLEAN
        other = Zone.CLEAN if zone is Zone.QUARANTINE else Zone.QUARANTINE
        self._lake.discard_uncommitted_part(other, dataset, partitions, name)
        return self._lake.write_part(zone, dataset, partitions, name, res.table, lineage).path, zone

    def today_ny(self) -> date:
        return self._wall().astimezone(self._nyse.tz).date()

    # ------------------------------------------------------------------ constituents
    def constituents(self, as_of: date) -> list[str]:
        key = f"constituents:{as_of.isoformat()}"
        t = self._store.get(key)
        if t is not None and t.status is Status.DONE and t.parts:
            return list(self._lake.read_part(t.parts[0])["symbol"].to_pylist())
        url = self._cfg.constituents_url
        got = self._get.get(url, accept="text/csv")
        if got is None:
            raise VendorResponseError(f"constituents list not found at {url}")
        raw = self._raw(SRC_LIST, DS_CONS, as_of, "constituents", got.response.body, url, "csv")
        table = parse_constituents(got.response.body)
        lin = self._lineage(SRC_LIST, DS_CONS, raw, "PASS", {"url": url}, key)
        part = self._lake.write_part(Zone.REF, DS_CONS, [("as_of", as_of.isoformat())], "sp500", table, lin)
        m = self._lake.write_manifest(
            DS_CONS,
            f"sp500-{as_of.isoformat()}",
            {
                "manifest_version": MANIFEST_VERSION,
                "dataset": DS_CONS,
                "as_of": as_of.isoformat(),
                "members": table.num_rows,
                "part": part.path,
                "lineage": lin.as_dict(),
                "survivorship": "CURRENT members only. Companies removed from the index before as_of are absent, "
                "so any backtest over this list overstates returns (survivorship bias).",
            },
        )
        self._store.mark(
            key,
            dataset=DS_CONS,
            symbol="SP500",
            period_id=as_of.isoformat(),
            status=Status.DONE,
            rows=table.num_rows,
            dq_status="PASS",
            parts=[part.path],
            raw_path=raw.path,
            manifest_path=m,
        )
        return list(table["symbol"].to_pylist())

    # ------------------------------------------------------------------ daily
    def _reference_dates(self, as_of: date) -> tuple[frozenset[date], tuple[date, date]] | None:
        t = self._store.get(f"daily:{self._cfg.reference_symbol}:{as_of.isoformat()}")
        if t is None or t.status is not Status.DONE:
            return None
        ds: set[date] = set()
        for p in t.parts:
            ds |= set(self._lake.read_part(p)["session_date"].to_pylist())
        return (frozenset(ds), (min(ds), max(ds))) if ds else None

    def daily(self, symbols: Sequence[str], as_of: date, *, max_symbols: int | None = None) -> RunSummary:
        s = RunSummary()
        ref_sym = self._cfg.reference_symbol
        seq = [ref_sym, *[x for x in dict.fromkeys(symbols) if x != ref_sym]]
        for sym in seq[: max_symbols if max_symbols is not None else len(seq)]:
            key = f"daily:{sym}:{as_of.isoformat()}"
            if self._store.finished(key):
                s.skipped += 1
                continue
            ref = None if sym == ref_sym else self._reference_dates(as_of)
            if sym != ref_sym and ref is None:
                raise VendorResponseError(f"reference symbol {ref_sym} has no daily data for {as_of}; run it first")
            s.attempted += 1
            try:
                self._daily_one(sym, as_of, key, ref, s)
            except _STOP:
                raise
            except _SKIP_ERRORS as e:
                self._store.mark(
                    key, dataset=DS_DAILY, symbol=sym, period_id=as_of.isoformat(), status=Status.FAILED,
                    last_error=f"{type(e).__name__}: {e}",
                )  # fmt: skip
                s.failed += 1
                s.errors.append(f"{key}: {e}")
                self._log(f"FAILED {key}: {e}")
        return s

    def _daily_one(
        self, sym: str, as_of: date, key: str, ref: tuple[frozenset[date], tuple[date, date]] | None, s: RunSummary
    ) -> None:
        period2 = _ny_midnight(as_of + timedelta(days=1), self._nyse.tz)
        url = chart_url(self._cfg.yahoo_chart_base, sym, interval="1d", period1=_EPOCH_1900, period2=period2)
        got = self._get.get(url, accept="application/json")
        if got is None:
            self._store.mark(key, dataset=DS_DAILY, symbol=sym, period_id=as_of.isoformat(), status=Status.NOT_FOUND)
            s.not_found += 1
            self._log(f"NOT_FOUND {key}")
            return
        ls = lake_symbol(sym)
        raw = self._raw(SRC_YAHOO, DS_DAILY, as_of, f"{ls}-1d", got.response.body, url, "json")
        pc_ = parse_chart(got.response.body, symbol=sym, interval="1d")
        bars = pc_.bars.filter(pc.less_equal(pc_.bars["session_date"], pa.scalar(as_of, pa.date32())))
        is_index = sym.startswith("^")
        years = sorted({d.year for d in bars["session_date"].to_pylist()})
        parts: list[str] = []
        per_year: dict[str, Any] = {}
        worst = "PASS"
        request = {"symbol": sym, "interval": "1d", "period1": _EPOCH_1900, "period2": period2}
        for y in years:
            yb = bars.filter(pc.equal(pc.year(bars["session_date"]), y))
            res = check_daily(
                yb,
                key=f"{key}:{y}",
                is_index=is_index,
                all_null_rows=sum(1 for d in pc_.all_null_dates if d.year == y),
                partial_null=sum(1 for d in pc_.partial_null_dates if d.year == y),
                rounded=0,
                reference_dates=None if ref is None else ref[0],
                reference_span=None if ref is None else ref[1],
                nyse=self._nyse,
                thresholds_version=self._thr,
            )
            st = report_status(res.report)
            worst = max(worst, st, key=("PASS", "WARN", "BLOCKED").index)
            lin = self._lineage(SRC_YAHOO, DS_DAILY, raw, st, request, f"{key}:{y}")
            path, zone = self._put(DS_DAILY, [("symbol", ls), ("year", str(y))], "bars", res, lin)
            parts.append(path)
            s.rows += res.table.num_rows
            if zone is Zone.QUARANTINE:
                s.quarantined_parts += 1
            per_year[str(y)] = {"rows": res.table.num_rows, "zone": zone.value, "dq": _dq_json(res, self._thr)}
        act_lin = self._lineage(SRC_YAHOO, DS_ACTIONS, raw, "PASS", request, key)
        act = self._lake.write_part(Zone.CLEAN, DS_ACTIONS, [("symbol", ls)], "events", pc_.actions, act_lin)
        m = self._lake.write_manifest(
            DS_DAILY,
            f"{ls}-{as_of.isoformat()}",
            {
                "manifest_version": MANIFEST_VERSION,
                "dataset": DS_DAILY,
                "symbol": sym,
                "as_of": as_of.isoformat(),
                "rows": bars.num_rows,
                "first_session": None if not bars.num_rows else min(bars["session_date"].to_pylist()).isoformat(),
                "last_session": None if not bars.num_rows else max(bars["session_date"].to_pylist()).isoformat(),
                "dq_status": worst,
                "years": per_year,
                "vendor_meta": {
                    k: pc_.meta.get(k) for k in ("exchangeName", "instrumentType", "firstTradeDate", "currency")
                },
                "price_note": "open/high/low/close are split-adjusted (Yahoo convention); adj_close is split- and "
                "dividend-adjusted; volume is split-adjusted. Actions part lists the splits and dividends.",
                "actions_part": act.path,
                "raw": raw.path,
            },
        )
        self._store.mark(
            key, dataset=DS_DAILY, symbol=sym, period_id=as_of.isoformat(), status=Status.DONE, rows=bars.num_rows,
            dq_status=worst, parts=parts, raw_path=raw.path, manifest_path=m,
        )  # fmt: skip
        s.done += 1
        self._log(f"DONE {key} rows={bars.num_rows} years={len(years)} dq={worst}")

    # ------------------------------------------------------------------ minute
    def pending_minute_days(self, sym: str, today: date) -> list[date]:
        lo = today - timedelta(days=self._cfg.minute_lookback_days)
        days = [lo + timedelta(days=i) for i in range((today - lo).days)]
        return [
            d for d in days if self._nyse.is_trading_day(d) and not self._store.finished(f"1m:{sym}:{d.isoformat()}")
        ]

    def minute(self, symbols: Sequence[str], *, max_symbols: int | None = None) -> RunSummary:
        s = RunSummary()
        today = self.today_ny()
        for sym in list(dict.fromkeys(symbols))[: max_symbols if max_symbols is not None else len(symbols)]:
            pending = self.pending_minute_days(sym, today)
            if not pending:
                s.skipped += 1
                continue
            for win in minute_windows(pending, self._cfg.minute_window_days):
                s.attempted += 1
                try:
                    self._minute_window(sym, win, s)
                except _STOP:
                    raise
                except _SKIP_ERRORS as e:
                    for d in win:
                        self._store.mark(
                            f"1m:{sym}:{d.isoformat()}", dataset=DS_1M, symbol=sym, period_id=d.isoformat(),
                            status=Status.FAILED, last_error=f"{type(e).__name__}: {e}",
                        )  # fmt: skip
                    s.failed += 1
                    s.errors.append(f"1m:{sym}:{win[0]}..{win[-1]}: {e}")
                    self._log(f"FAILED 1m:{sym}:{win[0]}..{win[-1]}: {e}")
        return s

    def _minute_window(self, sym: str, win: list[date], s: RunSummary) -> None:
        tz = self._nyse.tz
        p1, p2 = _ny_midnight(win[0], tz), _ny_midnight(win[-1] + timedelta(days=1), tz)
        url = chart_url(self._cfg.yahoo_chart_base, sym, interval="1m", period1=p1, period2=p2)
        got = self._get.get(url, accept="application/json")
        ls = lake_symbol(sym)
        if got is None:
            for d in win:
                self._store.mark(
                    f"1m:{sym}:{d.isoformat()}",
                    dataset=DS_1M,
                    symbol=sym,
                    period_id=d.isoformat(),
                    status=Status.NOT_FOUND,
                )
            s.not_found += 1
            return
        raw = self._raw(
            SRC_YAHOO,
            DS_1M,
            win[0],
            f"{ls}-1m-{win[0].isoformat()}-{win[-1].isoformat()}",
            got.response.body,
            url,
            "json",
        )
        pc_: ParsedChart = parse_chart(got.response.body, symbol=sym, interval="1m")
        request = {"symbol": sym, "interval": "1m", "period1": p1, "period2": p2}
        self._ingest_minute_days(
            SRC_YAHOO, DS_1M, sym, pc_.bars, win, raw, request, pc_.all_null_dates, pc_.partial_null_dates, s
        )
        self._log(f"DONE 1m:{sym}:{win[0]}..{win[-1]} bars={pc_.bars.num_rows}")

    def _ingest_minute_days(
        self,
        source: str,
        dataset: str,
        sym: str,
        bars: pa.Table,
        days: Sequence[date],
        raw: RawRef,
        request: dict[str, Any],
        all_null_dates: Sequence[date],
        partial_null_dates: Sequence[date],
        s: RunSummary,
    ) -> None:
        """Split a reply into NYSE sessions, DQ each, write one part per (symbol, session). Vendor-agnostic."""
        ls = lake_symbol(sym)
        prefix = "1m" if dataset == DS_1M else f"{dataset}"
        for d in days:
            key = f"{prefix}:{sym}:{d.isoformat()}"
            day_bars = bars.filter(pc.equal(bars["session_date"], pa.scalar(d, pa.date32())))
            res = check_minute(
                day_bars,
                key=key,
                day=d,
                is_index=sym.startswith("^"),
                all_null_rows=list(all_null_dates).count(d),
                partial_null=list(partial_null_dates).count(d),
                rounded=0,
                nyse=self._nyse,
                max_missing_fraction=self._mmf,
                thresholds_version=self._thr,
            )
            if res.table.num_rows == 0:
                self._store.mark(
                    key, dataset=dataset, symbol=sym, period_id=d.isoformat(), status=Status.NO_DATA,
                    raw_path=raw.path,
                )  # fmt: skip
                s.no_data += 1
                continue
            st = report_status(res.report)
            lin = self._lineage(source, dataset, raw, st, request, key)
            path, zone = self._put(dataset, [("symbol", ls), ("month", d.isoformat()[:7])], d.isoformat(), res, lin)
            m = self._lake.write_manifest(
                dataset,
                f"{ls}-{d.isoformat()}",
                {
                    "manifest_version": MANIFEST_VERSION,
                    "dataset": dataset,
                    "symbol": sym,
                    "session": d.isoformat(),
                    "rows": res.table.num_rows,
                    "part": {"zone": zone.value, "path": path},
                    "lineage": lin.as_dict(),
                    "dq": _dq_json(res, self._thr),
                },
            )
            self._store.mark(
                key, dataset=dataset, symbol=sym, period_id=d.isoformat(), status=Status.DONE, rows=res.table.num_rows,
                dq_status=st, parts=[path], raw_path=raw.path, manifest_path=m,
            )  # fmt: skip
            s.done += 1
            s.rows += res.table.num_rows
            if zone is Zone.QUARANTINE:
                s.quarantined_parts += 1

    # ------------------------------------------------------------------ Alpaca (needs a free key)
    def alpaca_minute(
        self, client: AlpacaBarsClient, symbols: Sequence[str], *, feed: Feed = "sip", days: Sequence[date]
    ) -> RunSummary:
        """1-minute SIP bars for NYSE sessions ``days`` (one request series per symbol and window)."""
        s = RunSummary()
        for sym in dict.fromkeys(symbols):
            pending = [
                d for d in days if self._nyse.is_trading_day(d)
                and not self._store.finished(f"{DS_ALPACA_1M}:{sym}:{d.isoformat()}")
            ]  # fmt: skip
            for win in minute_windows(pending, self._cfg.minute_window_days):
                s.attempted += 1
                start = datetime.combine(win[0], datetime.min.time(), self._nyse.tz)
                end = datetime.combine(win[-1] + timedelta(days=1), datetime.min.time(), self._nyse.tz)
                try:
                    got = client.bars([sym], timeframe="1Min", start=start, end=end, feed=feed)
                except _STOP:
                    raise
                except _SKIP_ERRORS as e:
                    s.failed += 1
                    s.errors.append(f"alpaca {sym} {win[0]}..{win[-1]}: {e}")
                    continue
                body = b"[" + b",".join(got.pages) + b"]"
                stem = f"{lake_symbol(sym)}-1m-{win[0].isoformat()}-{win[-1].isoformat()}"
                raw = self._raw(SRC_ALPACA, DS_ALPACA_1M, win[0], stem, body, f"alpaca{BARS_PATH}", "json")
                request = {
                    "symbol": sym,
                    "timeframe": "1Min",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "feed": feed,
                }
                self._ingest_minute_days(SRC_ALPACA, DS_ALPACA_1M, sym, got.tables[sym], win, raw, request, [], [], s)
        return s

    # ------------------------------------------------------------------ Cboe
    def cboe(self, as_of: date) -> RunSummary:
        s = RunSummary()
        for series in self._cfg.cboe_series:
            key = f"cboe:{series}:{as_of.isoformat()}"
            if self._store.finished(key):
                s.skipped += 1
                continue
            s.attempted += 1
            url = f"{self._cfg.cboe_base}/{series}_History.csv"
            try:
                got = self._get.get(url, accept="text/csv")
                if got is None:
                    self._store.mark(
                        key, dataset=DS_CBOE, symbol=series, period_id=as_of.isoformat(), status=Status.NOT_FOUND
                    )
                    s.not_found += 1
                    continue
                raw = self._raw(SRC_CBOE, DS_CBOE, as_of, series, got.response.body, url, "csv")
                table, rounded = parse_cboe_csv(got.response.body, series=series)
                parts: list[str] = []
                years: dict[str, Any] = {}
                worst, rows = "PASS", 0
                for y in sorted({d.year for d in table["session_date"].to_pylist()}):
                    yt = table.filter(pc.equal(pc.year(table["session_date"]), y))
                    res = check_index_series(
                        yt, key=f"{key}:{y}", rounded=rounded if not parts else 0, thresholds_version=self._thr
                    )
                    st = report_status(res.report)
                    worst = max(worst, st, key=("PASS", "WARN", "BLOCKED").index)
                    lin = self._lineage(SRC_CBOE, DS_CBOE, raw, st, {"url": url}, f"{key}:{y}")
                    path, zone = self._put(DS_CBOE, [("series", series), ("year", str(y))], "history", res, lin)
                    parts.append(path)
                    rows += res.table.num_rows
                    if zone is Zone.QUARANTINE:
                        s.quarantined_parts += 1
                    years[str(y)] = {"rows": res.table.num_rows, "zone": zone.value, "dq": _dq_json(res, self._thr)}
                m = self._lake.write_manifest(
                    DS_CBOE,
                    f"{series}-{as_of.isoformat()}",
                    {
                        "manifest_version": MANIFEST_VERSION,
                        "dataset": DS_CBOE,
                        "series": series,
                        "as_of": as_of.isoformat(),
                        "rows": rows,
                        "dq_status": worst,
                        "years": years,
                        "raw": raw.path,
                    },
                )
            except _STOP:
                raise
            except _SKIP_ERRORS as e:
                self._store.mark(
                    key, dataset=DS_CBOE, symbol=series, period_id=as_of.isoformat(), status=Status.FAILED,
                    last_error=f"{type(e).__name__}: {e}",
                )  # fmt: skip
                s.failed += 1
                s.errors.append(f"{key}: {e}")
                continue
            self._store.mark(
                key, dataset=DS_CBOE, symbol=series, period_id=as_of.isoformat(), status=Status.DONE,
                rows=rows, dq_status=worst, parts=parts, raw_path=raw.path, manifest_path=m,
            )  # fmt: skip
            s.done += 1
            s.rows += rows
        return s

"""Checkpointed Binance archive download into a crypto lake (``lake/crypto``; one task per archive file).

State lives in SQLite (``<lake>/jobs/crypto_jobs.sqlite``). A task is DONE only after the verified raw zip,
the Parquet part and the manifest are all on disk, so an interrupted run loses at most the file in flight and
re-running resumes. Statuses: PENDING -> DONE | NOT_PUBLISHED (404) | FAILED (retried next run) and REPLACED
(a daily file whose month later appeared as a monthly file; its part is removed, its raw bytes stay).

Listing and delisting dates are taken from what the archive holds and recorded, never filled: the symbol's
first file's first bar is its listing bound and, for a symbol whose archive stops before the run's last day,
the last file's last bar is its end bound (``LISTING_BOUNDARY``); minutes outside the bounds are not "missing".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from project100c.data.binance.archive import ArchiveKind, BinanceArchive, period_of, prefix
from project100c.data.binance.dq import DQ_VERSION, CheckResult, check_klines, check_series
from project100c.data.binance.parse import METRIC_RAW, PARSER_VERSION, parse_klines, parse_metrics, parse_rate
from project100c.data.lake import Lake, Lineage, Zone, sha256_hex
from project100c.dq.checks import DQReport, DQSeverity
from project100c.errors import (
    ChecksumMismatchError,
    DownloadJobError,
    LakeError,
    QuotaExhaustedError,
    TransportError,
    VendorRateLimitError,
    VendorRequestError,
    VendorResponseError,
    VendorServerError,
)

SOURCE = "binance"
MANIFEST_VERSION = "BINANCE-MANIFEST-1"
DATASET = {
    ArchiveKind.SPOT_KLINES: "binance_spot_klines",
    ArchiveKind.UM_KLINES: "binance_um_klines",
    ArchiveKind.UM_RATE: "binance_um_fundingrate",
    ArchiveKind.UM_METRICS: "binance_um_metrics",
}
PERIODS = {
    ArchiveKind.SPOT_KLINES: ("monthly", "daily"),
    ArchiveKind.UM_KLINES: ("monthly", "daily"),
    ArchiveKind.UM_RATE: ("monthly",),
    ArchiveKind.UM_METRICS: ("daily",),
}
KIND_RANK = {
    ArchiveKind.SPOT_KLINES: 0,
    ArchiveKind.UM_KLINES: 1,
    ArchiveKind.UM_RATE: 2,
    ArchiveKind.UM_METRICS: 3,
}


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    FAILED = "FAILED"
    REPLACED = "REPLACED"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  key TEXT PRIMARY KEY, kind TEXT NOT NULL, symbol TEXT NOT NULL, period TEXT NOT NULL, period_id TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, archive_sha256 TEXT,
  raw_path TEXT, part_path TEXT, part_sha256 TEXT, zone TEXT, rows INTEGER, dq_status TEXT, first_ts TEXT,
  last_ts TEXT, missing_slots INTEGER, manifest_path TEXT, updated_utc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_by_series ON tasks(kind, symbol, period_id);
CREATE TABLE IF NOT EXISTS listings(
  kind TEXT NOT NULL, symbol TEXT NOT NULL, listed_utc TEXT NOT NULL, through_day TEXT NOT NULL,
  monthly_files INTEGER NOT NULL, daily_files INTEGER NOT NULL, first_period TEXT, last_period TEXT,
  PRIMARY KEY (kind, symbol));
CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY, requests INTEGER NOT NULL);
"""


@dataclass(frozen=True, slots=True)
class Task:
    key: str
    kind: ArchiveKind
    symbol: str
    period: str
    period_id: str
    status: TaskStatus
    attempts: int
    dq_status: str | None
    rows: int | None


class CryptoJobStore:
    """SQLite task state plus the persisted daily request counter (a ratelimit.QuotaStore)."""

    def __init__(self, path: Path, *, wall_clock: Callable[[], datetime]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=120)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._wall = wall_clock

    def close(self) -> None:
        self._db.close()

    def _now(self) -> str:
        return self._wall().astimezone(UTC).isoformat()

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

    def upsert_tasks(self, kind: ArchiveKind, symbol: str, keys: Sequence[tuple[str, str, str]]) -> int:
        """(key, period, period_id) rows; new keys become PENDING. Returns how many were new."""
        new = 0
        with self._tx() as db:
            for key, period, pid in keys:
                cur = db.execute(
                    "INSERT OR IGNORE INTO tasks(key, kind, symbol, period, period_id, status, updated_utc) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (key, kind.value, symbol, period, pid, TaskStatus.PENDING.value, self._now()),
                )
                new += cur.rowcount
        return new

    def record_listing(
        self,
        kind: ArchiveKind,
        symbol: str,
        through: date,
        monthly: int,
        daily: int,
        first: str | None,
        last: str | None,
    ) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO listings VALUES(?,?,?,?,?,?,?,?)",
                (kind.value, symbol, self._now(), through.isoformat(), monthly, daily, first, last),
            )

    def listing(self, kind: ArchiveKind, symbol: str) -> tuple[str | None, str | None, str] | None:
        r = self._db.execute(
            "SELECT first_period, last_period, through_day FROM listings WHERE kind=? AND symbol=?",
            (kind.value, symbol),
        ).fetchone()
        return None if r is None else (r[0], r[1], str(r[2]))

    def tasks(
        self, *, kinds: Sequence[ArchiveKind] | None = None, statuses: Sequence[TaskStatus] | None = None
    ) -> list[Task]:
        rows = self._db.execute(
            "SELECT key, kind, symbol, period, period_id, status, attempts, dq_status, rows FROM tasks"
        ).fetchall()
        out = [
            Task(r[0], ArchiveKind(r[1]), r[2], r[3], r[4], TaskStatus(r[5]), int(r[6]), r[7], r[8])
            for r in rows
            if (kinds is None or ArchiveKind(r[1]) in kinds) and (statuses is None or TaskStatus(r[5]) in statuses)
        ]
        return sorted(out, key=lambda t: (KIND_RANK[t.kind], t.symbol, t.period_id))

    def part_of(self, key: str) -> str | None:
        r = self._db.execute("SELECT part_path FROM tasks WHERE key=?", (key,)).fetchone()
        return None if r is None or r[0] is None else str(r[0])

    def raw_of(self, key: str) -> str | None:
        r = self._db.execute("SELECT raw_path FROM tasks WHERE key=?", (key,)).fetchone()
        return None if r is None or r[0] is None else str(r[0])

    def mark(self, key: str, status: TaskStatus, **cols: Any) -> None:
        allowed = {
            "last_error",
            "archive_sha256",
            "raw_path",
            "part_path",
            "part_sha256",
            "zone",
            "rows",
            "dq_status",
            "first_ts",
            "last_ts",
            "missing_slots",
            "manifest_path",
        }
        bad = set(cols) - allowed
        if bad:
            raise DownloadJobError(f"unknown task columns {sorted(bad)}")
        sets = "".join(f", {k}=?" for k in cols)
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE tasks SET status=?, attempts=attempts+1, updated_utc=?{sets} WHERE key=?",
                (status.value, self._now(), *cols.values(), key),
            )
            if cur.rowcount != 1:
                raise DownloadJobError(f"task {key} not found")

    def counts(self) -> dict[tuple[str, str, str], int]:
        out: dict[tuple[str, str, str], int] = {}
        for k, s, st, n in self._db.execute("SELECT kind, symbol, status, COUNT(*) FROM tasks GROUP BY 1,2,3"):
            out[(str(k), str(s), str(st))] = int(n)
        return out

    def series_rows(self) -> list[tuple[Any, ...]]:
        return self._db.execute(
            "SELECT kind, symbol, period, period_id, status, rows, dq_status, zone, first_ts, last_ts, missing_slots, "
            "manifest_path FROM tasks ORDER BY kind, symbol, period_id"
        ).fetchall()


# ---------------------------------------------------------------------------------------------- planning
def _month_of(pid: str) -> str:
    return pid[:7]


def _period_bounds(pid: str) -> tuple[datetime, datetime]:
    if len(pid) == 7:
        y, m = int(pid[:4]), int(pid[5:7])
        start = datetime(y, m, 1, tzinfo=UTC)
        end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
        return start, end
    d = date.fromisoformat(pid)
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


@dataclass(slots=True)
class PlanResult:
    kind: ArchiveKind
    symbol: str
    monthly: int = 0
    daily: int = 0
    new: int = 0
    replaced: int = 0
    first_period: str | None = None
    last_period: str | None = None


def plan_series(
    *, archive: BinanceArchive, store: CryptoJobStore, lake: Lake, kind: ArchiveKind, symbol: str, through: date
) -> PlanResult:
    """List the archive for one series and register its files up to ``through`` (UTC date, inclusive).

    Monthly files are used where published; daily files only for days whose month has no monthly file yet.
    """
    res = PlanResult(kind, symbol)
    monthly: list[str] = []
    daily: list[str] = []
    for period in PERIODS[kind]:
        keys = archive.list_zips(prefix(kind, symbol, period))
        if period == "monthly":
            monthly = [k for k in keys if period_of(k) <= through.isoformat()[:7]]
        else:
            daily = [k for k in keys if period_of(k) <= through.isoformat()]
    months = {period_of(k) for k in monthly}
    if kind is ArchiveKind.UM_RATE:
        # the monthly rate file of the running month is only published after it ends
        months = {m for m in months if _period_bounds(m)[1].date() <= through + timedelta(days=1)}
        monthly = [k for k in monthly if period_of(k) in months]
    daily = [k for k in daily if _month_of(period_of(k)) not in months]
    rows = [(k, "monthly", period_of(k)) for k in monthly] + [(k, "daily", period_of(k)) for k in daily]
    res.monthly, res.daily = len(monthly), len(daily)
    res.new = store.upsert_tasks(kind, symbol, rows)
    # daily tasks of a month that now has a monthly file are replaced (their parts would duplicate it)
    for t in store.tasks(kinds=[kind]):
        if (
            t.symbol == symbol
            and t.period == "daily"
            and _month_of(t.period_id) in months
            and t.status is not TaskStatus.REPLACED
        ):
            part = store.part_of(t.key)
            if part is not None:
                p = lake.abspath(part)
                p.unlink(missing_ok=True)
            store.mark(t.key, TaskStatus.REPLACED, part_path=None, part_sha256=None, zone=None)
            res.replaced += 1
    pids = sorted(p for _, _, p in rows)
    res.first_period = pids[0] if pids else None
    res.last_period = pids[-1] if pids else None
    store.record_listing(kind, symbol, through, len(monthly), len(daily), res.first_period, res.last_period)
    return res


# ---------------------------------------------------------------------------------------------- ingest
@dataclass(slots=True)
class RunSummary:
    attempted: int = 0
    done: int = 0
    quarantined: int = 0
    not_published: int = 0
    failed: int = 0
    rows: int = 0
    errors: list[str] = field(default_factory=list)


def _issues_json(report: DQReport) -> list[dict[str, Any]]:
    return [
        {
            "code": i.code.value,
            "severity": i.severity.value,
            "ts": None if i.ts is None else i.ts.astimezone(UTC).isoformat(),
            "detail": i.detail,
        }
        for i in report.issues
    ]


def _status(report: DQReport) -> str:
    if not report.trustworthy:
        return "BLOCKED"
    return "WARN" if any(i.severity is DQSeverity.WARNING for i in report.issues) else "PASS"


@dataclass(frozen=True, slots=True)
class IngestOut:
    zone: Zone
    dq_status: str
    rows: int
    part_path: str
    part_sha256: str
    manifest_path: str
    first_ts: str | None
    last_ts: str | None
    missing_slots: int | None


def ingest_file(
    *,
    lake: Lake,
    store: CryptoJobStore,
    task: Task,
    body: bytes,
    raw_path: str,
    raw_sha256: str,
    fetched_at_utc: str,
    archive_sha256: str,
    config_version: str,
    max_missing_fraction: Decimal,
    thresholds_version: str,
) -> IngestOut:
    """Parse the zip, run DQ, write the part (clean or quarantine) and the manifest. Deterministic."""
    kind, sym = task.kind, task.symbol
    start, end = _period_bounds(task.period_id)
    listing = store.listing(kind, sym)
    first_p, last_p, through = listing if listing is not None else (None, None, task.period_id)
    result: CheckResult
    if kind in (ArchiveKind.SPOT_KLINES, ArchiveKind.UM_KLINES):
        parsed = parse_klines(body, task.key, symbol=sym, market="spot" if kind is ArchiveKind.SPOT_KLINES else "um")
        t = parsed.table
        expect_from = expect_to = None
        if t.num_rows:
            ts_i = pc.cast(t["ts"], pa.int64())
            lo, hi = pc.min_max(ts_i).values()
            if task.period_id == first_p:
                expect_from = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=lo.as_py() // 1000)
            ended = last_p is not None and _period_bounds(last_p)[1].date() <= date.fromisoformat(through[:10])
            if task.period_id == last_p and ended:
                expect_to = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=hi.as_py() // 1000 + 60_000_000)
        result = check_klines(
            t,
            parsed.raw_strings,
            key=task.key,
            period_start=start,
            period_end=end,
            expect_from=expect_from,
            expect_to=expect_to,
            max_missing_fraction=max_missing_fraction,
            thresholds_version=thresholds_version,
        )
        partitions = [("symbol", sym), ("interval", "1m"), ("month", _month_of(task.period_id))]
        unit = parsed.timestamp_unit
    elif kind is ArchiveKind.UM_RATE:
        parsed = parse_rate(body, task.key, symbol=sym)
        result = check_series(
            parsed.table,
            key=task.key,
            value_columns=("rate",),
            nullable_columns=(),
            period_start=start,
            period_end=end,
            step=None,
            max_missing_fraction=max_missing_fraction,
            thresholds_version=thresholds_version,
            nonnegative=False,
        )
        partitions = [("symbol", sym), ("month", _month_of(task.period_id))]
        unit = parsed.timestamp_unit
    else:
        parsed = parse_metrics(body, task.key, symbol=sym)
        result = check_series(
            parsed.table,
            key=task.key,
            value_columns=METRIC_RAW[2:],
            nullable_columns=METRIC_RAW[4:],
            period_start=start,
            period_end=end,
            step=timedelta(minutes=5),
            max_missing_fraction=max_missing_fraction,
            thresholds_version=thresholds_version,
            nonnegative=True,
        )
        partitions = [("symbol", sym), ("month", _month_of(task.period_id))]
        unit = parsed.timestamp_unit
    status = _status(result.report)
    zone = Zone.QUARANTINE if status == "BLOCKED" else Zone.CLEAN
    dataset = DATASET[kind]
    request = {"archive_key": task.key, "archive_sha256": archive_sha256}
    lineage = Lineage(
        source=SOURCE,
        dataset=dataset,
        data_version=f"{dataset}@{PARSER_VERSION}",
        parser_version=PARSER_VERSION,
        config_version=config_version,
        dq_thresholds_version=f"{DQ_VERSION}+{thresholds_version}",
        dq_status=status,
        raw_path=raw_path,
        raw_sha256=raw_sha256,
        fetched_at_utc=fetched_at_utc,
        request=request,
        job_id=None,
        chunk_key=task.key,
    )
    other = Zone.CLEAN if zone is Zone.QUARANTINE else Zone.QUARANTINE
    lake.discard_uncommitted_part(other, dataset, partitions, task.period_id)
    part = lake.write_part(zone, dataset, partitions, task.period_id, result.table, lineage)
    tsi = pc.cast(result.table["ts"], pa.int64()) if result.table.num_rows else None
    first_ts = last_ts = None
    if tsi is not None:
        lo, hi = pc.min_max(tsi).values()
        first_ts = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=lo.as_py() // 1000)).isoformat()
        last_ts = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=hi.as_py() // 1000)).isoformat()
    counts: dict[str, int] = {}
    for i in result.report.issues:
        k = f"{i.severity.value}:{i.code.value}"
        counts[k] = counts.get(k, 0) + 1
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "dataset": dataset,
        "archive_key": task.key,
        "symbol": sym,
        "period": task.period,
        "period_id": task.period_id,
        "timestamp_unit_in_file": unit,
        "status": "DONE",
        "rows": result.table.num_rows,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "part": {"zone": zone.value, "path": part.path, "sha256": part.sha256, "rows": part.rows},
        "lineage": lineage.as_dict(),
        "dq": {
            "status": status,
            "version": DQ_VERSION,
            "thresholds_version": thresholds_version,
            "stats": result.stats,
            "issue_counts": counts,
            "issues": _issues_json(result.report),
        },
    }
    name = task.key.rsplit("/", 1)[1].removesuffix(".zip")
    m = lake.write_manifest(dataset, name, manifest)
    missing = result.stats.get("missing_slots")
    return IngestOut(
        zone,
        status,
        result.table.num_rows,
        part.path,
        part.sha256,
        m,
        first_ts,
        last_ts,
        None if missing is None else int(missing),
    )


_STOP = (QuotaExhaustedError, VendorRateLimitError)


class CryptoDownloader:
    def __init__(
        self,
        *,
        archive: BinanceArchive,
        lake: Lake,
        store: CryptoJobStore,
        config_version: str,
        max_missing_fraction: Decimal,
        thresholds_version: str,
        wall_clock: Callable[[], datetime],
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self._a = archive
        self._lake = lake
        self._store = store
        self._cfg_v = config_version
        self._mmf = max_missing_fraction
        self._thr_v = thresholds_version
        self._wall = wall_clock
        self._log = log

    def run(self, *, kinds: Sequence[ArchiveKind], max_tasks: int | None = None) -> RunSummary:
        s = RunSummary()
        todo = self._store.tasks(kinds=kinds, statuses=[TaskStatus.PENDING, TaskStatus.FAILED])
        for task in todo[: max_tasks if max_tasks is not None else len(todo)]:
            s.attempted += 1
            try:
                got = self._a.fetch_verified(task.key)
            except _STOP:
                raise
            except (ChecksumMismatchError, VendorServerError, TransportError, VendorRequestError) as e:
                self._store.mark(task.key, TaskStatus.FAILED, last_error=f"{type(e).__name__}: {e}")
                s.failed += 1
                s.errors.append(f"{task.key}: {e}")
                self._log(f"FAILED {task.key}: {e}")
                continue
            if got is None:
                self._store.mark(task.key, TaskStatus.NOT_PUBLISHED, last_error="HTTP 404")
                s.not_published += 1
                continue
            day = _period_bounds(task.period_id)[0].date()
            fetched = self._wall().astimezone(UTC).isoformat()
            stem = task.key.rsplit("/", 1)[1].removesuffix(".zip")
            raw = self._lake.write_raw(
                SOURCE,
                DATASET[task.kind],
                day,
                stem,
                got.body,
                sidecar={
                    "fetched_at_utc": fetched,
                    "archive_key": task.key,
                    "checksum_line": got.checksum_line,
                    "archive_sha256": got.sha256,
                    "attempts": got.attempts,
                    "config_version": self._cfg_v,
                },
                ext="zip",
            )
            try:
                out = ingest_file(
                    lake=self._lake,
                    store=self._store,
                    task=task,
                    body=got.body,
                    raw_path=raw.path,
                    raw_sha256=raw.sha256,
                    fetched_at_utc=raw.fetched_at_utc,
                    archive_sha256=got.sha256,
                    config_version=self._cfg_v,
                    max_missing_fraction=self._mmf,
                    thresholds_version=self._thr_v,
                )
            except (VendorResponseError, LakeError) as e:
                self._store.mark(task.key, TaskStatus.FAILED, last_error=f"{type(e).__name__}: {e}", raw_path=raw.path)
                s.failed += 1
                s.errors.append(f"{task.key}: {e}")
                self._log(f"FAILED {task.key}: {e}")
                continue
            self._store.mark(
                task.key,
                TaskStatus.DONE,
                last_error=None,
                archive_sha256=got.sha256,
                raw_path=raw.path,
                part_path=out.part_path,
                part_sha256=out.part_sha256,
                zone=out.zone.value,
                rows=out.rows,
                dq_status=out.dq_status,
                first_ts=out.first_ts,
                last_ts=out.last_ts,
                missing_slots=out.missing_slots,
                manifest_path=out.manifest_path,
            )
            s.done += 1
            s.rows += out.rows
            if out.zone is Zone.QUARANTINE:
                s.quarantined += 1
            self._log(f"DONE {task.key} rows={out.rows} dq={out.dq_status}")
        return s

    def reingest(self, *, kinds: Sequence[ArchiveKind]) -> int:
        """Re-run parse + DQ from the stored raw zips (no network), e.g. after a DQ-rule change."""
        n = 0
        for task in self._store.tasks(kinds=kinds, statuses=[TaskStatus.DONE]):
            rp = self._store.raw_of(task.key)
            if rp is None:
                raise DownloadJobError(f"{task.key}: DONE without raw")
            body = self._lake.read_raw(rp)
            stem = task.key.rsplit("/", 1)[1].removesuffix(".zip")
            day = _period_bounds(task.period_id)[0].date()
            raw = self._lake.write_raw(SOURCE, DATASET[task.kind], day, stem, body, sidecar={}, ext="zip")
            out = ingest_file(
                lake=self._lake,
                store=self._store,
                task=task,
                body=body,
                raw_path=raw.path,
                raw_sha256=raw.sha256,
                fetched_at_utc=raw.fetched_at_utc,
                archive_sha256=sha256_hex(body),
                config_version=self._cfg_v,
                max_missing_fraction=self._mmf,
                thresholds_version=self._thr_v,
            )
            self._store.mark(
                task.key,
                TaskStatus.DONE,
                part_path=out.part_path,
                part_sha256=out.part_sha256,
                zone=out.zone.value,
                rows=out.rows,
                dq_status=out.dq_status,
                first_ts=out.first_ts,
                last_ts=out.last_ts,
                missing_slots=out.missing_slots,
                manifest_path=out.manifest_path,
            )
            n += 1
        return n

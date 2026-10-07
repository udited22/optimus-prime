"""Backtest input from the Parquet lake written by the Dhan downloader (B-01 <-> D-06/D-09).

The reader starts from the lake's **manifests**, never from a directory scan of parts, so every row that reaches
the backtester is traceable to a manifest, a part hash, a raw vendor file hash and the request that fetched it.
The caller names the download job specs; the reader re-plans their chunks and requires one manifest per planned
chunk (a missing manifest = incomplete download = refused). For each manifest:

* ``NO_DATA`` chunks are recorded and skipped; anything that is not ``DONE`` is refused.
* Parts in ``quarantine/`` (DQ BLOCKED) are never used. DQ ``WARN`` parts are used only with ``accept_warn``.
* The part is re-hashed against the manifest (``LakeError`` on mismatch), its embedded lineage must equal the
  manifest's lineage, its row count must match, and every part of a dataset must share one ``data_version``.
* Identical duplicate rows (overlapping chunks) are dropped and counted; conflicting duplicates raise.

Rolling expired-option rows are keyed by ATM offset. They are re-keyed into per-contract series
``NIFTY|<expiry>|<strike>|<CE|PE>`` using the row's actual strike and an expiry derived from
(``expiry_flag``, ``expiry_code``, trade date): code *n* = the *n*-th weekly (or monthly) expiry on or after the
trade date. That mapping is **UNVERIFIED** (see ``RollingOptionJobSpec``) and is written into the run metadata.
Lot sizes come from the dated lot-size history (D-04).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa

from project100c.backtest.feed import ReplayFeed
from project100c.backtest.types import OptionContract
from project100c.calendar.model import ExpiryCalendar
from project100c.core_types import OptionRight
from project100c.data.dhan.config import DhanConfig
from project100c.data.dhan.jobs import (
    CANDLE_SCHEMA,
    DATASET_CANDLES,
    DATASET_ROLLING,
    ROLLING_SCHEMA,
    CandleJobSpec,
    Chunk,
    RollingOptionJobSpec,
    data_version,
    job_id_for,
    plan_chunks,
)
from project100c.data.lake import Lake, canonical_json
from project100c.errors import BacktestDataError, LakeError
from project100c.instruments.lot_history import LotSizeHistory
from project100c.market_types import Bar
from project100c.sessions.model import IST

READER_VERSION = "LAKE-READER-2026-10-02.1"  # OD-016: conflicting duplicates become missing minutes
JobSpecT = RollingOptionJobSpec | CandleJobSpec
EXPIRY_CODE_ASSUMPTION = (
    "UNVERIFIED: Dhan rolling-option expiry_code n is taken as the n-th expiry (weekly for WEEK, monthly for MONTH) "
    "on or after the trade date; to be verified against bhavcopy on the first real pull"
)


def contract_key(underlying: str, expiry: date, strike: Decimal, right: OptionRight | str) -> str:
    s = strike.normalize()
    return f"{underlying}|{expiry.isoformat()}|{format(s, 'f')}|{OptionRight(right).value}"


@dataclass(frozen=True, slots=True)
class PartUse:
    manifest: str
    part: str
    sha256: str
    rows: int
    rows_used: int
    dq_status: str
    job_id: str
    chunk_key: str
    raw_sha256: str
    fetched_at_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "part": self.part,
            "sha256": self.sha256,
            "rows": self.rows,
            "rows_used": self.rows_used,
            "dq_status": self.dq_status,
            "job_id": self.job_id,
            "chunk_key": self.chunk_key,
            "raw_sha256": self.raw_sha256,
            "fetched_at_utc": self.fetched_at_utc,
        }


@dataclass(frozen=True, slots=True)
class Skipped:
    manifest: str
    reason: str  # NO_DATA | QUARANTINED | DQ_WARN_EXCLUDED


@dataclass(slots=True)
class LakeReadReport:
    dataset: str
    data_version: str
    job_ids: tuple[str, ...]
    parts: list[PartUse] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    identical_duplicates: int = 0
    # OD-016: same key and minute with different values across parts -> every such row dropped (WARN)
    conflict_minutes: int = 0
    conflict_rows_dropped: int = 0
    # (part, trading day) whose missing minutes (incl. conflicts) exceed the DQ threshold -> that series-day excluded
    conflict_series_days_excluded: list[tuple[str, str]] = field(default_factory=list)

    def skipped_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skipped:
            out[s.reason] = out.get(s.reason, 0) + 1
        return dict(sorted(out.items()))

    def fingerprint(self) -> str:
        """Hash of exactly which parts (by content hash) fed the run: same fingerprint == same input data."""
        body = {
            "reader": READER_VERSION,
            "dataset": self.dataset,
            "data_version": self.data_version,
            "parts": sorted((p.part, p.sha256, p.rows_used) for p in self.parts),
        }
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "data_version": self.data_version,
            "job_ids": list(self.job_ids),
            "fingerprint": self.fingerprint(),
            "parts_used": len(self.parts),
            "rows_used": sum(p.rows_used for p in self.parts),
            "skipped": self.skipped_by_reason(),
            "identical_duplicates": self.identical_duplicates,
            "conflict_minutes": self.conflict_minutes,
            "conflict_rows_dropped": self.conflict_rows_dropped,
            "conflict_series_days_excluded": [list(x) for x in self.conflict_series_days_excluded],
            "dq_warn_parts": sum(p.dq_status == "WARN" for p in self.parts),
        }


def _check_schema(table: pa.Table, schema: pa.Schema, rel: str) -> None:
    missing = [f.name for f in schema if f.name not in table.schema.names]
    if missing:
        raise BacktestDataError(f"{rel}: part lacks columns {missing}")


def _planned(specs: Sequence[JobSpecT], config: DhanConfig, rolling: bool) -> tuple[list[Chunk], tuple[str, ...]]:
    if not specs:
        raise BacktestDataError("select at least one download job spec (explicit input selection)")
    chunks: list[Chunk] = []
    for sp in specs:
        if isinstance(sp, RollingOptionJobSpec) != rolling:
            raise BacktestDataError(f"job spec {job_id_for(sp)} is of the wrong kind for this dataset")
        chunks.extend(plan_chunks(sp, config))
    uniq = {c.key: c for c in chunks}  # identical payloads across jobs are the same chunk
    return [uniq[k] for k in sorted(uniq)], tuple(sorted({job_id_for(sp) for sp in specs}))


def _read_dataset(
    lake: Lake,
    dataset: str,
    schema: pa.Schema,
    specs: Sequence[JobSpecT],
    config: DhanConfig,
    accept_warn: bool,
) -> tuple[list[tuple[PartUse, dict[str, list[Any]]]], LakeReadReport]:
    """Every chunk the job specs plan must have a manifest (manifests are named by chunk key, which is a hash of the
    exact request payload); a missing one means the download is incomplete and the run is refused."""
    chunks, job_ids = _planned(specs, config, dataset == DATASET_ROLLING)
    expected_version = data_version(dataset)
    report = LakeReadReport(dataset, expected_version, job_ids)
    out: list[tuple[PartUse, dict[str, list[Any]]]] = []
    missing: list[str] = []
    for ch in chunks:
        rel = lake.manifest_rel(dataset, ch.key)
        try:
            m = lake.read_manifest(rel)
        except LakeError:
            if not lake.abspath(rel).exists():
                missing.append(ch.key)
                continue
            raise
        if m.get("chunk_key") != ch.key or m.get("request") != dict(ch.payload):
            raise LakeError(f"{rel}: manifest does not describe the planned request for chunk {ch.key}")
        if m.get("data_version") != expected_version:
            raise BacktestDataError(
                f"{rel}: data_version {m.get('data_version')!r} != reader's {expected_version!r} (re-ingest or "
                "upgrade the reader; versions are never mixed in one run)"
            )
        status = m.get("status")
        if status == "NO_DATA":
            report.skipped.append(Skipped(rel, "NO_DATA"))
            continue
        if status != "DONE":
            raise BacktestDataError(f"{rel}: manifest status {status!r} is not DONE/NO_DATA")
        part, dq = m["part"], m["dq"]
        if part["zone"] == "quarantine" or dq["status"] == "BLOCKED":
            report.skipped.append(Skipped(rel, "QUARANTINED"))
            continue
        if part["zone"] != "clean":
            raise BacktestDataError(f"{rel}: unexpected part zone {part['zone']!r}")
        if dq["status"] == "WARN" and not accept_warn:
            report.skipped.append(Skipped(rel, "DQ_WARN_EXCLUDED"))
            continue
        if dq["status"] not in ("PASS", "WARN"):
            raise BacktestDataError(f"{rel}: unknown DQ status {dq['status']!r}")
        table = lake.read_part(part["path"], expected_sha256=part["sha256"])  # LakeError on corruption
        lineage = lake.lineage_of(part["path"])
        if lineage != m["lineage"]:
            raise LakeError(f"{rel}: part lineage differs from its manifest (tampered or mismatched part)")
        if table.num_rows != part["rows"]:
            raise LakeError(f"{rel}: part has {table.num_rows} rows, manifest says {part['rows']}")
        _check_schema(table, schema, part["path"])
        use = PartUse(
            rel,
            part["path"],
            part["sha256"],
            table.num_rows,
            0,
            dq["status"],
            str(m["job_id"]),
            str(m["chunk_key"]),
            str(m["raw"]["sha256"]),
            str(m["raw"]["fetched_at_utc"]),
        )
        out.append((use, table.to_pydict()))
    if missing:
        raise BacktestDataError(
            f"{dataset}: download incomplete: {len(missing)} of {len(chunks)} planned chunks have no manifest "
            f"(first: {missing[:3]}); run or resume the download job"
        )
    return out, report


def _in_range(ts: datetime, d0: date, d1: date) -> bool:
    return d0 <= ts.astimezone(IST).date() <= d1


def _need(v: Any, what: str, where: str) -> Any:
    if v is None:
        raise BacktestDataError(f"{where}: null {what} in a clean part")
    return v


@dataclass(frozen=True, slots=True)
class LakeData:
    """Everything a bar backtest needs, plus the provenance to reproduce it."""

    option_bars: tuple[Bar, ...]
    contracts: Mapping[str, OptionContract]
    spot: Mapping[tuple[str, datetime], Decimal]
    iv: Mapping[tuple[str, datetime], Decimal]
    candles: Mapping[str, tuple[Bar, ...]]
    reports: tuple[LakeReadReport, ...]
    assumptions: tuple[str, ...]
    date_from: date
    date_to: date

    def spot_at(self, key: str, ts: datetime) -> Decimal | None:
        return self.spot.get((key, ts))

    def feed(self, *, interval: timedelta, feed_latency: timedelta = timedelta(0)) -> ReplayFeed:
        bars: list[Bar] = list(self.option_bars)
        for label in sorted(self.candles):
            bars.extend(self.candles[label])
        return ReplayFeed.from_bars(bars, interval=interval, feed_latency=feed_latency)

    def metadata(self) -> dict[str, Any]:
        return {
            "reader": READER_VERSION,
            "window": {"from": self.date_from.isoformat(), "to": self.date_to.isoformat()},
            "inputs": [r.as_metadata() for r in self.reports],
            "contracts": len(self.contracts),
            "option_bars": len(self.option_bars),
            "candle_bars": {k: len(v) for k, v in sorted(self.candles.items())},
            "assumptions": list(self.assumptions),
        }


REGULAR_SESSION_MINUTES = 375  # 09:15..15:29 IST, one-minute bars


class _RowSet:
    """Row merge across parts with the OD-016 rule.

    Identical duplicates collapse to one row. When the same key and minute carry DIFFERENT values in different parts
    (seen on 3-Dec-2025 10:03 between Dhan's code-2 ATM and ATM+1 series), every row for that key and minute is
    dropped and the minute is treated as missing (WARN). Each part that lost a row counts it as a missing minute for
    that trading day, on top of the minutes it already lacked against a regular 375-minute session; if that exceeds
    ``max_missing_fraction`` (the DQ ``max_missing_candle_fraction``), the part's rows for that day are excluded, as a
    BLOCKING missing-candle day would be. Only days with a conflict are evaluated, so a short special session is
    judged conservatively (it may be excluded) and never silently passed.
    """

    def __init__(self, report: LakeReadReport, max_missing_fraction: Decimal) -> None:
        self._report = report
        self._frac = max_missing_fraction
        self.rows: dict[tuple[str, datetime], tuple[tuple[Any, ...], int, date]] = {}
        self._conflicted: set[tuple[str, datetime]] = set()
        self._present: dict[tuple[int, date], int] = {}
        self._dropped: dict[tuple[int, date], int] = {}

    def add(self, key: str, ts: datetime, vals: tuple[Any, ...], part: int, day: date) -> None:
        self._present[(part, day)] = self._present.get((part, day), 0) + 1
        k = (key, ts)
        if k in self._conflicted:
            self._drop(part, day)
            return
        prev = self.rows.get(k)
        if prev is None:
            self.rows[k] = (vals, part, day)
            return
        if prev[0] == vals:
            self._report.identical_duplicates += 1
            return
        del self.rows[k]
        self._conflicted.add(k)
        self._report.conflict_minutes += 1
        self._drop(prev[1], prev[2])
        self._drop(part, day)

    def _drop(self, part: int, day: date) -> None:
        self._report.conflict_rows_dropped += 1
        self._dropped[(part, day)] = self._dropped.get((part, day), 0) + 1

    def finish(self, part_names: Sequence[str]) -> list[int]:
        """Apply the missing-minute threshold; return rows used per part."""
        excluded: set[tuple[int, date]] = set()
        for (part, day), n in sorted(self._dropped.items()):
            missing = max(0, REGULAR_SESSION_MINUTES - self._present[(part, day)]) + n
            if Decimal(missing) / REGULAR_SESSION_MINUTES > self._frac:
                excluded.add((part, day))
                self._report.conflict_series_days_excluded.append((part_names[part], day.isoformat()))
        if excluded:
            self.rows = {k: v for k, v in self.rows.items() if (v[1], v[2]) not in excluded}
        used = [0] * len(part_names)
        for _, part, _ in self.rows.values():
            used[part] += 1
        return used


class DhanLakeReader:
    def __init__(
        self,
        lake: Lake,
        *,
        config: DhanConfig,
        expiries: ExpiryCalendar,
        lots: LotSizeHistory,
        accept_warn: bool = False,
        max_missing_fraction: Decimal = Decimal("0.01"),
    ) -> None:
        self._lake = lake
        self._config = config
        self._exp = expiries
        self._lots = lots
        self._accept_warn = accept_warn
        self._max_missing = max_missing_fraction
        self._expiry_cache: dict[tuple[str, int, date], date] = {}

    def expiry_for(self, flag: str, code: int, trade_date: date) -> date:
        """The contract expiry a rolling row refers to (see ``EXPIRY_CODE_ASSUMPTION``)."""
        k = (flag, code, trade_date)
        if k in self._expiry_cache:
            return self._expiry_cache[k]
        if code < 1:
            raise BacktestDataError(
                f"expiry_code {code}: annexure and SDK docs disagree on code 0 (UNVERIFIED); only codes >= 1 are "
                "mapped until verified"
            )
        if flag == "WEEK":
            exps = [e.date for e in self._exp.expiries_between(trade_date, trade_date + timedelta(days=7 * code + 14))]
        elif flag == "MONTH":
            exps = []
            y, mth = trade_date.year, trade_date.month
            while len(exps) < code:
                e = self._exp.monthly(y, mth).date
                if e >= trade_date:
                    exps.append(e)
                y, mth = (y + 1, 1) if mth == 12 else (y, mth + 1)
        else:
            raise BacktestDataError(f"unknown expiry_flag {flag!r}")
        if len(exps) < code:  # pragma: no cover - the window above always holds enough weeks
            raise BacktestDataError(f"cannot resolve expiry code {code} for {trade_date}")
        self._expiry_cache[k] = exps[code - 1]
        return exps[code - 1]

    def load(
        self,
        *,
        option_jobs: Sequence[RollingOptionJobSpec],
        candle_jobs: Sequence[CandleJobSpec],
        date_from: date,
        date_to: date,
    ) -> LakeData:
        """Load options and candles whose IST trade date lies in [date_from, date_to].

        One of the two job lists may be empty (that dataset is then not read, e.g. to load options and candles over
        different windows); selecting nothing at all is refused."""
        if date_to < date_from:
            raise BacktestDataError("date_to before date_from")
        if not option_jobs and not candle_jobs:
            raise BacktestDataError("select at least one download job spec (explicit input selection)")
        opt_bars: list[Bar] = []
        contracts: dict[str, OptionContract] = {}
        spot: dict[tuple[str, datetime], Decimal] = {}
        iv: dict[tuple[str, datetime], Decimal] = {}
        candles: dict[str, tuple[Bar, ...]] = {}
        reports: list[LakeReadReport] = []
        if option_jobs:
            opt_bars, contracts, spot, iv, rep_o = self._options(option_jobs, date_from, date_to)
            reports.append(rep_o)
        if candle_jobs:
            candles, rep_c = self._candles(candle_jobs, date_from, date_to)
            reports.append(rep_c)
        return LakeData(
            tuple(opt_bars),
            contracts,
            spot,
            iv,
            candles,
            tuple(reports),
            (EXPIRY_CODE_ASSUMPTION, f"lot sizes from {self._lots.version}"),
            date_from,
            date_to,
        )

    def _options(
        self, jobs: Sequence[RollingOptionJobSpec], d0: date, d1: date
    ) -> tuple[
        list[Bar],
        dict[str, OptionContract],
        dict[tuple[str, datetime], Decimal],
        dict[tuple[str, datetime], Decimal],
        LakeReadReport,
    ]:
        parts, report = _read_dataset(
            self._lake, DATASET_ROLLING, ROLLING_SCHEMA, jobs, self._config, self._accept_warn
        )
        rs = _RowSet(report, self._max_missing)
        contracts: dict[str, OptionContract] = {}
        lots: dict[str, int] = {}
        for i, (use, cols) in enumerate(parts):
            n = len(cols["ts"])
            for j in range(n):
                ts: datetime = cols["ts"][j]
                if not _in_range(ts, d0, d1):
                    continue
                where = f"{use.part} row {j}"
                trade_date = ts.astimezone(IST).date()
                underlying = str(cols["underlying"][j])
                strike = Decimal(_need(cols["strike"][j], "strike", where))
                right = OptionRight(cols["right"][j])
                expiry = self.expiry_for(str(cols["expiry_flag"][j]), int(cols["expiry_code"][j]), trade_date)
                key = contract_key(underlying, expiry, strike, right)
                lot = self._lots.lot_size_for(self._exp, expiry, trade_date)
                if lots.setdefault(key, lot) != lot:
                    raise BacktestDataError(f"{key}: lot size changes within the data ({lots[key]} -> {lot})")
                if key not in contracts:
                    contracts[key] = OptionContract(key, underlying, expiry, strike, right, lot)
                vals = (
                    _need(cols["open"][j], "open", where),
                    _need(cols["high"][j], "high", where),
                    _need(cols["low"][j], "low", where),
                    _need(cols["close"][j], "close", where),
                    int(_need(cols["volume"][j], "volume", where)),
                    None if cols["oi"][j] is None else int(cols["oi"][j]),
                    _need(cols["spot"][j], "spot", where),
                    cols["iv"][j],
                )
                rs.add(key, ts, vals, i, trade_date)
        used = rs.finish([u.part for u, _ in parts])
        report.parts = [_with_used(u, used[i]) for i, (u, _) in enumerate(parts)]
        bars: list[Bar] = []
        spot: dict[tuple[str, datetime], Decimal] = {}
        iv: dict[tuple[str, datetime], Decimal] = {}
        for (key, ts), ((o, h, lo, c, v, oi, sp, vol), _, _) in sorted(rs.rows.items()):
            bars.append(Bar(key, ts.astimezone(IST), o, h, lo, c, v, oi))
            spot[(key, ts.astimezone(IST))] = sp
            if vol is not None:
                iv[(key, ts.astimezone(IST))] = vol
        return bars, dict(sorted(contracts.items())), spot, iv, report

    def _candles(
        self, jobs: Sequence[CandleJobSpec], d0: date, d1: date
    ) -> tuple[dict[str, tuple[Bar, ...]], LakeReadReport]:
        parts, report = _read_dataset(self._lake, DATASET_CANDLES, CANDLE_SCHEMA, jobs, self._config, self._accept_warn)
        rs = _RowSet(report, self._max_missing)
        for i, (use, cols) in enumerate(parts):
            for j in range(len(cols["ts"])):
                ts: datetime = cols["ts"][j]
                if not _in_range(ts, d0, d1):
                    continue
                where = f"{use.part} row {j}"
                key = str(cols["label"][j])
                vals = (
                    _need(cols["open"][j], "open", where),
                    _need(cols["high"][j], "high", where),
                    _need(cols["low"][j], "low", where),
                    _need(cols["close"][j], "close", where),
                    int(_need(cols["volume"][j], "volume", where)),
                    None if cols["oi"][j] is None else int(cols["oi"][j]),
                )
                rs.add(key, ts, vals, i, ts.astimezone(IST).date())
        used = rs.finish([u.part for u, _ in parts])
        report.parts = [_with_used(u, used[i]) for i, (u, _) in enumerate(parts)]
        out: dict[str, list[Bar]] = {}
        for (key, ts), ((o, h, lo, c, v, oi), _, _) in sorted(rs.rows.items()):
            out.setdefault(key, []).append(Bar(key, ts.astimezone(IST), o, h, lo, c, v, oi))
        return {k: tuple(v) for k, v in sorted(out.items())}, report


def _with_used(u: PartUse, used: int) -> PartUse:
    return PartUse(
        u.manifest, u.part, u.sha256, u.rows, used, u.dq_status, u.job_id, u.chunk_key, u.raw_sha256, u.fetched_at_utc
    )

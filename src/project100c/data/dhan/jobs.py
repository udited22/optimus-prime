"""Resumable Dhan download jobs (backlog D-06).

A job spec is planned into deterministic *chunks* (one vendor call each). Chunk state lives in SQLite
(``lake/jobs/dhan_jobs.sqlite`` by default). A chunk is marked DONE only after its raw bytes, Parquet part
and manifest are all on disk, so killing the process at any point and re-running resumes without
duplicates: an unfinished chunk is simply redone, and its deterministic part name replaces any leftover.

Statuses: PENDING -> DONE | NO_DATA | FAILED (retried on the next run) | REJECTED (bad request, not retried).
Auth, subscription, quota and persistent rate-limit errors stop the run and leave the chunk PENDING.
A run that leaves FAILED/REJECTED chunks raises DownloadIncompleteError with the list.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from project100c.core_types import OptionRight
from project100c.data.dhan.client import DataEndpoint, DhanDataClient, DhanReply, loads_decimal
from project100c.data.dhan.config import DhanConfig
from project100c.data.dhan.dq import DQStatus, IngestContext, SeriesKind, check_ingest, dq_status
from project100c.data.dhan.parse import (
    IV_SCALE,
    PARSER_VERSION,
    PRICE_SCALE,
    ROLLING_FIELDS,
    ParsedColumns,
    parse_candles,
    parse_rolling_option,
)
from project100c.data.lake import Lake, Lineage, PartRef, RawRef, Zone, canonical_json, sha256_hex
from project100c.dq.checks import DQReport
from project100c.errors import (
    DownloadIncompleteError,
    DownloadJobError,
    EndpointNotAllowedError,
    LakeError,
    MissingCredentialError,
    QuotaExhaustedError,
    TransportError,
    VendorAuthError,
    VendorRateLimitError,
    VendorRequestError,
    VendorResponseError,
    VendorServerError,
    VendorSubscriptionError,
)

SOURCE = "dhan"
SCHEMA_VERSION = 1
DATASET_ROLLING = "dhan_rolling_option"
DATASET_CANDLES = "dhan_intraday"
MANIFEST_VERSION = "DHAN-MANIFEST-1"
DQ_CHECKS_VERSION = "dhan-dq-2026-10-02.2"  # ingest DQ rules (dq.py); .1 special sessions WARN; .2 OD-015


def data_version(dataset: str) -> str:
    return f"{dataset}@schema{SCHEMA_VERSION}+{PARSER_VERSION}"


# ---------------------------------------------------------------------------------------------- specs
class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_date: date
    to_date: date  # EXCLUSIVE
    window_days: int = Field(default=30, gt=0)
    interval_min: Literal[1, 5, 15, 25, 60] = 1

    @model_validator(mode="after")
    def _range(self) -> _Spec:
        if self.to_date <= self.from_date:
            raise ValueError("to_date (exclusive) must be after from_date")
        return self


class RollingOptionJobSpec(_Spec):
    """Expired options, rolling by ATM offset (S31). Defaults: NIFTY weekly, near expiry, ATM±10, CE+PE, all fields.

    ``expiry_codes``: VERIFIED on the first real pull (2-Oct-2026): 1 = the nearest expiry on or after the trade
    date (the expiry day itself included: the 22-Sep and 29-Sep-2026 code-1 ATM calls settle to intrinsic at
    15:39 with IV 0), 2 = the next, 3 = the one after. Code 0 is rejected by Dhan (DH-905 "expiryCode is
    required"), so it is not allowed here.
    """

    kind: Literal["rolling_option"] = "rolling_option"
    underlying: str = "NIFTY"
    underlying_security_id: int = 13
    exchange_segment: Literal["NSE_FNO", "BSE_FNO"] = "NSE_FNO"  # BSE_FNO: SENSEX (verified 3-Oct-2026)
    instrument: Literal["OPTIDX"] = "OPTIDX"
    expiry_flag: Literal["WEEK", "MONTH"] = "WEEK"
    expiry_codes: tuple[int, ...] = (1,)
    strike_offsets: tuple[int, ...] = tuple(range(-10, 11))
    rights: tuple[OptionRight, ...] = (OptionRight.CE, OptionRight.PE)
    required_data: tuple[str, ...] = ROLLING_FIELDS

    @model_validator(mode="after")
    def _check(self) -> RollingOptionJobSpec:
        if not self.strike_offsets or any(abs(o) > 10 for o in self.strike_offsets):
            raise ValueError("strike_offsets must be within ATM-10..ATM+10 (S31)")
        if len(set(self.strike_offsets)) != len(self.strike_offsets) or len(set(self.rights)) != len(self.rights):
            raise ValueError("duplicate strike offsets or rights")
        if not self.expiry_codes or any(c not in (1, 2, 3) for c in self.expiry_codes):
            raise ValueError("expiry_codes must be in 1..3 (Dhan rejects 0 for rolling options: DH-905)")
        bad = [f for f in self.required_data if f not in ROLLING_FIELDS]
        if bad or not {"open", "high", "low", "close"} <= set(self.required_data):
            raise ValueError(f"required_data must include OHLC and only {ROLLING_FIELDS}; bad={bad}")
        return self


class CandleJobSpec(_Spec):
    """Intraday candles (S57) for an index (IDX_I: NIFTY 13, SENSEX 51) or an active futures contract (NSE_FNO or
    BSE_FNO)."""

    kind: Literal["intraday"] = "intraday"
    label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$")
    security_id: str = Field(pattern=r"^[0-9]+$")
    exchange_segment: Literal["IDX_I", "NSE_FNO", "BSE_FNO"]
    instrument: Literal["INDEX", "FUTIDX"]
    oi: bool = False

    @model_validator(mode="after")
    def _check(self) -> CandleJobSpec:
        if (self.exchange_segment == "IDX_I") != (self.instrument == "INDEX"):
            raise ValueError("INDEX must use IDX_I and FUTIDX must use NSE_FNO or BSE_FNO")
        if self.instrument == "INDEX" and self.oi:
            raise ValueError("an index has no open interest")
        return self

    @property
    def series_kind(self) -> SeriesKind:
        return SeriesKind.INDEX if self.instrument == "INDEX" else SeriesKind.FUTURE


JobSpec = Annotated[RollingOptionJobSpec | CandleJobSpec, Field(discriminator="kind")]
_SPEC: TypeAdapter[RollingOptionJobSpec | CandleJobSpec] = TypeAdapter(JobSpec)


def parse_spec(obj: Mapping[str, Any]) -> RollingOptionJobSpec | CandleJobSpec:
    try:
        return _SPEC.validate_python(dict(obj))
    except ValidationError as e:
        raise DownloadJobError(f"invalid job spec: {e}") from e


def spec_json(spec: RollingOptionJobSpec | CandleJobSpec) -> str:
    return canonical_json(spec.model_dump(mode="json")).decode()


# Version of the spec -> chunk-payload mapping (plan_chunks). It is part of the job id, so changing how a spec is
# turned into requests creates new jobs instead of colliding with stored plans. 2 = rolling-option toDate sent
# inclusive (the vendor treats it as inclusive, verified 2-Oct-2026).
PLAN_VERSION = 2


def job_id_for(spec: RollingOptionJobSpec | CandleJobSpec) -> str:
    return f"{spec.kind}-{sha256_hex((spec_json(spec) + f'|plan{PLAN_VERSION}').encode())[:16]}"


# ---------------------------------------------------------------------------------------------- planning
@dataclass(frozen=True, slots=True)
class Chunk:
    key: str
    seq: int
    endpoint: DataEndpoint
    payload: Mapping[str, Any]
    window_from: date
    window_to: date  # exclusive
    meta: Mapping[str, Any] = field(default_factory=dict)


def _strike_label(offset: int) -> str:
    return "ATM" if offset == 0 else f"ATM{offset:+d}"


def _windows(spec: _Spec, max_days: int) -> Iterator[tuple[date, date]]:
    if spec.window_days > max_days:
        raise DownloadJobError(f"window_days {spec.window_days} exceeds the vendor maximum {max_days}")
    d = spec.from_date
    while d < spec.to_date:
        e = min(d + timedelta(days=spec.window_days), spec.to_date)
        yield d, e
        d = e


def _key(endpoint: DataEndpoint, payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json({"endpoint": endpoint.value, "payload": dict(payload)}))[:24]


def plan_chunks(spec: RollingOptionJobSpec | CandleJobSpec, config: DhanConfig) -> list[Chunk]:
    out: list[Chunk] = []
    if isinstance(spec, RollingOptionJobSpec):
        for w0, w1 in _windows(spec, config.rolling_option_max_window_days):
            for code in spec.expiry_codes:
                for right in spec.rights:
                    for off in spec.strike_offsets:
                        payload = {
                            "exchangeSegment": spec.exchange_segment,
                            "interval": str(spec.interval_min),
                            "securityId": spec.underlying_security_id,
                            "instrument": spec.instrument,
                            "expiryFlag": spec.expiry_flag,
                            "expiryCode": code,
                            "strike": _strike_label(off),
                            "drvOptionType": "CALL" if right is OptionRight.CE else "PUT",
                            "requiredData": list(spec.required_data),
                            "fromDate": w0.isoformat(),
                            # INCLUSIVE in practice (verified 2-Oct-2026: 28..29-Sep returns both days), although
                            # S31 says non-inclusive; so the last day of the [w0, w1) window is sent
                            "toDate": (w1 - timedelta(days=1)).isoformat(),
                        }
                        meta = {"expiry_code": code, "right": right.value, "strike_offset": off}
                        ep = DataEndpoint.ROLLING_OPTION
                        out.append(Chunk(_key(ep, payload), len(out), ep, payload, w0, w1, meta))
    else:
        for w0, w1 in _windows(spec, config.intraday_max_window_days):
            last = w1 - timedelta(days=1)
            payload = {
                "securityId": spec.security_id,
                "exchangeSegment": spec.exchange_segment,
                "instrument": spec.instrument,
                "interval": str(spec.interval_min),
                "oi": spec.oi,
                "fromDate": f"{w0.isoformat()} 09:00:00",
                "toDate": f"{last.isoformat()} 16:00:00",
            }
            ep = DataEndpoint.INTRADAY
            out.append(Chunk(_key(ep, payload), len(out), ep, payload, w0, w1, {}))
    keys = [c.key for c in out]
    if len(set(keys)) != len(keys):  # pragma: no cover - payloads differ by construction
        raise DownloadJobError("duplicate chunk keys in plan")
    return out


@dataclass(frozen=True, slots=True)
class PlanEstimate:
    chunks: int
    min_minutes_at_rate: float
    days_at_daily_budget: int


def estimate(chunks: list[Chunk], config: DhanConfig) -> PlanEstimate:
    n = len(chunks)
    return PlanEstimate(n, n / config.requests_per_second / 60.0, -(-n // config.requests_per_day))


# ---------------------------------------------------------------------------------------------- store
class ChunkStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    NO_DATA = "NO_DATA"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(
  job_id TEXT NOT NULL, chunk_key TEXT NOT NULL, seq INTEGER NOT NULL, endpoint TEXT NOT NULL,
  payload_json TEXT NOT NULL, meta_json TEXT NOT NULL, window_from TEXT NOT NULL, window_to TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  raw_path TEXT, raw_sha256 TEXT, part_path TEXT, part_sha256 TEXT, rows INTEGER, dq_status TEXT,
  manifest_path TEXT, updated_utc TEXT NOT NULL,
  PRIMARY KEY (job_id, chunk_key));
CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY, requests INTEGER NOT NULL);
"""


@dataclass(frozen=True, slots=True)
class ChunkRow:
    chunk: Chunk
    status: ChunkStatus
    attempts: int
    last_error: str | None
    part_path: str | None
    part_sha256: str | None
    manifest_path: str | None
    rows: int | None
    dq_status: str | None


class JobStore:
    """SQLite job state + the persisted daily request counter (implements ratelimit.QuotaStore)."""

    def __init__(self, path: Path, *, wall_clock: Callable[[], datetime]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=60)  # several backfill workers share it
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

    # quota
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

    # jobs
    def ensure_job(self, spec: RollingOptionJobSpec | CandleJobSpec, chunks: list[Chunk]) -> str:
        jid = job_id_for(spec)
        sj = spec_json(spec)
        with self._tx() as db:
            row = db.execute("SELECT spec_json FROM jobs WHERE job_id=?", (jid,)).fetchone()
            if row is not None:
                if row[0] != sj:
                    raise DownloadJobError(f"job {jid} exists with a different spec")
                have = {r[0] for r in db.execute("SELECT chunk_key FROM chunks WHERE job_id=?", (jid,))}
                if have != {c.key for c in chunks}:
                    raise DownloadJobError(f"job {jid}: stored chunk plan differs from the current plan")
                return jid
            db.execute("INSERT INTO jobs VALUES(?,?,?)", (jid, sj, self._now()))
            for c in chunks:
                db.execute(
                    "INSERT INTO chunks(job_id, chunk_key, seq, endpoint, payload_json, meta_json, window_from, "
                    "window_to, status, updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        jid,
                        c.key,
                        c.seq,
                        c.endpoint.value,
                        canonical_json(dict(c.payload)).decode(),
                        canonical_json(dict(c.meta)).decode(),
                        c.window_from.isoformat(),
                        c.window_to.isoformat(),
                        ChunkStatus.PENDING.value,
                        self._now(),
                    ),
                )
        return jid

    def spec(self, job_id: str) -> RollingOptionJobSpec | CandleJobSpec:
        row = self._db.execute("SELECT spec_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise DownloadJobError(f"unknown job {job_id}")
        return parse_spec(loads_decimal(row[0].encode()))

    def chunks(self, job_id: str, statuses: frozenset[ChunkStatus] | None = None) -> list[ChunkRow]:
        rows = self._db.execute(
            "SELECT chunk_key, seq, endpoint, payload_json, meta_json, window_from, window_to, status, attempts, "
            "last_error, part_path, part_sha256, manifest_path, rows, dq_status FROM chunks WHERE job_id=? "
            "ORDER BY seq",
            (job_id,),
        ).fetchall()
        out: list[ChunkRow] = []
        for r in rows:
            st = ChunkStatus(r[7])
            if statuses is not None and st not in statuses:
                continue
            ch = Chunk(
                r[0],
                int(r[1]),
                DataEndpoint(r[2]),
                loads_decimal(r[3].encode()),
                date.fromisoformat(r[5]),
                date.fromisoformat(r[6]),
                loads_decimal(r[4].encode()),
            )
            out.append(ChunkRow(ch, st, int(r[8]), r[9], r[10], r[11], r[12], r[13], r[14]))
        return out

    def counts(self, job_id: str) -> dict[str, int]:
        return {
            str(s): int(n)
            for s, n in self._db.execute(
                "SELECT status, COUNT(*) FROM chunks WHERE job_id=? GROUP BY status", (job_id,)
            )
        }

    def mark(
        self,
        job_id: str,
        key: str,
        status: ChunkStatus,
        *,
        error: str | None = None,
        attempts: int = 0,
        raw: RawRef | None = None,
        part: PartRef | None = None,
        rows: int | None = None,
        dq: str | None = None,
        manifest: str | None = None,
    ) -> None:
        with self._tx() as db:
            cur = db.execute(
                "UPDATE chunks SET status=?, last_error=?, attempts=attempts+?, raw_path=COALESCE(?, raw_path), "
                "raw_sha256=COALESCE(?, raw_sha256), part_path=?, part_sha256=?, rows=?, dq_status=?, "
                "manifest_path=?, updated_utc=? WHERE job_id=? AND chunk_key=?",
                (
                    status.value,
                    error,
                    attempts,
                    raw.path if raw else None,
                    raw.sha256 if raw else None,
                    part.path if part else None,
                    part.sha256 if part else None,
                    rows,
                    dq,
                    manifest,
                    self._now(),
                    job_id,
                    key,
                ),
            )
            if cur.rowcount != 1:
                raise DownloadJobError(f"chunk {job_id}/{key} not found")

    def raw_of(self, job_id: str, key: str) -> str | None:
        r = self._db.execute("SELECT raw_path FROM chunks WHERE job_id=? AND chunk_key=?", (job_id, key)).fetchone()
        return None if r is None or r[0] is None else str(r[0])

    def note_error(self, job_id: str, key: str, error: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE chunks SET last_error=?, attempts=attempts+1, updated_utc=? WHERE job_id=? AND chunk_key=?",
                (error, self._now(), job_id, key),
            )


# ---------------------------------------------------------------------------------------------- ingest
_DEC = pa.decimal128(18, PRICE_SCALE)
_IV = pa.decimal128(18, IV_SCALE)
_TS = pa.timestamp("ns", tz="UTC")

ROLLING_SCHEMA = pa.schema(
    [
        ("ts", _TS),
        ("underlying", pa.string()),
        ("underlying_security_id", pa.string()),
        ("expiry_flag", pa.string()),
        ("expiry_code", pa.int16()),
        ("strike_label", pa.string()),
        ("strike_offset", pa.int16()),
        ("right", pa.string()),
        ("interval_min", pa.int16()),
        ("open", _DEC),
        ("high", _DEC),
        ("low", _DEC),
        ("close", _DEC),
        ("volume", pa.int64()),
        ("oi", pa.int64()),
        ("iv", _IV),
        ("strike", _DEC),
        ("spot", _DEC),
        ("source_chunk", pa.string()),
    ]
)

CANDLE_SCHEMA = pa.schema(
    [
        ("ts", _TS),
        ("label", pa.string()),
        ("security_id", pa.string()),
        ("exchange_segment", pa.string()),
        ("instrument", pa.string()),
        ("interval_min", pa.int16()),
        ("open", _DEC),
        ("high", _DEC),
        ("low", _DEC),
        ("close", _DEC),
        ("volume", pa.int64()),
        ("oi", pa.int64()),
        ("source_chunk", pa.string()),
    ]
)


def _table(
    schema: pa.Schema, n: int, const: Mapping[str, Any], cols: Mapping[str, list[Any]], ts: list[datetime]
) -> Any:
    arrays: dict[str, Any] = {}
    for f in schema:
        if f.name == "ts":
            arrays["ts"] = pa.array(ts, type=_TS)
        elif f.name in const:
            arrays[f.name] = pa.array([const[f.name]] * n, type=f.type)
        elif f.name in cols:
            arrays[f.name] = pa.array(cols[f.name], type=f.type)
        else:
            arrays[f.name] = pa.nulls(n, type=f.type)
    return pa.table(arrays, schema=schema)


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: ChunkStatus
    rows: int
    dq: DQStatus | None
    report: DQReport | None
    part: PartRef | None
    manifest_path: str


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


def ingest_chunk(
    *,
    lake: Lake,
    ctx: IngestContext,
    config_version: str,
    job_id: str,
    spec: RollingOptionJobSpec | CandleJobSpec,
    chunk: Chunk,
    raw: RawRef,
    no_data_code: str | None,
) -> IngestResult:
    """Parse the STORED raw bytes, run DQ, write the part (clean or quarantine) and the manifest.

    Deterministic: the same raw ref always yields byte-identical outputs.
    """
    rolling = isinstance(spec, RollingOptionJobSpec)
    dataset = DATASET_ROLLING if rolling else DATASET_CANDLES
    base: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "dataset": dataset,
        "data_version": data_version(dataset),
        "job_id": job_id,
        "chunk_key": chunk.key,
        "endpoint": chunk.endpoint.value,
        "request": dict(chunk.payload),
        "window": {"from": chunk.window_from.isoformat(), "to_exclusive": chunk.window_to.isoformat()},
        "raw": {"path": raw.path, "sha256": raw.sha256, "size": raw.size, "fetched_at_utc": raw.fetched_at_utc},
        "config_version": config_version,
        "dq_checks_version": DQ_CHECKS_VERSION,
    }
    if no_data_code is not None:
        m = lake.write_manifest(
            dataset,
            chunk.key,
            {**base, "status": ChunkStatus.NO_DATA.value, "rows": 0, "no_data_reason": f"vendor code {no_data_code}"},
        )
        return IngestResult(ChunkStatus.NO_DATA, 0, None, None, None, m)
    parsed = loads_decimal(lake.read_raw(raw.path))
    cols: ParsedColumns
    if isinstance(spec, RollingOptionJobSpec):
        right = OptionRight(chunk.meta["right"])
        cols = parse_rolling_option(parsed, right, spec.required_data)
        kind, offset = SeriesKind.OPTION, int(chunk.meta["strike_offset"])
        series_key = f"{spec.underlying}|{spec.expiry_flag}|{chunk.meta['expiry_code']}|{_strike_label(offset)}|{right}"
    else:
        cols = parse_candles(parsed, with_oi=spec.oi)
        kind, offset, series_key = spec.series_kind, None, spec.label
    if cols.no_data_reason is not None:
        m = lake.write_manifest(
            dataset,
            chunk.key,
            {**base, "status": ChunkStatus.NO_DATA.value, "rows": 0, "no_data_reason": cols.no_data_reason},
        )
        return IngestResult(ChunkStatus.NO_DATA, 0, None, None, None, m)
    report = check_ingest(
        cols,
        series_key=series_key,
        kind=kind,
        window_from=chunk.window_from,
        window_to_exclusive=chunk.window_to,
        interval_min=spec.interval_min,
        strike_offset=offset,
        ctx=ctx,
    )
    status = dq_status(report)
    zone = Zone.QUARANTINE if status is DQStatus.BLOCKED else Zone.CLEAN
    lineage = Lineage(
        source=SOURCE,
        dataset=dataset,
        data_version=data_version(dataset),
        parser_version=PARSER_VERSION,
        config_version=config_version,
        dq_thresholds_version=report.thresholds_version,
        dq_status=status.value,
        raw_path=raw.path,
        raw_sha256=raw.sha256,
        fetched_at_utc=raw.fetched_at_utc,
        request=dict(chunk.payload),
        job_id=job_id,
        chunk_key=chunk.key,
    )
    n = cols.rows
    if isinstance(spec, RollingOptionJobSpec):
        const = {
            "underlying": spec.underlying,
            "underlying_security_id": str(spec.underlying_security_id),
            "expiry_flag": spec.expiry_flag,
            "expiry_code": int(chunk.meta["expiry_code"]),
            "strike_label": _strike_label(int(chunk.meta["strike_offset"])),
            "strike_offset": int(chunk.meta["strike_offset"]),
            "right": str(chunk.meta["right"]),
            "interval_min": spec.interval_min,
            "source_chunk": chunk.key,
        }
        table = _table(ROLLING_SCHEMA, n, const, cols.cols, cols.ts)
        partitions = [
            ("underlying", spec.underlying),
            ("expiry_flag", spec.expiry_flag),
            ("expiry_code", str(chunk.meta["expiry_code"])),
            ("strike", _strike_label(int(chunk.meta["strike_offset"]))),
            ("right", str(chunk.meta["right"])),
            ("window_from", chunk.window_from.isoformat()),
        ]
    else:
        c = dict(cols.cols)
        if "open_interest" in c:
            c["oi"] = c.pop("open_interest")
        const = {
            "label": spec.label,
            "security_id": spec.security_id,
            "exchange_segment": spec.exchange_segment,
            "instrument": spec.instrument,
            "interval_min": spec.interval_min,
            "source_chunk": chunk.key,
        }
        table = _table(CANDLE_SCHEMA, n, const, c, cols.ts)
        partitions = [
            ("label", spec.label),
            ("interval", f"{spec.interval_min}m"),
            ("window_from", chunk.window_from.isoformat()),
        ]
    other = Zone.CLEAN if zone is Zone.QUARANTINE else Zone.QUARANTINE
    lake.discard_uncommitted_part(other, dataset, partitions, chunk.key)  # leftover of a crashed attempt
    part = lake.write_part(zone, dataset, partitions, chunk.key, table, lineage)
    counts: dict[str, int] = {}
    for i in report.issues:
        k = f"{i.severity.value}:{i.code.value}"
        counts[k] = counts.get(k, 0) + 1
    manifest = {
        **base,
        "status": ChunkStatus.DONE.value,
        "rows": n,
        "part": {"zone": zone.value, "path": part.path, "sha256": part.sha256, "rows": part.rows},
        "lineage": lineage.as_dict(),
        "dq": {
            "status": status.value,
            "thresholds_version": report.thresholds_version,
            "issue_counts": counts,
            "issues": _issues_json(report),
        },
    }
    m = lake.write_manifest(dataset, chunk.key, manifest)
    return IngestResult(ChunkStatus.DONE, n, status, report, part, m)


# ---------------------------------------------------------------------------------------------- run
@dataclass(slots=True)
class RunSummary:
    job_id: str
    attempted: int = 0
    done: int = 0
    no_data: int = 0
    failed: int = 0
    rejected: int = 0
    quarantined: int = 0
    rows: int = 0
    requests: int = 0
    errors: list[str] = field(default_factory=list)


_STOP = (
    VendorAuthError,
    VendorSubscriptionError,
    QuotaExhaustedError,
    MissingCredentialError,
    EndpointNotAllowedError,
)


class DhanDownloader:
    def __init__(
        self,
        *,
        client: DhanDataClient,
        config: DhanConfig,
        lake: Lake,
        store: JobStore,
        ctx: IngestContext,
        before_commit: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._cfg = config
        self._lake = lake
        self._store = store
        self._ctx = ctx
        self._hook = before_commit

    def create(self, spec: RollingOptionJobSpec | CandleJobSpec) -> str:
        return self._store.ensure_job(spec, plan_chunks(spec, self._cfg))

    def run(self, job_id: str, *, max_chunks: int | None = None) -> RunSummary:
        spec = self._store.spec(job_id)
        todo = self._store.chunks(job_id, frozenset({ChunkStatus.PENDING, ChunkStatus.FAILED}))
        s = RunSummary(job_id)
        dataset = DATASET_ROLLING if isinstance(spec, RollingOptionJobSpec) else DATASET_CANDLES
        for row in todo[: max_chunks if max_chunks is not None else len(todo)]:
            ch = row.chunk
            s.attempted += 1
            try:
                reply: DhanReply = self._client.call(ch.endpoint, ch.payload)
            except _STOP as e:
                self._store.note_error(job_id, ch.key, f"{type(e).__name__}: {e}")
                raise
            except VendorRateLimitError as e:
                self._store.mark(job_id, ch.key, ChunkStatus.FAILED, error=f"{type(e).__name__}: {e}", attempts=1)
                raise
            except VendorRequestError as e:
                self._store.mark(job_id, ch.key, ChunkStatus.REJECTED, error=f"{type(e).__name__}: {e}", attempts=1)
                s.rejected += 1
                s.errors.append(f"{ch.key}: {e}")
                continue
            except (VendorServerError, TransportError, VendorResponseError) as e:
                self._store.mark(job_id, ch.key, ChunkStatus.FAILED, error=f"{type(e).__name__}: {e}", attempts=1)
                s.failed += 1
                s.errors.append(f"{ch.key}: {e}")
                continue
            s.requests += reply.attempts
            raw = self._lake.write_raw(
                SOURCE,
                dataset,
                ch.window_from,
                ch.key,
                reply.body,
                sidecar={
                    "fetched_at_utc": reply.fetched_at_utc,
                    "endpoint": ch.endpoint.value,
                    "request": dict(ch.payload),
                    "http_status": reply.http_status,
                    "attempts": reply.attempts,
                    "vendor_code": reply.vendor_code,
                    "job_id": job_id,
                    "chunk_key": ch.key,
                    "config_version": self._cfg.version,
                },
            )
            try:
                res = ingest_chunk(
                    lake=self._lake,
                    ctx=self._ctx,
                    config_version=self._cfg.version,
                    job_id=job_id,
                    spec=spec,
                    chunk=ch,
                    raw=raw,
                    no_data_code=reply.vendor_code if reply.no_data else None,
                )
            except (VendorResponseError, LakeError) as e:
                self._store.mark(
                    job_id, ch.key, ChunkStatus.FAILED, error=f"{type(e).__name__}: {e}", attempts=1, raw=raw
                )
                s.failed += 1
                s.errors.append(f"{ch.key}: {e}")
                continue
            if self._hook is not None:
                self._hook(ch.key)  # tests: simulated crash between file writes and the commit
            self._store.mark(
                job_id,
                ch.key,
                res.status,
                attempts=1,
                raw=raw,
                part=res.part,
                rows=res.rows,
                dq=None if res.dq is None else res.dq.value,
                manifest=res.manifest_path,
            )
            if res.status is ChunkStatus.NO_DATA:
                s.no_data += 1
            else:
                s.done += 1
                s.rows += res.rows
                if res.dq is DQStatus.BLOCKED:
                    s.quarantined += 1
        if s.failed or s.rejected:
            raise DownloadIncompleteError(
                f"job {job_id}: {s.failed} FAILED (retried on re-run), {s.rejected} REJECTED; first: {s.errors[:3]}"
            )
        return s


def reingest_blocked(
    *, lake: Lake, store: JobStore, ctx: IngestContext, config: DhanConfig, job_id: str
) -> list[tuple[str, str, str]]:
    """Re-run ingest from the STORED raw bytes for every DONE chunk whose DQ was BLOCKED (no network call).

    Used after a DQ-rule fix. Deterministic: the raw file is not touched; the quarantine part is replaced by a
    clean one if the chunk now passes (or rewritten in quarantine if it still fails). Returns
    (chunk key, old DQ status, new DQ status) for each chunk re-ingested.
    """
    spec = store.spec(job_id)
    dataset = DATASET_ROLLING if isinstance(spec, RollingOptionJobSpec) else DATASET_CANDLES
    out: list[tuple[str, str, str]] = []
    for r in store.chunks(job_id, frozenset({ChunkStatus.DONE})):
        if r.dq_status != DQStatus.BLOCKED.value:
            continue
        ch = r.chunk
        row = store.raw_of(job_id, ch.key)
        if row is None:
            raise DownloadJobError(f"{job_id}/{ch.key}: DONE without a raw file")
        body = lake.read_raw(row)
        raw = lake.write_raw(SOURCE, dataset, ch.window_from, ch.key, body, sidecar={})  # same bytes -> same ref
        res = ingest_chunk(
            lake=lake,
            ctx=ctx,
            config_version=config.version,
            job_id=job_id,
            spec=spec,
            chunk=ch,
            raw=raw,
            no_data_code=None,
        )
        store.mark(
            job_id,
            ch.key,
            res.status,
            raw=raw,
            part=res.part,
            rows=res.rows,
            dq=None if res.dq is None else res.dq.value,
            manifest=res.manifest_path,
        )
        out.append((ch.key, DQStatus.BLOCKED.value, "-" if res.dq is None else res.dq.value))
    return out


def verify_job(lake: Lake, store: JobStore, job_id: str) -> list[str]:
    """Re-hash every committed part and check its manifest; returns problems (empty == all good)."""
    problems: list[str] = []
    for r in store.chunks(job_id, frozenset({ChunkStatus.DONE, ChunkStatus.NO_DATA})):
        if r.manifest_path is None:
            problems.append(f"{r.chunk.key}: no manifest")
            continue
        try:
            m = lake.read_manifest(r.manifest_path)
            if r.status is ChunkStatus.DONE:
                if r.part_path is None or r.part_sha256 is None:
                    problems.append(f"{r.chunk.key}: DONE without part")
                    continue
                lake.read_part(r.part_path, expected_sha256=r.part_sha256)
                if m.get("part", {}).get("sha256") != r.part_sha256:
                    problems.append(f"{r.chunk.key}: manifest/part hash disagree")
        except LakeError as e:
            problems.append(f"{r.chunk.key}: {e}")
    return problems

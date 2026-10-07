"""Dhan instrument list (S58): a PUBLIC file, fetched WITHOUT any credential header.

The raw CSV is stored immutably in ``raw/dhan/instrument_list/date=<IST date>/``; the parsed contracts for the
requested underlyings go to ``ref/dhan_instruments/date=<IST date>/`` as Parquet with lineage, plus a manifest.
The same CSV also gives the security ids of the currently listed NIFTY futures for intraday candle jobs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pyarrow as pa

from project100c.data.dhan.config import DhanConfig
from project100c.data.http import HttpRequest, HttpTransport
from project100c.data.lake import Lake, Lineage, PartRef, RawRef, Zone
from project100c.errors import TransportError, VendorServerError
from project100c.instruments.master import InstrumentKind, InstrumentMaster
from project100c.instruments.parsers import ParseReport, parse_dhan_csv_bytes
from project100c.sessions.model import IST

DATASET_INSTRUMENTS = "dhan_instruments"
INSTRUMENTS_PARSER_VERSION = "dhan-instruments-2026-10-01.1"

INSTRUMENT_SCHEMA = pa.schema(
    [
        ("security_id", pa.string()),
        ("underlying", pa.string()),
        ("kind", pa.string()),
        ("right", pa.string()),
        ("expiry", pa.date32()),
        ("strike", pa.decimal128(12, 2)),
        ("lot_size", pa.int32()),
        ("tick_size", pa.decimal128(8, 4)),
        ("freeze_qty", pa.int32()),
        ("trading_symbol", pa.string()),
        ("weekly_flag", pa.bool_()),
    ]
)


@dataclass(frozen=True, slots=True)
class InstrumentSnapshot:
    raw: RawRef
    master: InstrumentMaster
    report: ParseReport
    part: PartRef
    manifest_path: str


def master_table(master: InstrumentMaster) -> pa.Table:
    cs = sorted(master.contracts, key=lambda c: (c.underlying, c.expiry, c.kind.value, c.strike or 0, c.right or ""))
    return pa.table(
        {
            "security_id": [c.exchange_token for c in cs],
            "underlying": [c.underlying for c in cs],
            "kind": [c.kind.value for c in cs],
            "right": [None if c.right is None else c.right.value for c in cs],
            "expiry": [c.expiry for c in cs],
            "strike": [c.strike for c in cs],
            "lot_size": [c.lot_size for c in cs],
            "tick_size": [c.tick_size for c in cs],
            "freeze_qty": [c.freeze_qty for c in cs],
            "trading_symbol": [c.trading_symbol for c in cs],
            "weekly_flag": [c.source_weekly_flag for c in cs],
        },
        schema=INSTRUMENT_SCHEMA,
    )


def download_instrument_list(
    *,
    transport: HttpTransport,
    config: DhanConfig,
    lake: Lake,
    wall_clock: Callable[[], datetime],
    underlyings: frozenset[str] = frozenset({"NIFTY"}),
) -> InstrumentSnapshot:
    now = wall_clock()
    req = HttpRequest("GET", config.instrument_list_url, {"Accept": "text/csv"})  # no credential header
    resp = transport.send(req, timeout_s=float(config.http_timeout_seconds))
    if resp.status != 200:
        raise VendorServerError(f"instrument list: HTTP {resp.status}", http_status=resp.status, code=None)
    if not resp.body:
        raise TransportError("instrument list: empty body")
    day = now.astimezone(IST).date()
    raw = lake.write_raw(
        "dhan",
        "instrument_list",
        day,
        "api-scrip-master-detailed",
        resp.body,
        sidecar={"fetched_at_utc": now.astimezone(UTC).isoformat(), "url": config.instrument_list_url},
    )
    master, report = parse_dhan_csv_bytes(
        lake.read_raw(raw.path), name="api-scrip-master-detailed.csv", as_of=now, underlyings=underlyings
    )
    lineage = Lineage(
        source="dhan",
        dataset=DATASET_INSTRUMENTS,
        data_version=f"{DATASET_INSTRUMENTS}@schema1+{INSTRUMENTS_PARSER_VERSION}",
        parser_version=INSTRUMENTS_PARSER_VERSION,
        config_version=config.version,
        dq_thresholds_version="n/a",
        dq_status="PASS",
        raw_path=raw.path,
        raw_sha256=raw.sha256,
        fetched_at_utc=raw.fetched_at_utc,
        request={"url": config.instrument_list_url, "underlyings": sorted(underlyings)},
    )
    part = lake.write_part(
        Zone.REF, DATASET_INSTRUMENTS, [("date", day.isoformat())], raw.sha256[:16], master_table(master), lineage
    )
    manifest = lake.write_manifest(
        DATASET_INSTRUMENTS,
        f"{day.isoformat()}-{raw.sha256[:16]}",
        {
            "dataset": DATASET_INSTRUMENTS,
            "raw": {"path": raw.path, "sha256": raw.sha256, "size": raw.size},
            "part": {"path": part.path, "sha256": part.sha256, "rows": part.rows},
            "rows_total": report.rows_total,
            "rows_used": report.rows_used,
            "skipped": report.skipped,
            "lineage": lineage.as_dict(),
        },
    )
    return InstrumentSnapshot(raw, master, report, part, manifest)


def futures_security_ids(master: InstrumentMaster, underlying: str = "NIFTY") -> list[tuple[str, str]]:
    """(security_id, expiry ISO) of the listed futures, nearest first: inputs for intraday futures jobs."""
    fut = [c for c in master.contracts if c.underlying == underlying and c.kind is InstrumentKind.FUTURE]
    return [(c.exchange_token, c.expiry.isoformat()) for c in sorted(fut, key=lambda c: c.expiry)]

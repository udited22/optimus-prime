"""Parsers for public, login-free instrument files. Every skipped row is counted with a reason (no silent drops).

* Upstox complete/exchange JSON (``NSE.json.gz``): the chosen live broker (OD-004). ``tick_size`` is in
  paise (5.0 == Rs 0.05), established by cross-checking against Kite's dump (see tests/fixtures/instruments).
* Kite public NFO CSV dump: used as an independent cross-check source.
* Dhan detailed scrip master CSV (``api-scrip-master-detailed.csv``, S58): the historical-data vendor (OD-011).
  ``TICK_SIZE`` for NSE derivatives is in paise like Upstox (5.0 == Rs 0.05, 10.0 == Rs 0.10 for futures),
  established by cross-checking the same exchange tokens against Kite (tests/fixtures/instruments).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from project100c.core_types import OptionRight
from project100c.errors import CrossSourceMismatchError, InstrumentMasterError
from project100c.instruments.master import Contract, InstrumentKind, InstrumentMaster
from project100c.sessions.model import IST

UPSTOX_TICK_UNIT_DIVISOR = Decimal(100)  # paise -> rupees
DHAN_TICK_UNIT_DIVISOR = Decimal(100)  # paise -> rupees (derivatives)


@dataclass(frozen=True, slots=True)
class ParseReport:
    source: str
    rows_total: int
    rows_used: int
    skipped: dict[str, int] = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as e:
        raise InstrumentMasterError(f"instrument file not found: {path}") from e


def _dec(v: Any, what: str, row: int) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise InstrumentMasterError(f"row {row}: {what} missing/invalid ({v!r})")
    try:
        d = Decimal(str(v))
    except InvalidOperation as e:
        raise InstrumentMasterError(f"row {row}: {what}={v!r} is not numeric") from e
    if not d.is_finite():
        raise InstrumentMasterError(f"row {row}: {what} not finite")
    return d


def _int(v: Any, what: str, row: int) -> int:
    d = _dec(v, what, row)
    if d != d.to_integral_value():
        raise InstrumentMasterError(f"row {row}: {what}={v!r} is not an integer")
    return int(d)


def _req(obj: dict[str, Any], key: str, row: int) -> Any:
    if key not in obj or obj[key] in (None, ""):
        raise InstrumentMasterError(f"row {row}: required field {key!r} missing")
    return obj[key]


def parse_upstox_json(
    path: Path,
    *,
    as_of: datetime,
    underlyings: frozenset[str] | None = None,
    allow_expired: bool = False,
) -> tuple[InstrumentMaster, ParseReport]:
    raw = _read_bytes(path)
    data = gzip.decompress(raw) if path.suffix == ".gz" else raw
    try:
        rows = json.loads(data)
    except json.JSONDecodeError as e:
        raise InstrumentMasterError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(rows, list):
        raise InstrumentMasterError(f"{path}: expected a JSON array")
    skipped: Counter[str] = Counter()
    contracts: list[Contract] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise InstrumentMasterError(f"row {i}: not an object")
        if r.get("segment") != "NSE_FO":
            skipped["segment_not_NSE_FO"] += 1
            continue
        itype = r.get("instrument_type")
        if itype not in ("CE", "PE", "FUT"):
            skipped[f"instrument_type_{itype}"] += 1
            continue
        und = str(_req(r, "underlying_symbol", i))
        if underlyings is not None and und not in underlyings:
            skipped["underlying_filtered"] += 1
            continue
        expiry_ms = _int(_req(r, "expiry", i), "expiry", i)
        expiry = datetime.fromtimestamp(expiry_ms / 1000, IST).date()
        kind = InstrumentKind.FUTURE if itype == "FUT" else InstrumentKind.OPTION
        strike = _dec(_req(r, "strike_price", i), "strike_price", i) if kind is InstrumentKind.OPTION else None
        fq = r.get("freeze_quantity")
        contracts.append(
            Contract(
                source="upstox",
                instrument_key=str(_req(r, "instrument_key", i)),
                exchange_token=str(_req(r, "exchange_token", i)),
                underlying=und,
                kind=kind,
                right=OptionRight(itype) if kind is InstrumentKind.OPTION else None,
                expiry=expiry,
                strike=strike,
                lot_size=_int(_req(r, "lot_size", i), "lot_size", i),
                tick_size=_dec(_req(r, "tick_size", i), "tick_size", i) / UPSTOX_TICK_UNIT_DIVISOR,
                freeze_qty=None if fq is None else _int(fq, "freeze_quantity", i),
                trading_symbol=str(_req(r, "trading_symbol", i)),
                source_weekly_flag=r.get("weekly") if isinstance(r.get("weekly"), bool) else None,
            )
        )
    master = InstrumentMaster(
        contracts, as_of=as_of, source=f"upstox:{path.name}", source_sha256=_sha256(raw), allow_expired=allow_expired
    )
    return master, ParseReport(str(path), len(rows), len(contracts), dict(skipped))


KITE_COLUMNS = (
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
)


def parse_kite_csv(
    path: Path,
    *,
    as_of: datetime,
    underlyings: frozenset[str] | None = None,
    allow_expired: bool = False,
) -> tuple[InstrumentMaster, ParseReport]:
    raw = _read_bytes(path)
    text = raw.decode("utf-8")
    reader = csv.DictReader(text.splitlines())
    if tuple(reader.fieldnames or ()) != KITE_COLUMNS:
        raise InstrumentMasterError(f"{path}: unexpected header {reader.fieldnames}")
    skipped: Counter[str] = Counter()
    contracts: list[Contract] = []
    n = 0
    for i, r in enumerate(reader):
        n += 1
        if r["exchange"] != "NFO":
            skipped["exchange_not_NFO"] += 1
            continue
        itype = r["instrument_type"]
        if itype not in ("CE", "PE", "FUT"):
            skipped[f"instrument_type_{itype}"] += 1
            continue
        und = r["name"]
        if underlyings is not None and und not in underlyings:
            skipped["underlying_filtered"] += 1
            continue
        try:
            expiry = date.fromisoformat(r["expiry"])
        except ValueError as e:
            raise InstrumentMasterError(f"row {i}: bad expiry {r['expiry']!r}") from e
        kind = InstrumentKind.FUTURE if itype == "FUT" else InstrumentKind.OPTION
        contracts.append(
            Contract(
                source="kite",
                instrument_key=f"NFO|{r['exchange_token']}",
                exchange_token=r["exchange_token"],
                underlying=und,
                kind=kind,
                right=OptionRight(itype) if kind is InstrumentKind.OPTION else None,
                expiry=expiry,
                strike=_dec(r["strike"], "strike", i) if kind is InstrumentKind.OPTION else None,
                lot_size=_int(r["lot_size"], "lot_size", i),
                tick_size=_dec(r["tick_size"], "tick_size", i),
                freeze_qty=None,
                trading_symbol=r["tradingsymbol"],
            )
        )
    master = InstrumentMaster(
        contracts, as_of=as_of, source=f"kite:{path.name}", source_sha256=_sha256(raw), allow_expired=allow_expired
    )
    return master, ParseReport(str(path), n, len(contracts), dict(skipped))


DHAN_REQUIRED_COLUMNS = (
    "EXCH_ID",
    "SEGMENT",
    "SECURITY_ID",
    "INSTRUMENT",
    "UNDERLYING_SECURITY_ID",
    "UNDERLYING_SYMBOL",
    "SYMBOL_NAME",
    "DISPLAY_NAME",
    "LOT_SIZE",
    "SM_EXPIRY_DATE",
    "STRIKE_PRICE",
    "OPTION_TYPE",
    "TICK_SIZE",
    "EXPIRY_FLAG",
    "SM_FREEZE_QTY",
)


def parse_dhan_csv_bytes(
    raw: bytes,
    *,
    name: str,
    as_of: datetime,
    underlyings: frozenset[str] | None = None,
    allow_expired: bool = False,
) -> tuple[InstrumentMaster, ParseReport]:
    """NSE index/stock F&O rows (``EXCH_ID=NSE``, ``SEGMENT=D``, ``INSTRUMENT`` in FUTIDX/OPTIDX/FUTSTK/OPTSTK)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise InstrumentMasterError(f"{name}: not UTF-8: {e}") from e
    reader = csv.DictReader(text.splitlines())
    header = [h for h in (reader.fieldnames or []) if h != ""]  # the file ends each line with a comma
    missing = [c for c in DHAN_REQUIRED_COLUMNS if c not in header]
    if missing:
        raise InstrumentMasterError(f"{name}: Dhan header missing columns {missing}")
    skipped: Counter[str] = Counter()
    contracts: list[Contract] = []
    n = 0
    for i, r in enumerate(reader):
        n += 1
        if r["EXCH_ID"] != "NSE" or r["SEGMENT"] != "D":
            skipped["not_NSE_derivative"] += 1
            continue
        inst = r["INSTRUMENT"]
        if inst not in ("FUTIDX", "OPTIDX", "FUTSTK", "OPTSTK"):
            skipped[f"instrument_{inst}"] += 1
            continue
        und = r["UNDERLYING_SYMBOL"]
        if underlyings is not None and und not in underlyings:
            skipped["underlying_filtered"] += 1
            continue
        try:
            expiry = date.fromisoformat(r["SM_EXPIRY_DATE"])
        except ValueError as e:
            raise InstrumentMasterError(f"row {i}: bad SM_EXPIRY_DATE {r['SM_EXPIRY_DATE']!r}") from e
        is_opt = inst.startswith("OPT")
        right: OptionRight | None = None
        if is_opt:
            if r["OPTION_TYPE"] not in ("CE", "PE"):
                raise InstrumentMasterError(f"row {i}: option with OPTION_TYPE={r['OPTION_TYPE']!r}")
            right = OptionRight(r["OPTION_TYPE"])
        fq = r.get("SM_FREEZE_QTY") or ""
        freeze = _int(fq, "SM_FREEZE_QTY", i) if fq not in ("", "0") else None
        flag = r["EXPIRY_FLAG"]
        contracts.append(
            Contract(
                source="dhan",
                instrument_key=f"DHAN|NSE_FNO|{r['SECURITY_ID']}",
                exchange_token=r["SECURITY_ID"],
                underlying=und,
                kind=InstrumentKind.OPTION if is_opt else InstrumentKind.FUTURE,
                right=right,
                expiry=expiry,
                strike=_dec(r["STRIKE_PRICE"], "STRIKE_PRICE", i) if is_opt else None,
                lot_size=_int(r["LOT_SIZE"], "LOT_SIZE", i),
                tick_size=_dec(r["TICK_SIZE"], "TICK_SIZE", i) / DHAN_TICK_UNIT_DIVISOR,
                freeze_qty=freeze,
                trading_symbol=r["SYMBOL_NAME"],
                source_weekly_flag={"W": True, "M": False}.get(flag) if is_opt else None,
            )
        )
    master = InstrumentMaster(
        contracts, as_of=as_of, source=f"dhan:{name}", source_sha256=_sha256(raw), allow_expired=allow_expired
    )
    return master, ParseReport(name, n, len(contracts), dict(skipped))


def parse_dhan_csv(
    path: Path,
    *,
    as_of: datetime,
    underlyings: frozenset[str] | None = None,
    allow_expired: bool = False,
) -> tuple[InstrumentMaster, ParseReport]:
    return parse_dhan_csv_bytes(
        _read_bytes(path), name=path.name, as_of=as_of, underlyings=underlyings, allow_expired=allow_expired
    )


@dataclass(frozen=True, slots=True)
class Mismatch:
    exchange_token: str
    field: str
    a: str
    b: str


def cross_check(a: InstrumentMaster, b: InstrumentMaster, underlying: str, *, strict: bool = True) -> list[Mismatch]:
    """Join two masters on exchange_token and compare contract attributes.

    Tokens present in only one source are reported as field 'presence'. With strict=True any attribute
    mismatch (not mere presence differences) raises CrossSourceMismatchError.
    """
    ia = {c.exchange_token: c for c in a.contracts if c.underlying == underlying}
    ib = {c.exchange_token: c for c in b.contracts if c.underlying == underlying}
    if not ia or not ib:
        raise InstrumentMasterError(f"cross_check: {underlying} missing in one source")
    out: list[Mismatch] = []
    for tok in sorted(set(ia) ^ set(ib)):
        out.append(Mismatch(tok, "presence", str(tok in ia), str(tok in ib)))
    attr_mismatch = False
    for tok in sorted(set(ia) & set(ib)):
        ca, cb = ia[tok], ib[tok]
        for fld in ("kind", "right", "expiry", "strike", "lot_size", "tick_size"):
            va, vb = getattr(ca, fld), getattr(cb, fld)
            if va != vb:
                out.append(Mismatch(tok, fld, str(va), str(vb)))
                attr_mismatch = True
    if strict and attr_mismatch:
        bad = [m for m in out if m.field != "presence"]
        raise CrossSourceMismatchError(f"{len(bad)} attribute mismatches, e.g. {bad[0]}")
    return out

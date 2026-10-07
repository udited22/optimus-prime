"""Parse Dhan chart responses (columnar JSON arrays) into typed columns. Structural problems raise
VendorResponseError; value-level problems are left for the ingest DQ checks (nothing is dropped here).

Shapes (S31, S57):
* ``/charts/rollingoption``: ``{"data": {"ce": {<field>: [...], "timestamp": [...]}, "pe": null}}``
* ``/charts/intraday`` and ``/charts/historical``: ``{"open": [...], ..., "timestamp": [...], "open_interest": [...]}``

Timestamps are epoch seconds and are taken to be the bar START (the S31 example 1756698300 is 09:15 IST).
Prices are kept as Decimal. Values with more decimals than the storage scale are rounded half-even and
COUNTED (``rounded`` in the result), so the DQ step can report them: nothing changes silently.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from project100c.core_types import OptionRight
from project100c.errors import VendorResponseError

PARSER_VERSION = "dhan-parse-2026-10-01.1"
PRICE_SCALE = 4
IV_SCALE = 6
_PRICE_Q = Decimal(1).scaleb(-PRICE_SCALE)
_IV_Q = Decimal(1).scaleb(-IV_SCALE)
_MIN_EPOCH = 946684800  # 2000-01-01
_MAX_EPOCH = 4102444800  # 2100-01-01

ROLLING_FIELDS = ("open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot")
CANDLE_FIELDS = ("open", "high", "low", "close", "volume")
_PRICE_FIELDS = frozenset({"open", "high", "low", "close", "strike", "spot"})
_INT_FIELDS = frozenset({"volume", "oi", "open_interest"})


@dataclass(slots=True)
class ParsedColumns:
    ts: list[datetime]
    cols: dict[str, list[Any]]
    rounded: dict[str, int] = field(default_factory=dict)
    no_data_reason: str | None = None

    @property
    def rows(self) -> int:
        return len(self.ts)


def _num(v: Any, where: str) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise VendorResponseError(f"{where}: boolean where a number was expected")
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise VendorResponseError(f"{where}: non-finite number")
        return v
    if isinstance(v, float):  # only if a caller parsed JSON without Decimal
        raise VendorResponseError(f"{where}: float value (parse JSON with Decimal)")
    raise VendorResponseError(f"{where}: expected a number, got {type(v).__name__}")


def _ts(v: Any, where: str) -> datetime:
    d = _num(v, where)
    if d is None or d != d.to_integral_value():
        raise VendorResponseError(f"{where}: timestamp must be integral epoch seconds, got {v!r}")
    n = int(d)
    if not _MIN_EPOCH <= n < _MAX_EPOCH:
        raise VendorResponseError(f"{where}: epoch {n} outside 2000..2100 (milliseconds?)")
    return datetime.fromtimestamp(n, UTC)


def _convert(name: str, values: Sequence[Any], rounded: dict[str, int]) -> list[Any]:
    out: list[Any] = []
    for i, v in enumerate(values):
        where = f"{name}[{i}]"
        d = _num(v, where)
        if d is None:
            out.append(None)
        elif name in _INT_FIELDS:
            if d != d.to_integral_value():
                raise VendorResponseError(f"{where}: {d} is not an integer")
            out.append(int(d))
        else:
            q = d.quantize(_IV_Q if name == "iv" else _PRICE_Q, rounding=ROUND_HALF_EVEN)
            if q != d:
                rounded[name] = rounded.get(name, 0) + 1
            out.append(q)
    return out


def _columns(obj: Mapping[str, Any], fields: Sequence[str], where: str) -> ParsedColumns:
    ts_raw = obj.get("timestamp")
    if ts_raw is None or not isinstance(ts_raw, list):
        raise VendorResponseError(f"{where}: 'timestamp' array missing")
    n = len(ts_raw)
    ts = [_ts(v, f"{where}.timestamp[{i}]") for i, v in enumerate(ts_raw)]
    rounded: dict[str, int] = {}
    cols: dict[str, list[Any]] = {}
    for f in fields:
        arr = obj.get(f)
        if not isinstance(arr, list):
            raise VendorResponseError(f"{where}: requested field {f!r} missing or not an array")
        if len(arr) != n:
            raise VendorResponseError(f"{where}: ragged arrays: {f!r} has {len(arr)} values, timestamp has {n}")
        cols[f] = _convert(f, arr, rounded)
    return ParsedColumns(ts, cols, rounded, None if n else "empty arrays")


def parse_rolling_option(parsed: Any, right: OptionRight, required: Sequence[str]) -> ParsedColumns:
    bad = [f for f in required if f not in ROLLING_FIELDS]
    if bad:
        raise VendorResponseError(f"unknown requiredData fields {bad}")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), dict):
        raise VendorResponseError("rollingoption: expected {'data': {...}}")
    side_key = "ce" if right is OptionRight.CE else "pe"
    side = parsed["data"].get(side_key)
    if side is None:
        return ParsedColumns([], {f: [] for f in required}, {}, f"data.{side_key} is null")
    if not isinstance(side, dict):
        raise VendorResponseError(f"rollingoption: data.{side_key} is not an object")
    return _columns(side, required, f"rollingoption.data.{side_key}")


def parse_candles(parsed: Any, *, with_oi: bool) -> ParsedColumns:
    if not isinstance(parsed, dict):
        raise VendorResponseError("candles: expected a JSON object")
    fields = (*CANDLE_FIELDS, "open_interest") if with_oi else CANDLE_FIELDS
    return _columns(parsed, fields, "candles")

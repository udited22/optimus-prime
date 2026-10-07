"""Parsers for the US no-key sources: Yahoo chart JSON, Cboe index-history CSV, the S&P 500 constituents CSV.

Prices become Decimal (never float in the lake). Yahoo serves single-precision values widened to double
(e.g. 501.9800109863281 for 501.98): a value that is exactly a float32 is decoded to the shortest decimal that
round-trips through float32 (501.98); any other value to the shortest double repr. Values with more than
``SCALE`` decimals are rounded half-even and counted in ``rounded`` (reported as VALUE_ROUNDED). The vendor
bytes stay unchanged in ``raw/``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import pyarrow as pa

from project100c.errors import VendorResponseError

PARSER_VERSION = "us-parse-2026-10-03.1"
SCALE = 10
PRICE = pa.decimal128(28, SCALE)
TS = pa.timestamp("ns", tz="UTC")
_Q = Decimal(1).scaleb(-SCALE)
_SYMBOL = re.compile(r"^\^?[A-Z0-9]{1,6}([.\-][A-Z0-9]{1,3})?$")


# ---------------------------------------------------------------------------------------------- symbols
def check_symbol(symbol: str) -> str:
    if not _SYMBOL.match(symbol):
        raise ValueError(f"bad US symbol {symbol!r}")
    return symbol


def vendor_symbol(symbol: str) -> str:
    """Canonical (exchange) symbol -> Yahoo symbol: share-class dot becomes a dash (BRK.B -> BRK-B)."""
    return check_symbol(symbol).replace(".", "-")


def lake_symbol(symbol: str) -> str:
    """Safe partition value: index symbols lose the caret (^GSPC -> INDEX-GSPC)."""
    s = check_symbol(symbol)
    return f"INDEX-{s[1:]}" if s.startswith("^") else s


def chart_url(base: str, symbol: str, *, interval: str, period1: int, period2: int) -> str:
    if interval not in ("1d", "1m"):
        raise ValueError(f"unsupported interval {interval!r}")
    q: dict[str, str] = {
        "interval": interval,
        "period1": str(period1),
        "period2": str(period2),
        "includePrePost": "false",
    }
    if interval == "1d":
        q |= {"events": "div,split", "includeAdjustedClose": "true"}
    return f"{base}/{quote(vendor_symbol(symbol), safe='')}?{urlencode(q)}"


# ---------------------------------------------------------------------------------------------- numbers
def f32_decimal(x: float) -> Decimal:
    f = struct.unpack("<f", struct.pack("<f", x))[0] if abs(x) < 3.4e38 else None
    if f is not None and f == x:
        for digits in range(1, 10):
            s = f"{x:.{digits}g}"
            if struct.unpack("<f", struct.pack("<f", float(s)))[0] == f:
                return Decimal(s)
    return Decimal(repr(x))


@dataclass(slots=True)
class _Dec:
    rounded: int = 0

    def __call__(self, v: Any) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise VendorResponseError(f"non-numeric price {v!r}")
        d = f32_decimal(float(v)) if isinstance(v, float) else Decimal(v)
        q = d.quantize(_Q, rounding=ROUND_HALF_EVEN)
        if q != d:
            self.rounded += 1
        return q


# ---------------------------------------------------------------------------------------------- Yahoo chart
@dataclass(slots=True)
class ParsedChart:
    bars: pa.Table
    actions: pa.Table
    meta: dict[str, Any]
    all_null_dates: list[date]  # session date of each row Yahoo emits with every value null (dropped, reported)
    partial_null_dates: list[date] = field(default_factory=list)  # rows with some values null: BLOCKING in DQ
    rounded: int = 0


BAR_FIELDS = ("open", "high", "low", "close")


def _session_dates(ts: list[int], tz: ZoneInfo) -> list[date]:
    return [datetime.fromtimestamp(t, UTC).astimezone(tz).date() for t in ts]


def parse_chart(body: bytes, *, symbol: str, interval: str) -> ParsedChart:
    try:
        doc = json.loads(body)
        chart = doc["chart"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise VendorResponseError(f"{symbol}: not a chart reply: {e}") from None
    if chart.get("error"):
        raise VendorResponseError(f"{symbol}: chart error {chart['error']}")
    res = (chart.get("result") or [None])[0]
    if not isinstance(res, dict):
        raise VendorResponseError(f"{symbol}: empty chart result")
    meta = dict(res.get("meta") or {})
    tzname = str(meta.get("exchangeTimezoneName") or "")
    try:
        tz = ZoneInfo(tzname)
    except (ValueError, KeyError) as e:
        raise VendorResponseError(f"{symbol}: bad exchange timezone {tzname!r}: {e}") from None
    ts: list[int] = list(res.get("timestamp") or [])
    quote_ = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    adj = (((res.get("indicators") or {}).get("adjclose") or [{}])[0] or {}).get("adjclose")
    cols = {k: list(quote_.get(k) or [None] * len(ts)) for k in (*BAR_FIELDS, "volume")}
    if adj is not None:
        cols["adj_close"] = list(adj)
    for k, v in cols.items():
        if len(v) != len(ts):
            raise VendorResponseError(f"{symbol}: column {k} has {len(v)} values for {len(ts)} timestamps")
    dec = _Dec()
    keep: list[int] = []
    partial: list[date] = []
    all_null: list[date] = []
    sdates = _session_dates(ts, tz)
    for i in range(len(ts)):
        vals = [cols[k][i] for k in (*BAR_FIELDS, "volume")]
        if all(v is None for v in vals):
            all_null.append(sdates[i])
            continue
        if any(v is None for v in vals):
            partial.append(sdates[i])
        keep.append(i)
    kts = [ts[i] for i in keep]
    data: dict[str, pa.Array] = {
        "ts": pa.array([t * 1_000_000_000 for t in kts], pa.int64()).cast(TS),
        "session_date": pa.array([sdates[i] for i in keep], pa.date32()),
        "symbol": pa.array([symbol] * len(kts), pa.string()),
    }
    for k in BAR_FIELDS:
        data[k] = pa.array([dec(cols[k][i]) for i in keep], PRICE)
    vol = [cols["volume"][i] for i in keep]
    if any(v is not None and (isinstance(v, bool) or not isinstance(v, int | float) or v != int(v)) for v in vol):
        raise VendorResponseError(f"{symbol}: non-integer volume")
    data["volume"] = pa.array([None if v is None else int(v) for v in vol], pa.int64())
    if interval == "1d":
        a = cols.get("adj_close")
        data["adj_close"] = pa.array([dec(a[i]) if a is not None else None for i in keep], PRICE)
    bars = pa.table(data)
    ev = res.get("events") or {}
    rows: list[dict[str, Any]] = []
    for kind, key in (("dividend", "dividends"), ("split", "splits")):
        for item in (ev.get(key) or {}).values():
            t = int(item["date"])
            rows.append(
                {
                    "ts": t * 1_000_000_000,
                    "session_date": _session_dates([t], tz)[0],
                    "symbol": symbol,
                    "kind": kind,
                    "amount": dec(item.get("amount")) if kind == "dividend" else None,
                    "numerator": int(item["numerator"]) if kind == "split" else None,
                    "denominator": int(item["denominator"]) if kind == "split" else None,
                }
            )
    rows.sort(key=lambda r: (r["ts"], r["kind"]))
    actions = pa.table(
        {
            "ts": pa.array([r["ts"] for r in rows], pa.int64()).cast(TS),
            "session_date": pa.array([r["session_date"] for r in rows], pa.date32()),
            "symbol": pa.array([r["symbol"] for r in rows], pa.string()),
            "kind": pa.array([r["kind"] for r in rows], pa.string()),
            "amount": pa.array([r["amount"] for r in rows], PRICE),
            "numerator": pa.array([r["numerator"] for r in rows], pa.int64()),
            "denominator": pa.array([r["denominator"] for r in rows], pa.int64()),
        }
    )
    return ParsedChart(bars, actions, meta, all_null, partial, dec.rounded)


# ---------------------------------------------------------------------------------------------- Cboe
def parse_cboe_csv(body: bytes, *, series: str) -> tuple[pa.Table, int]:
    """``DATE,OPEN,HIGH,LOW,CLOSE`` or ``DATE,<SERIES>`` (value-only series such as VVIX, SKEW), M/D/YYYY dates."""
    text = body.decode("utf-8-sig", "strict")
    rdr = csv.reader(io.StringIO(text))
    head = [h.strip().upper() for h in next(rdr, [])]
    if not head or head[0] != "DATE":
        raise VendorResponseError(f"{series}: unexpected header {head[:6]}")
    ohlc = head[1:] == ["OPEN", "HIGH", "LOW", "CLOSE"]
    if not ohlc and len(head) != 2:
        raise VendorResponseError(f"{series}: unexpected header {head[:6]}")
    dec = _Dec()
    dates: list[date] = []
    vals: dict[str, list[Decimal | None]] = {k: [] for k in ("open", "high", "low", "close")}
    for n, row in enumerate(rdr, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        try:
            dates.append(datetime.strptime(row[0].strip(), "%m/%d/%Y").date())
            nums = [Decimal(c.strip()) if c.strip() else None for c in row[1:]]
        except (ValueError, ArithmeticError) as e:
            raise VendorResponseError(f"{series}: line {n}: {e}") from None
        if len(nums) != len(head) - 1:
            raise VendorResponseError(f"{series}: line {n}: {len(nums)} values for header {head}")
        if ohlc:
            for k, v in zip(("open", "high", "low", "close"), nums, strict=True):
                vals[k].append(_q(v, dec))
        else:
            for k in ("open", "high", "low"):
                vals[k].append(None)
            vals["close"].append(_q(nums[0], dec))
    t = pa.table(
        {
            "session_date": pa.array(dates, pa.date32()),
            "series": pa.array([series] * len(dates), pa.string()),
            **{k: pa.array(v, PRICE) for k, v in vals.items()},
        }
    )
    return t, dec.rounded


def _q(v: Decimal | None, dec: _Dec) -> Decimal | None:
    if v is None:
        return None
    q = v.quantize(_Q, rounding=ROUND_HALF_EVEN)
    if q != v:
        dec.rounded += 1
    return q


# ---------------------------------------------------------------------------------------------- constituents
CONSTITUENT_COLUMNS = ("Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Date added", "CIK")


def parse_constituents(body: bytes) -> pa.Table:
    text = body.decode("utf-8-sig", "strict")
    rdr = csv.DictReader(io.StringIO(text))
    missing = [c for c in CONSTITUENT_COLUMNS if c not in (rdr.fieldnames or [])]
    if missing:
        raise VendorResponseError(f"constituents: missing columns {missing}")
    rows = list(rdr)
    syms = [r["Symbol"].strip().upper() for r in rows]
    for s in syms:
        check_symbol(s)
    if len(set(syms)) != len(syms):
        raise VendorResponseError("constituents: duplicate symbols")
    if not 400 <= len(syms) <= 600:
        raise VendorResponseError(f"constituents: implausible member count {len(syms)}")

    def _d(v: str) -> date | None:
        v = v.strip()
        try:
            return date.fromisoformat(v[:10]) if v else None
        except ValueError:
            return None

    return pa.table(
        {
            "symbol": pa.array(syms, pa.string()),
            "security": pa.array([r["Security"].strip() for r in rows], pa.string()),
            "gics_sector": pa.array([r["GICS Sector"].strip() for r in rows], pa.string()),
            "gics_sub_industry": pa.array([r["GICS Sub-Industry"].strip() for r in rows], pa.string()),
            "date_added": pa.array([_d(r["Date added"]) for r in rows], pa.date32()),
            "cik": pa.array([r["CIK"].strip().zfill(10) if r["CIK"].strip() else None for r in rows], pa.string()),
        }
    )

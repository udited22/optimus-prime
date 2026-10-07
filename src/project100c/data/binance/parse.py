"""Parse Binance archive zips (one CSV each) into typed Arrow tables. Prices are Decimal, never float.

Format facts (checked on real files, 3-Oct-2026):

* klines: 12 columns ``open_time, open, high, low, close, volume, close_time, quote_volume, count,
  taker_buy_volume, taker_buy_quote_volume, ignore``. Newer USD-M files carry that header row; older ones and
  spot files have none. Spot files from 2025-01-01 carry MICROSECOND timestamps (16 digits); older ones and all
  USD-M files carry milliseconds. The unit is detected per file from the digit count, never assumed.
* rate files: ``calc_time, funding_interval_hours, last_funding_rate`` (ms timestamps; header row).
* metrics: ``create_time`` (``YYYY-MM-DD HH:MM:SS`` UTC), symbol, open interest (contracts and USDT) and ratios.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

from project100c.errors import VendorResponseError

PARSER_VERSION = "binance-parse-2026-10-03.1"
PRICE = pa.decimal128(38, 8)
RATE = pa.decimal128(38, 12)
METRIC = pa.decimal128(38, 18)
TS = pa.timestamp("ns", tz="UTC")

KLINE_RAW = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)
RATE_RAW = ("calc_time", "funding_interval_hours", "last_funding_rate")
METRIC_RAW = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


def unzip_single(body: bytes, key: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if len(names) != 1:
                raise VendorResponseError(f"{key}: expected one CSV in the zip, found {names[:5]}")
            return zf.read(names[0])
    except zipfile.BadZipFile as e:
        raise VendorResponseError(f"{key}: bad zip: {e}") from None


def _read_csv(csv: bytes, names: tuple[str, ...], key: str) -> pa.Table:
    if not csv.strip():
        return pa.table({n: pa.array([], pa.string()) for n in names})
    first = csv.split(b"\n", 1)[0].strip().decode("utf-8", "replace")
    has_header = not first[:1].isdigit()
    if has_header:
        got = tuple(c.strip() for c in first.split(","))
        if got != names:
            raise VendorResponseError(f"{key}: unexpected header {got}")
    try:
        return pacsv.read_csv(
            io.BytesIO(csv),
            read_options=pacsv.ReadOptions(column_names=list(names), skip_rows=1 if has_header else 0),
            convert_options=pacsv.ConvertOptions(column_types={n: pa.string() for n in names}),
        )
    except (pa.ArrowInvalid, ValueError) as e:
        raise VendorResponseError(f"{key}: CSV parse failed: {e}") from None


def epoch_to_ts(col: pa.ChunkedArray, key: str) -> pa.Array:
    """Epoch integers (ms or us, detected from the digit count) -> timestamp[ns, UTC]."""
    ints = pc.cast(col, pa.int64())
    if len(ints) == 0:
        return pa.array([], TS)
    lengths = pc.utf8_length(col)
    lo, hi = pc.min_max(lengths).values()
    if lo.as_py() != hi.as_py() or lo.as_py() not in (13, 16):
        raise VendorResponseError(f"{key}: epoch timestamps with {lo}..{hi} digits (want all 13 or all 16)")
    unit = "ms" if lo.as_py() == 13 else "us"
    return pc.cast(pc.cast(ints, pa.timestamp(unit, tz="UTC")), TS).combine_chunks()


@dataclass(frozen=True, slots=True)
class Parsed:
    table: pa.Table
    raw_strings: pa.Table  # the original string columns (for conflicting-duplicate checks)
    timestamp_unit: str


def _dec(col: pa.ChunkedArray, typ: pa.DataType, key: str, name: str) -> pa.Array:
    try:
        return pc.cast(col, typ).combine_chunks()
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
        raise VendorResponseError(f"{key}: column {name} is not a {typ} decimal: {e}") from None


def parse_klines(body: bytes, key: str, *, symbol: str, market: str) -> Parsed:
    raw = _read_csv(unzip_single(body, key), KLINE_RAW, key)
    n = raw.num_rows
    ts = epoch_to_ts(raw["open_time"], key)
    unit = "none" if n == 0 else ("ms" if pc.utf8_length(raw["open_time"])[0].as_py() == 13 else "us")
    table = pa.table(
        {
            "ts": ts,
            "symbol": pa.array([symbol] * n, pa.string()),
            "market": pa.array([market] * n, pa.string()),
            "interval": pa.array(["1m"] * n, pa.string()),
            "open": _dec(raw["open"], PRICE, key, "open"),
            "high": _dec(raw["high"], PRICE, key, "high"),
            "low": _dec(raw["low"], PRICE, key, "low"),
            "close": _dec(raw["close"], PRICE, key, "close"),
            "volume": _dec(raw["volume"], PRICE, key, "volume"),
            "close_ts": epoch_to_ts(raw["close_time"], key),
            "quote_volume": _dec(raw["quote_volume"], PRICE, key, "quote_volume"),
            "count": pc.cast(raw["count"], pa.int64()).combine_chunks(),
            "taker_buy_volume": _dec(raw["taker_buy_volume"], PRICE, key, "taker_buy_volume"),
            "taker_buy_quote_volume": _dec(raw["taker_buy_quote_volume"], PRICE, key, "taker_buy_quote_volume"),
        }
    )
    return Parsed(table, raw, unit)


def parse_rate(body: bytes, key: str, *, symbol: str) -> Parsed:
    raw = _read_csv(unzip_single(body, key), RATE_RAW, key)
    n = raw.num_rows
    table = pa.table(
        {
            "ts": epoch_to_ts(raw["calc_time"], key),
            "symbol": pa.array([symbol] * n, pa.string()),
            "interval_hours": pc.cast(raw["funding_interval_hours"], pa.int16()).combine_chunks(),
            "rate": _dec(raw["last_funding_rate"], RATE, key, "last_funding_rate"),
        }
    )
    return Parsed(table, raw, "ms")


def parse_metrics(body: bytes, key: str, *, symbol: str) -> Parsed:
    raw = _read_csv(unzip_single(body, key), METRIC_RAW, key)
    n = raw.num_rows
    try:
        ts = pc.cast(pc.strptime(raw["create_time"], format="%Y-%m-%d %H:%M:%S", unit="s", error_is_null=False), TS)
    except pa.ArrowInvalid as e:
        raise VendorResponseError(f"{key}: bad create_time: {e}") from None
    cols: dict[str, pa.Array] = {
        "ts": pa.chunked_array(ts).combine_chunks().cast(pa.timestamp("ns", tz="UTC")),
        "symbol": pa.array([symbol] * n, pa.string()),
    }
    for name in METRIC_RAW[2:]:
        # empty cells are real (some early days lack the ratio columns): keep them null, never zero
        c = pc.if_else(pc.equal(raw[name], ""), pa.scalar(None, pa.string()), raw[name])
        cols[name] = _dec(c, METRIC, key, name)
    return Parsed(pa.table(cols), raw, "s")

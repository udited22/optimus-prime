"""Upstox Market Data Feed V3 messages: a protobuf wire-format decoder (and encoder, for fakes and replays) on the
standard library, so no protobuf dependency or generated code is needed.

Schema: ``https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto`` (package
``com.upstox.marketdatafeederv3udapi.rpc.proto``), fetched 3-Oct-2026. Only the fields we use are modelled;
unknown fields are skipped, as protobuf requires, so a schema addition cannot break decoding.

    FeedResponse { Type type = 1; map<string, Feed> feeds = 2; int64 currentTs = 3; MarketInfo marketInfo = 4; }
    Feed { oneof { LTPC ltpc = 1; FullFeed fullFeed = 2; FirstLevelWithGreeks firstLevelWithGreeks = 3; }
           RequestMode requestMode = 4; }
    FullFeed { oneof { MarketFullFeed marketFF = 1; IndexFullFeed indexFF = 2; } }
    MarketFullFeed { LTPC ltpc = 1; MarketLevel marketLevel = 2; OptionGreeks optionGreeks = 3;
                     MarketOHLC marketOHLC = 4; double atp = 5; int64 vtt = 6; double oi = 7; double iv = 8;
                     double tbq = 9; double tsq = 10; }
    IndexFullFeed { LTPC ltpc = 1; MarketOHLC marketOHLC = 2; }
    FirstLevelWithGreeks { LTPC ltpc = 1; Quote firstDepth = 2; OptionGreeks optionGreeks = 3; int64 vtt = 4;
                           double oi = 5; double iv = 6; }
    LTPC { double ltp = 1; int64 ltt = 2; int64 ltq = 3; double cp = 4; }
    MarketLevel { repeated Quote bidAskQuote = 1; }
    Quote { int64 bidQ = 1; double bidP = 2; int64 askQ = 3; double askP = 4; }
    MarketInfo { map<string, MarketStatus> segmentStatus = 1; }
    enum Type { initial_feed = 0; live_feed = 1; market_info = 2; }
    enum MarketStatus { PRE_OPEN_START = 0; PRE_OPEN_END = 1; NORMAL_OPEN = 2; NORMAL_CLOSE = 3;
                        CLOSING_START = 4; CLOSING_END = 5; }
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

from project100c.errors import DataSourceError

TYPE_NAMES = {0: "initial_feed", 1: "live_feed", 2: "market_info"}
MARKET_STATUS = {
    0: "PRE_OPEN_START",
    1: "PRE_OPEN_END",
    2: "NORMAL_OPEN",
    3: "NORMAL_CLOSE",
    4: "CLOSING_START",
    5: "CLOSING_END",
}


class FeedDecodeError(DataSourceError):
    """The bytes are not a valid FeedResponse."""


# -- wire format -------------------------------------------------------------------------------------------


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(b) or shift > 63:
            raise FeedDecodeError("truncated or oversized varint")
        c = b[i]
        i += 1
        result |= (c & 0x7F) << shift
        if not c & 0x80:
            return result, i
        shift += 7


def _signed64(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _fields(b: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """(field number, wire type, value): varint -> int, fixed64/32 -> raw bytes, length-delimited -> bytes."""
    i = 0
    while i < len(b):
        tag, i = _varint(b, i)
        fno, wt = tag >> 3, tag & 7
        if fno == 0:
            raise FeedDecodeError("field number 0")
        if wt == 0:
            v, i = _varint(b, i)
            yield fno, wt, v
        elif wt == 1:
            if i + 8 > len(b):
                raise FeedDecodeError("truncated fixed64")
            yield fno, wt, b[i : i + 8]
            i += 8
        elif wt == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise FeedDecodeError("truncated length-delimited field")
            yield fno, wt, b[i : i + n]
            i += n
        elif wt == 5:
            if i + 4 > len(b):
                raise FeedDecodeError("truncated fixed32")
            yield fno, wt, b[i : i + 4]
            i += 4
        else:
            raise FeedDecodeError(f"unsupported wire type {wt}")


def _dbl(v: int | bytes) -> float:
    if not isinstance(v, bytes) or len(v) != 8:
        raise FeedDecodeError("expected a double")
    return float(struct.unpack("<d", v)[0])


def _int(v: int | bytes) -> int:
    if not isinstance(v, int):
        raise FeedDecodeError("expected a varint")
    return _signed64(v)


def _msg(v: int | bytes) -> bytes:
    if not isinstance(v, bytes):
        raise FeedDecodeError("expected a message")
    return v


# -- decoded types ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ltpc:
    ltp: float = 0.0
    ltt: int = 0  # last trade time, epoch milliseconds
    ltq: int = 0
    cp: float = 0.0


@dataclass(frozen=True, slots=True)
class DepthLevel:
    bid_q: int = 0
    bid_p: float = 0.0
    ask_q: int = 0
    ask_p: float = 0.0


@dataclass(frozen=True, slots=True)
class InstrumentFeed:
    kind: str  # "ltpc" | "market" | "index" | "first_level"
    ltpc: Ltpc | None = None
    depth: tuple[DepthLevel, ...] = ()
    oi: float | None = None
    vtt: int | None = None
    request_mode: int = 0


@dataclass(frozen=True, slots=True)
class FeedResponse:
    type: str
    feeds: dict[str, InstrumentFeed] = field(default_factory=dict)
    current_ts: int = 0  # epoch milliseconds
    segment_status: dict[str, str] = field(default_factory=dict)


def _ltpc(b: bytes) -> Ltpc:
    ltp = cp = 0.0
    ltt = ltq = 0
    for f, _, v in _fields(b):
        if f == 1:
            ltp = _dbl(v)
        elif f == 2:
            ltt = _int(v)
        elif f == 3:
            ltq = _int(v)
        elif f == 4:
            cp = _dbl(v)
    return Ltpc(ltp, ltt, ltq, cp)


def _level(b: bytes) -> DepthLevel:
    bq = aq = 0
    bp = ap = 0.0
    for f, _, v in _fields(b):
        if f == 1:
            bq = _int(v)
        elif f == 2:
            bp = _dbl(v)
        elif f == 3:
            aq = _int(v)
        elif f == 4:
            ap = _dbl(v)
    return DepthLevel(bq, bp, aq, ap)


def _market_ff(b: bytes) -> InstrumentFeed:
    ltpc: Ltpc | None = None
    depth: list[DepthLevel] = []
    oi: float | None = None
    vtt: int | None = None
    for f, _, v in _fields(b):
        if f == 1:
            ltpc = _ltpc(_msg(v))
        elif f == 2:
            depth = [_level(_msg(q)) for g, _, q in _fields(_msg(v)) if g == 1]
        elif f == 6:
            vtt = _int(v)
        elif f == 7:
            oi = _dbl(v)
    return InstrumentFeed("market", ltpc, tuple(depth), oi, vtt)


def _first_level(b: bytes) -> InstrumentFeed:
    ltpc: Ltpc | None = None
    depth: tuple[DepthLevel, ...] = ()
    oi: float | None = None
    vtt: int | None = None
    for f, _, v in _fields(b):
        if f == 1:
            ltpc = _ltpc(_msg(v))
        elif f == 2:
            depth = (_level(_msg(v)),)
        elif f == 4:
            vtt = _int(v)
        elif f == 5:
            oi = _dbl(v)
    return InstrumentFeed("first_level", ltpc, depth, oi, vtt)


def _feed(b: bytes) -> InstrumentFeed:
    out = InstrumentFeed("ltpc")
    mode = 0
    for f, _, v in _fields(b):
        if f == 1:
            out = InstrumentFeed("ltpc", _ltpc(_msg(v)))
        elif f == 2:
            for g, _, w in _fields(_msg(v)):
                if g == 1:
                    out = _market_ff(_msg(w))
                elif g == 2:
                    lt = next((_ltpc(_msg(x)) for h, _, x in _fields(_msg(w)) if h == 1), None)
                    out = InstrumentFeed("index", lt)
        elif f == 3:
            out = _first_level(_msg(v))
        elif f == 4:
            mode = _int(v)
    return InstrumentFeed(out.kind, out.ltpc, out.depth, out.oi, out.vtt, mode)


def _map_entry(b: bytes) -> tuple[str, int | bytes | None]:
    key = ""
    val: int | bytes | None = None
    for f, _, v in _fields(b):
        if f == 1:
            key = _msg(v).decode("utf-8", errors="strict")
        elif f == 2:
            val = v
    return key, val


def decode_feed_response(b: bytes) -> FeedResponse:
    typ = 0
    feeds: dict[str, InstrumentFeed] = {}
    ts = 0
    seg: dict[str, str] = {}
    try:
        for f, _, v in _fields(b):
            if f == 1:
                typ = _int(v)
            elif f == 2:
                k, val = _map_entry(_msg(v))
                feeds[k] = _feed(val if isinstance(val, bytes) else b"")
            elif f == 3:
                ts = _int(v)
            elif f == 4:
                for g, _, w in _fields(_msg(v)):
                    if g == 1:
                        k, st = _map_entry(_msg(w))
                        seg[k] = MARKET_STATUS.get(st if isinstance(st, int) else 0, f"UNKNOWN_{st!r}")
    except UnicodeDecodeError as e:
        raise FeedDecodeError("instrument key is not UTF-8") from e
    return FeedResponse(TYPE_NAMES.get(typ, f"unknown_{typ}"), feeds, ts, seg)


# -- encoder (fakes, replays) ------------------------------------------------------------------------------------


def _ev(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        if n:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def _tag(f: int, wt: int) -> bytes:
    return _ev(f << 3 | wt)


def _ed(f: int, x: float) -> bytes:
    return _tag(f, 1) + struct.pack("<d", x)


def _ei(f: int, x: int) -> bytes:
    return _tag(f, 0) + _ev(x)


def _em(f: int, payload: bytes) -> bytes:
    return _tag(f, 2) + _ev(len(payload)) + payload


def _enc_ltpc(x: Ltpc) -> bytes:
    return _ed(1, x.ltp) + _ei(2, x.ltt) + _ei(3, x.ltq) + _ed(4, x.cp)


def _enc_level(d: DepthLevel) -> bytes:
    return _ei(1, d.bid_q) + _ed(2, d.bid_p) + _ei(3, d.ask_q) + _ed(4, d.ask_p)


def _enc_feed(x: InstrumentFeed) -> bytes:
    lt = _em(1, _enc_ltpc(x.ltpc)) if x.ltpc is not None else b""
    if x.kind == "ltpc":
        body = lt
    elif x.kind == "market":
        lvl = _em(2, b"".join(_em(1, _enc_level(d)) for d in x.depth))
        extra = (_ei(6, x.vtt) if x.vtt is not None else b"") + (_ed(7, x.oi) if x.oi is not None else b"")
        body = _em(2, _em(1, lt + lvl + extra))
    elif x.kind == "index":
        body = _em(2, _em(2, lt))
    elif x.kind == "first_level":
        fd = _em(2, _enc_level(x.depth[0])) if x.depth else b""
        body = _em(3, lt + fd + (_ed(5, x.oi) if x.oi is not None else b""))
    else:
        raise ValueError(f"unknown feed kind {x.kind}")
    return body + _ei(4, x.request_mode)


def encode_feed_response(r: FeedResponse) -> bytes:
    typ = {v: k for k, v in TYPE_NAMES.items()}[r.type]
    out = _ei(1, typ)
    for k, f in r.feeds.items():
        out += _em(2, _em(1, k.encode()) + _em(2, _enc_feed(f)))
    out += _ei(3, r.current_ts)
    if r.segment_status:
        inv = {v: k for k, v in MARKET_STATUS.items()}
        out += _em(4, b"".join(_em(1, _em(1, k.encode()) + _ei(2, inv[v])) for k, v in r.segment_status.items()))
    return out

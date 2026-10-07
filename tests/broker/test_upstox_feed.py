"""K-B1 feed: Market Data Feed V3 protobuf decoding, the WebSocket client, and the feed client's gap and
staleness handling against a local fake WebSocket server. No network beyond 127.0.0.1; prices are SIMULATED."""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from project100c.broker.upstox.credentials import UpstoxSession
from project100c.broker.upstox.feed import UpstoxFeed, to_quote
from project100c.broker.upstox.feed_proto import (
    DepthLevel,
    FeedDecodeError,
    FeedResponse,
    InstrumentFeed,
    Ltpc,
    decode_feed_response,
    encode_feed_response,
)
from project100c.broker.upstox.transport import HttpResponse
from project100c.netio.ws import WsClosed, WsError, ws_connect
from project100c.sessions import IST

from .fake_ws import FakeWsServer, wait_for

OPT = "NSE_FO|52567"
IDX = "NSE_INDEX|Nifty 50"
T0 = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _market(bid: float, ask: float, *, bq: int = 650, aq: int = 650, ltp: float = 101.0) -> InstrumentFeed:
    lv = (DepthLevel(bq, bid, aq, ask), DepthLevel(1300, bid - 0.05, 1300, ask + 0.05))
    return InstrumentFeed("market", Ltpc(ltp, _ms(T0), 65, 99.0), lv, oi=123450.0, vtt=1_000_000, request_mode=1)


def _resp(typ: str = "live_feed", **feeds: InstrumentFeed) -> bytes:
    return encode_feed_response(FeedResponse(typ, {k.replace("__", "|"): v for k, v in feeds.items()}, _ms(T0)))


# -- protobuf -------------------------------------------------------------------------------------------------


def test_round_trip_every_feed_kind() -> None:
    r = FeedResponse(
        "initial_feed",
        {
            OPT: _market(100.5, 101.0),
            IDX: InstrumentFeed("index", Ltpc(25012.35, _ms(T0), 0, 24980.0)),
            "NSE_FO|1": InstrumentFeed("first_level", Ltpc(5.0, 1, 2, 4.0), (DepthLevel(10, 4.95, 20, 5.05),), oi=7.0),
            "NSE_FO|2": InstrumentFeed("ltpc", Ltpc(-1.0, -5, 0, 0.0)),
        },
        _ms(T0),
        {"NSE_FO": "NORMAL_OPEN", "NSE_INDEX": "CLOSING_START"},
    )
    back = decode_feed_response(encode_feed_response(r))
    assert back == r


def test_unknown_fields_are_skipped() -> None:
    raw = _resp(x=_market(1.0, 1.1))
    extra = bytes([0x2A, 0x03]) + b"abc" + bytes([0x30, 0x05]) + bytes([0x3D]) + struct.pack("<f", 1.5)  # f5,f6,f7
    assert decode_feed_response(raw + extra) == decode_feed_response(raw)


@pytest.mark.parametrize("bad", [b"\x12\x05ab", b"\x08", b"\x11\x01\x02", b"\x00\x01", b"\x0b"])
def test_truncated_or_invalid_bytes_raise(bad: bytes) -> None:
    with pytest.raises(FeedDecodeError):
        decode_feed_response(bad)


def test_quote_conversion_is_conservative() -> None:
    now = T0 + timedelta(milliseconds=40)
    q = to_quote(OPT, _market(100.5, 101.0), _ms(T0), now)
    assert q is not None and q.bid == D("100.5") and q.ask == D("101.0") and q.bid_qty == 650 and q.oi == 123450
    assert q.exchange_ts == T0.astimezone(UTC) and q.receive_ts == now and q.is_option
    one_sided = to_quote(OPT, _market(0.0, 101.0, bq=0), _ms(T0), now)
    assert one_sided is not None and one_sided.bid is None and one_sided.bid_qty is None and one_sided.ask is not None
    no_size = to_quote(OPT, _market(100.0, 101.0, aq=0), _ms(T0), now)
    assert no_size is not None and no_size.ask is None
    crossed = to_quote(OPT, _market(102.0, 101.0), _ms(T0), now)
    assert crossed is not None and crossed.bid is None and crossed.ask is None and crossed.ltp == D("101.0")
    idx = to_quote(IDX, InstrumentFeed("index", Ltpc(25000.0, 0, 0, 0.0)), _ms(T0), now)
    assert idx is not None and not idx.is_option and idx.exchange_ts == T0.astimezone(UTC)
    assert to_quote(OPT, InstrumentFeed("ltpc", Ltpc(0.0, 0, 0, 0.0)), 0, now) is None


# -- the WebSocket client ---------------------------------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[FakeWsServer]:
    s = FakeWsServer()
    yield s
    s.close()


def test_ws_messages_fragments_ping_and_close(server: FakeWsServer) -> None:
    c = ws_connect(server.url, timeout_s=2)
    wait_for(lambda: len(server.conns) == 1)
    sc = server.conns[0]
    assert sc.path.endswith("code=one-time")
    c.send_binary(b'{"x":1}')
    wait_for(lambda: sc.received == [(0x2, b'{"x":1}')])
    assert c.recv(0.05) is None
    sc.frame(0x9, b"hb")  # ping, answered with a pong
    sc.frame(0x2, b"he", fin=False)
    sc.frame(0x0, b"llo")
    assert c.recv(1.0) == b"hello"
    wait_for(lambda: (0xA, b"hb") in sc.received)
    big = bytes(70000)
    sc.send(big)
    assert c.recv(1.0) == big
    sc.frame(0x8, struct.pack("!H", 1001))
    with pytest.raises(WsClosed, match="1001"):
        c.recv(1.0)


def test_ws_refuses_bad_handshakes() -> None:
    s = FakeWsServer(accept_override="wrong")
    try:
        with pytest.raises(WsError, match="handshake refused"):
            ws_connect(s.url, timeout_s=2)
    finally:
        s.close()
    with pytest.raises(WsError, match="localhost"):
        ws_connect("ws://feed.example.com/x")
    with pytest.raises(WsError):
        ws_connect("https://127.0.0.1/x")


def test_ws_peer_drop_is_closed(server: FakeWsServer) -> None:
    c = ws_connect(server.url, timeout_s=2)
    wait_for(lambda: len(server.conns) == 1)
    server.conns[0].drop()
    with pytest.raises(WsClosed):
        c.recv(1.0)
    assert c.closed


# -- the feed client ------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


class Authorize:
    def __init__(self, url: str) -> None:
        self.url = url
        self.calls = 0
        self.fail_next = False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_s: float,
    ) -> HttpResponse:
        assert method == "GET" and url.endswith("/v3/feed/market-data-feed/authorize")
        assert headers["Authorization"] == "Bearer tok"
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            return HttpResponse(401, {"status": "error"})
        return HttpResponse(200, {"status": "success", "data": {"authorized_redirect_uri": self.url}})


def _feed(server: FakeWsServer, clock: Clock) -> tuple[UpstoxFeed, Authorize]:
    auth = Authorize(server.url)
    f = UpstoxFeed(UpstoxSession("tok", T0.replace(hour=8)), clock, transport=auth, recv_timeout_s=0.2)
    f.subscribe([OPT, IDX])
    return f, auth


def _poll_until(f: UpstoxFeed, key: str, tries: int = 20) -> dict[str, Any]:
    for _ in range(tries):
        got = f.poll()
        if key in got:
            return dict(got)
    raise AssertionError(f"no quote for {key}")


def test_feed_subscribes_and_streams_quotes(server: FakeWsServer) -> None:
    clock = Clock()
    f, auth = _feed(server, clock)
    f.poll()  # connects
    wait_for(lambda: len(server.conns) == 1 and len(server.conns[0].received) == 1)
    op, sub = server.conns[0].received[0]
    msg = json.loads(sub)
    assert (
        op == 0x2 and msg["method"] == "sub" and msg["data"] == {"mode": "full", "instrumentKeys": sorted([OPT, IDX])}
    )
    server.conns[0].send(_resp("initial_feed", NSE_FO__52567=_market(100.5, 101.0)))
    got = _poll_until(f, OPT)
    assert got[OPT].ask == D("101.0") and f.healthy(clock.t) and auth.calls == 1
    clock.t += timedelta(seconds=6)
    assert f.stale_keys(clock.t) == {OPT, IDX} and not f.healthy(clock.t)
    server.conns[0].send(encode_feed_response(FeedResponse("market_info", {}, _ms(T0), {"NSE_FO": "NORMAL_OPEN"})))
    for _ in range(10):
        f.poll()
        if f.segment_status:
            break
    assert f.segment_status == {"NSE_FO": "NORMAL_OPEN"}
    assert [g.reason for g in f.gaps] == ["SILENT"] and f.gaps[0].seconds == 6.0
    f.subscribe([OPT, "NSE_FO|999"])  # only the new key is sent
    wait_for(lambda: len(server.conns[0].received) == 2)
    assert json.loads(server.conns[0].received[1][1])["data"]["instrumentKeys"] == ["NSE_FO|999"]
    f.close()


def test_feed_reconnects_with_backoff_and_records_the_gap(server: FakeWsServer) -> None:
    clock = Clock()
    f, auth = _feed(server, clock)
    f.poll()
    wait_for(lambda: len(server.conns) == 1)
    server.conns[0].send(_resp(NSE_FO__52567=_market(100.5, 101.0)))
    _poll_until(f, OPT)
    clock.t += timedelta(seconds=1)
    server.conns[0].drop()
    for _ in range(10):
        f.poll()
        if not f.connected:
            break
    assert not f.connected and f.gaps[-1].reason == "DISCONNECTED" and f.gaps[-1].end is None
    assert f.poll() == {} and auth.calls == 1  # backoff: no reconnect yet
    clock.t += timedelta(seconds=1)
    f.poll()  # reconnects, authorizing again (single-use URL) and resubscribing
    assert auth.calls == 2 and f.connected
    wait_for(lambda: len(server.conns) == 2 and len(server.conns[1].received) == 1)
    clock.t += timedelta(seconds=2)
    server.conns[1].send(_resp("initial_feed", NSE_FO__52567=_market(100.0, 100.5)))
    got = _poll_until(f, OPT)
    assert got[OPT].bid == D("100.0")
    g = f.gaps[-1]
    assert g.end is not None and g.seconds == 4.0 and f.connects == 2  # from the last message before the drop
    f.close()


def test_feed_connect_failure_backs_off(server: FakeWsServer) -> None:
    clock = Clock()
    f, auth = _feed(server, clock)
    auth.fail_next = True
    f.poll()
    assert not f.connected and f.gaps[-1].reason.startswith("CONNECT_FAILED")
    f.poll()
    assert auth.calls == 1
    clock.t += timedelta(seconds=1)
    f.poll()
    assert f.connected and auth.calls == 2
    f.close()


def test_feed_drops_after_repeated_garbage(server: FakeWsServer) -> None:
    clock = Clock()
    f, _ = _feed(server, clock)
    f.poll()
    wait_for(lambda: len(server.conns) == 1)
    for _ in range(5):
        server.conns[0].send(b"\x0b")
    for _ in range(20):
        f.poll()
        if not f.connected:
            break
    assert f.decode_errors == 5 and f.gaps[-1].reason == "DECODE_ERRORS"


def test_feed_refuses_an_expired_session(server: FakeWsServer) -> None:
    clock = Clock()
    f, auth = _feed(server, clock)
    clock.t = datetime(2026, 10, 6, 3, 31, tzinfo=IST)
    assert f.poll() == {} and auth.calls == 0 and f.gaps[-1].reason == "SESSION_EXPIRED"

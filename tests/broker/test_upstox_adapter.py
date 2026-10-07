"""K-04 Upstox adapter against the in-memory fake (no network). Everything here is SIMULATED."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from project100c.broker import OrderEventKind, OrderRequest, OrderStatus, OrderType
from project100c.broker.upstox import (
    HttpTransport,
    UpstoxAppCredentials,
    UpstoxBroker,
    UpstoxConfig,
    UpstoxSession,
    load_app_credentials,
    load_session,
    map_status,
    request_access_token,
    session_from_webhook,
)
from project100c.core_types import OrderSide
from project100c.errors import (
    BrokerDisconnectedError,
    BrokerError,
    BrokerRejectError,
    BrokerTimeoutError,
    MissingCredentialError,
)
from project100c.execution import ExecutionGateway
from project100c.execution.ids import broker_tag
from project100c.sessions import IST

from .upstox_fake import TOKEN, FakeUpstox

KEY = "NSE_FO|NIFTY06OCT2625000CE"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 5, 10, 0, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(clock: Clock) -> FakeUpstox:
    f = FakeUpstox(clock)
    f.set_quote(KEY, "2.95", "3.00")
    return f


def _session(clock: Clock, token: str = TOKEN) -> UpstoxSession:
    return UpstoxSession(token, clock.t.replace(hour=8))


@pytest.fixture
def ub(fake: FakeUpstox, clock: Clock) -> UpstoxBroker:
    return UpstoxBroker(_session(clock), clock=clock, transport=fake)


def buy(cid: str = "I1.ENTRY.1", price: str = "2.90", qty: int = 65) -> OrderRequest:
    return OrderRequest(cid, KEY, OrderSide.BUY, qty, OrderType.LIMIT, D(price), tag="ENTRY")


def sl(cid: str = "I1.PROT.1", qty: int = 65) -> OrderRequest:
    return OrderRequest(cid, KEY, OrderSide.SELL, qty, OrderType.SL, D("2.25"), D("2.30"), tag="PROTECTIVE")


def test_place_maps_body_and_returns_order_id(ub: UpstoxBroker, fake: FakeUpstox) -> None:
    oid = ub.place(buy())
    body = fake.bodies[-1]
    assert fake.calls[-1] == ("POST", "/v3/order/place")
    assert body["tag"] == broker_tag("I1.ENTRY.1") and len(body["tag"]) <= 40
    assert body["order_type"] == "LIMIT" and body["product"] == "I" and body["validity"] == "DAY"
    assert body["slice"] is False and body["is_amo"] is False and body["trigger_price"] == 0
    (o,) = ub.orders()
    assert o.broker_order_id == oid and o.request.client_order_id == "I1.ENTRY.1" and o.request.tag == "ENTRY"
    assert o.status is OrderStatus.OPEN and o.filled_qty == 0


def test_fill_trades_positions_and_funds(ub: UpstoxBroker, fake: FakeUpstox) -> None:
    ub.place(buy(price="3.00"))
    (o,) = ub.orders()
    assert o.status is OrderStatus.FILLED and o.avg_fill_price == D("3.0")
    (f,) = ub.trades()
    assert f.client_order_id == "I1.ENTRY.1" and f.qty == 65 and f.price == D("3.0") and f.side is OrderSide.BUY
    assert f.ts.tzinfo is not None
    (p,) = ub.positions()
    assert p.net_qty == 65 and p.buy_qty == 65 and p.buy_value == D("195.0")
    assert ub.funds() == D("10000.0")


def test_stop_limit_trigger_and_events(ub: UpstoxBroker, fake: FakeUpstox, clock: Clock) -> None:
    ub.place(buy(price="3.00"))
    oid = ub.place(sl())
    kinds = [e.kind for e in ub.poll_events()]
    assert OrderEventKind.FILL in kinds and OrderEventKind.ACK in kinds
    (s,) = [o for o in ub.orders() if o.broker_order_id == oid]
    assert s.status is OrderStatus.TRIGGER_PENDING and s.trigger_price == D("2.3")
    clock.t += timedelta(seconds=30)
    fake.set_quote(KEY, "2.25", "2.30")
    ev = ub.poll_events()
    assert [e.kind for e in ev] == [OrderEventKind.TRIGGERED, OrderEventKind.FILL]
    assert ev[1].fill is not None and ev[1].fill.price == D("2.25") and ev[1].client_order_id == "I1.PROT.1"
    assert ub.poll_events() == []  # each fill is reported once


def test_modify_and_cancel(ub: UpstoxBroker, fake: FakeUpstox) -> None:
    oid = ub.place(buy())
    ub.modify(oid, price=D("2.92"))
    assert fake.calls[-1] == ("PUT", "/v3/order/modify") and fake.bodies[-1]["quantity"] == 65
    (o,) = ub.orders()
    assert o.price == D("2.92") and o.modifications == 1
    ub.cancel(oid)
    assert ub.orders()[0].status is OrderStatus.CANCELLED
    with pytest.raises(BrokerRejectError):
        ub.cancel(oid)
    with pytest.raises(BrokerRejectError, match="UNKNOWN_ORDER"):
        ub.modify("999", price=D("1"))


def test_order_endpoints_use_the_hft_host(ub: UpstoxBroker, fake: FakeUpstox, clock: Clock) -> None:
    other = UpstoxBroker(
        _session(clock), clock=clock, transport=fake, config=UpstoxConfig(hft_base_url="https://api.upstox.com")
    )
    with pytest.raises(BrokerRejectError, match="UDAPI100060"):
        other.place(buy())


@pytest.mark.parametrize(
    ("fault", "exc", "connected"),
    [
        ("timeout", BrokerTimeoutError, True),
        ("5xx", BrokerTimeoutError, True),
        ("disconnect", BrokerDisconnectedError, False),
        ("static_ip", BrokerDisconnectedError, False),
        ("reject", BrokerRejectError, True),
    ],
)
def test_error_taxonomy(ub: UpstoxBroker, fake: FakeUpstox, fault: str, exc: type[Exception], connected: bool) -> None:
    fake.faults.place.append(fault)
    with pytest.raises(exc):
        ub.place(buy())
    assert ub.is_connected() is connected
    if fault == "5xx":
        with pytest.raises(BrokerTimeoutError, match="fate unknown"):
            fake.faults.place.append("5xx")
            ub.place(buy("I1.ENTRY.2"))


def test_bad_token_is_a_disconnect(fake: FakeUpstox, clock: Clock) -> None:
    b = UpstoxBroker(_session(clock, "wrong-token"), clock=clock, transport=fake)
    with pytest.raises(BrokerDisconnectedError, match="UDAPI100050"):
        b.orders()
    assert not b.is_connected()


def test_expired_session_refuses_before_any_io(ub: UpstoxBroker, fake: FakeUpstox, clock: Clock) -> None:
    clock.t = datetime(2026, 10, 6, 3, 30, tzinfo=IST)
    n = len(fake.calls)
    with pytest.raises(BrokerDisconnectedError, match="SESSION_EXPIRED"):
        ub.place(buy())
    assert len(fake.calls) == n and not ub.is_connected()


def test_order_window_refuses_before_any_io(fake: FakeUpstox, clock: Clock) -> None:
    b = UpstoxBroker(_session(clock), clock=clock, transport=fake, order_window=lambda t: t.hour < 15)
    clock.t = clock.t.replace(hour=15, minute=1)
    with pytest.raises(BrokerRejectError, match="OUTSIDE_ORDER_WINDOW"):
        b.place(buy())
    assert fake.calls == []


def test_lost_response_is_resolved_by_the_gateway_from_the_order_book(
    ub: UpstoxBroker, fake: FakeUpstox, clock: Clock
) -> None:
    gw = ExecutionGateway(ub, clock)
    fake.faults.place.append("timeout_after_accept")
    with pytest.raises(BrokerTimeoutError):
        gw.place(buy())
    oid = gw.place(buy())  # same client order id: resolved from the book, never sent twice
    assert sum(1 for c in fake.calls if c == ("POST", "/v3/order/place")) == 1
    assert ub.orders()[0].broker_order_id == oid


def test_restart_rebuilds_the_tag_map(ub: UpstoxBroker, fake: FakeUpstox, clock: Clock) -> None:
    ub.place(buy())
    fresh = UpstoxBroker(_session(clock), clock=clock, transport=fake)
    assert fresh.orders()[0].request.client_order_id.startswith("FOREIGN-")
    fresh.remember([buy()])
    (o,) = fresh.orders()
    assert o.request.client_order_id == "I1.ENTRY.1" and o.request.tag == "ENTRY"


def test_exit_all_cancels_open_orders_then_exits(ub: UpstoxBroker, fake: FakeUpstox) -> None:
    ub.place(buy(price="3.00"))
    prot = ub.place(sl())
    res = ub.exit_all()
    assert res.cancelled_order_ids == (prot,) and len(res.exit_order_ids) == 1 and res.failed_instruments == ()
    assert res.pricing_verified is False
    assert [p.net_qty for p in ub.positions()] == [0]
    exits = [o for o in ub.orders() if o.broker_order_id in res.exit_order_ids]
    assert exits[0].request.tag == "EXIT_ALL" and exits[0].request.client_order_id.startswith("EXIT-ALL-")
    assert exits[0].status is OrderStatus.FILLED
    again = ub.exit_all()  # UDAPI1111: nothing to exit is not a failure
    assert again.exit_order_ids == () and again.failed_instruments == ()


def test_exit_all_partial_reports_failed_instruments(ub: UpstoxBroker, fake: FakeUpstox) -> None:
    ub.place(buy(price="3.00"))
    fake.faults.exit_all_skip.add(KEY)
    res = ub.exit_all()
    assert res.failed_instruments == (KEY,) and res.exit_order_ids == ()


@pytest.mark.parametrize(
    ("raw", "filled", "xid", "want"),
    [
        ("complete", 65, "X", OrderStatus.FILLED),
        ("rejected", 0, "", OrderStatus.REJECTED),
        ("cancelled", 10, "X", OrderStatus.CANCELLED),
        ("trigger pending", 0, "X", OrderStatus.TRIGGER_PENDING),
        ("open", 0, "X", OrderStatus.OPEN),
        ("open", 10, "X", OrderStatus.PARTIALLY_FILLED),
        ("modify pending", 0, "X", OrderStatus.OPEN),
        ("put order req received", 0, "", OrderStatus.PENDING_ACK),
        ("validation pending", 0, "", OrderStatus.PENDING_ACK),
        ("something new", 0, "X", OrderStatus.OPEN),
    ],
)
def test_status_mapping_never_invents_a_terminal_state(raw: str, filled: int, xid: str, want: OrderStatus) -> None:
    assert map_status(raw, filled, xid) is want


def test_config_refuses_delivery_product() -> None:
    with pytest.raises(BrokerError):
        UpstoxConfig(product="D")


# -- credentials and the daily token -------------------------------------------------------------------------


def test_credentials_come_from_env_and_never_print() -> None:
    env = {"UPSTOX_API_KEY": "k" * 36, "UPSTOX_API_SECRET": "s" * 10, "UPSTOX_REDIRECT_URI": "https://x.invalid/cb"}
    c = load_app_credentials(env)
    assert "kkkk" not in repr(c) and "ssss" not in repr(c)
    with pytest.raises(MissingCredentialError):
        load_app_credentials({})
    with pytest.raises(MissingCredentialError):
        load_session(datetime.now(IST), {})
    s = load_session(datetime(2026, 10, 5, 8, tzinfo=IST), {"UPSTOX_ACCESS_TOKEN": "abc"})
    assert "abc" not in repr(s)


@pytest.mark.parametrize(
    ("issued", "expiry"),
    [((8, 0), (6, 3, 30)), ((2, 0), (5, 3, 30)), ((3, 30), (6, 3, 30))],
)
def test_session_expires_at_0330_ist(issued: tuple[int, int], expiry: tuple[int, int, int]) -> None:
    s = UpstoxSession("t", datetime(2026, 10, 5, *issued, tzinfo=IST))
    assert s.expires_at == datetime(2026, 10, expiry[0], expiry[1], expiry[2], tzinfo=IST)


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def _payload(**kw: Any) -> dict[str, Any]:
    now = datetime(2026, 10, 5, 8, 30, tzinfo=IST)
    base = {
        "client_id": "app-1",
        "user_id": "U1",
        "access_token": "tok",
        "token_type": "Bearer",
        "issued_at": _ms(now),
        "expires_at": _ms(datetime(2026, 10, 6, 3, 30, tzinfo=IST)),
        "message_type": "access_token",
    }
    return {**base, **kw}


def test_webhook_payload_becomes_a_session() -> None:
    now = datetime(2026, 10, 5, 8, 31, tzinfo=IST)
    s = session_from_webhook(_payload(), expected_client_id="app-1", now=now)
    assert s.valid_at(now) and s.expires_at == datetime(2026, 10, 6, 3, 30, tzinfo=IST)


@pytest.mark.parametrize(
    "bad",
    [
        {"client_id": "other-app"},
        {"message_type": "order_update"},
        {"token_type": "mac"},
        {"access_token": ""},
        {"expires_at": _ms(datetime(2026, 10, 5, 8, 0, tzinfo=IST))},
        {"expires_at": _ms(datetime(2026, 10, 7, 3, 30, tzinfo=IST))},
        {"issued_at": "yesterday"},
    ],
)
def test_webhook_fails_closed(bad: dict[str, Any]) -> None:
    with pytest.raises(BrokerError):
        session_from_webhook(_payload(**bad), expected_client_id="app-1", now=datetime(2026, 10, 5, 8, 31, tzinfo=IST))


def test_token_request(fake: FakeUpstox) -> None:
    ack = request_access_token(UpstoxAppCredentials("app-1", "sec", "https://x.invalid/cb"), fake)
    assert fake.calls[-1] == ("POST", "/v3/login/auth/token/request/app-1")
    assert fake.bodies[-1] == {"client_secret": "sec"}
    assert ack.authorization_expiry.tzinfo is UTC


# -- the real HTTP transport, against a local server ---------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/slow"):
            import time

            # Far beyond the client's 0.1 s timeout, so a loaded machine cannot let the reply arrive before the
            # client polls (it flaked once at 0.5 s with another test run using the CPU).
            time.sleep(2.0)
        if self.path.startswith("/html"):
            self._reply(502, b"<html>bad gateway</html>")
            return
        self._reply(
            200,
            json.dumps({"status": "success", "path": self.path, "auth": self.headers.get("Authorization")}).encode(),
        )

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n))
        self._reply(400, json.dumps({"status": "error", "echo": body}).encode())


@pytest.fixture
def server() -> Iterator[str]:
    s = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()
    s.server_close()


def test_http_transport_round_trip(server: str) -> None:
    t = HttpTransport()
    r = t.request("GET", f"{server}/v2/x", headers={"Authorization": "Bearer z"}, params={"a": "1"}, timeout_s=2)
    assert r.status == 200 and r.body["path"] == "/v2/x?a=1" and r.body["auth"] == "Bearer z"
    r = t.request("POST", f"{server}/p", headers={}, json_body={"q": 1}, timeout_s=2)
    assert r.status == 400 and r.body["echo"] == {"q": 1}
    r = t.request("GET", f"{server}/html", headers={}, timeout_s=2)
    assert r.status == 502 and "_raw" in r.body
    with pytest.raises(BrokerTimeoutError):
        t.request("GET", f"{server}/slow", headers={}, timeout_s=0.1)


def test_http_transport_refused_and_plain_http() -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(BrokerDisconnectedError):
        HttpTransport().request("GET", f"http://127.0.0.1:{port}/x", headers={}, timeout_s=1)
    with pytest.raises(BrokerDisconnectedError, match="non-HTTPS"):
        HttpTransport().request("GET", "http://api.upstox.com/v2/x", headers={}, timeout_s=1)

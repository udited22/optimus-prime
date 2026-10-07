"""The notifier-webhook receiver for the daily Upstox token (docs/engineering/alerts-and-daily-token.md). Local only;
the token is fake."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

import pytest

from project100c.broker.upstox.credentials import UpstoxSession
from project100c.broker.upstox.webhook import NotifierReceiver, serve
from project100c.errors import BrokerError
from project100c.kernel.runtime import MemoryAlerts
from project100c.ops import GateState, TokenGate
from project100c.sessions import IST

PATH = "/upstox/notify/9f2c41d07be35a68"
FAKE = "fake-access-value-123"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 5, 8, 50, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.t


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def _payload(**kw: Any) -> bytes:
    base = {
        "client_id": "app-1",
        "user_id": "U1",
        "access_token": FAKE,
        "token_type": "Bearer",
        "issued_at": _ms(datetime(2026, 10, 5, 8, 49, tzinfo=IST)),
        "expires_at": _ms(datetime(2026, 10, 6, 3, 30, tzinfo=IST)),
        "message_type": "access_token",
    }
    return json.dumps({**base, **kw}).encode()


def _rx(clock: Clock) -> tuple[NotifierReceiver, TokenGate, list[UpstoxSession], MemoryAlerts]:
    alerts = MemoryAlerts()
    gate = TokenGate(lambda: clock.t + timedelta(hours=18), alerts, broker_name="Upstox", ref_factory=lambda: "AB12CD")
    gate.start(clock.t)
    got: list[UpstoxSession] = []
    return NotifierReceiver("app-1", PATH, gate, got.append, clock), gate, got, alerts


def _st(g: TokenGate) -> GateState:
    return g.state


def test_valid_callback_activates_the_gate_and_never_echoes_the_value() -> None:
    clock = Clock()
    rx, gate, got, alerts = _rx(clock)
    code, body = rx.handle("POST", PATH, "application/json", _payload())
    assert code == 200 and body == {"status": "accepted"} and FAKE not in json.dumps(body)
    assert _st(gate) is GateState.ACTIVE and gate.trading_allowed(clock.t)
    assert len(got) == 1 and got[0].access_token == FAKE
    assert all(FAKE not in m for _, m in alerts.sent)


@pytest.mark.parametrize(
    ("method", "path", "ctype", "body", "code"),
    [
        ("POST", "/upstox/notify/wrong-path-000000", "application/json", _payload(), 404),
        ("GET", PATH, "application/json", b"", 405),
        ("POST", PATH, "text/plain", _payload(), 400),
        ("POST", PATH, "application/json", b"{" + b" " * 5000 + b"}", 400),
        ("POST", PATH, "application/json", b"not json", 400),
        ("POST", PATH, "application/json", b"[1, 2]", 400),
        ("POST", PATH, "application/json", _payload(client_id="other-app"), 400),
        ("POST", PATH, "application/json", _payload(message_type="order_update"), 400),
    ],
)
def test_everything_else_is_refused_and_changes_nothing(
    method: str, path: str, ctype: str, body: bytes, code: int
) -> None:
    clock = Clock()
    rx, gate, got, _ = _rx(clock)
    assert rx.handle(method, path, ctype, body)[0] == code
    assert _st(gate) is GateState.REQUESTED and got == []


def test_a_denied_day_refuses_a_late_token() -> None:
    clock = Clock()
    rx, gate, got, _ = _rx(clock)
    gate.deny("AB12CD", clock.t)
    assert rx.handle("POST", PATH, "application/json", _payload())[0] == 409 and got == []


def test_rate_limit_counts_only_the_real_path() -> None:
    clock = Clock()
    rx, _, _, _ = _rx(clock)
    rx.max_requests_per_min = 3
    for _ in range(10):
        rx.handle("POST", "/scan", "application/json", b"{}")
    codes = [rx.handle("POST", PATH, "application/json", b"x")[0] for _ in range(4)]
    assert codes == [400, 400, 400, 429]
    clock.t += timedelta(minutes=1)
    assert rx.handle("POST", PATH, "application/json", _payload())[0] == 200


def test_path_must_be_unguessable() -> None:
    clock = Clock()
    _, gate, _, _ = _rx(clock)
    with pytest.raises(BrokerError):
        NotifierReceiver("app-1", "/hook", gate, lambda s: None, clock)


def test_http_server_round_trip_on_loopback() -> None:
    clock = Clock()
    rx, gate, got, _ = _rx(clock)
    srv = serve(rx, "127.0.0.1", 0)
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{PATH}",
            data=_payload(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            assert r.status == 200 and json.loads(r.read()) == {"status": "accepted"}
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=3)
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
    assert _st(gate) is GateState.ACTIVE and len(got) == 1
    with pytest.raises(BrokerError, match="loopback"):
        serve(rx, "0.0.0.0", 0)

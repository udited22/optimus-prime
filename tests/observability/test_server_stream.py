"""The event-stream backend over a real (loopback) HTTP server."""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.observability.dashboard import DashEvent, EventBus, EventKind
from project100c.observability.dashboard.events import DashboardError
from project100c.observability.dashboard.server import make_server
from project100c.sessions import IST

from .helpers import serve

T = datetime(2026, 10, 5, 9, 30, tzinfo=IST)


def _ev(i: int) -> DashEvent:
    return DashEvent(T, EventKind.TICK, {"spot": Decimal(24800 + i)})


def _parse_sse(raw: str) -> list[dict[str, object]]:
    out = []
    for block in raw.split("\n\n")[:-1]:  # the text after the last blank line is an incomplete block
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ln and not ln.startswith(":") and ": " in ln)
        if lines.get("event") == "dash":
            d = json.loads(lines["data"])
            assert str(d["seq"]) == lines["id"]
            out.append(d)
    return out


def test_health_topology_config_and_snapshot() -> None:
    bus = EventBus()
    bus.publish([_ev(1), DashEvent(T, EventKind.LOG, {"level": "INFO", "text": "hello"})])
    with serve(bus) as s:
        st, h = s.json("GET", "/api/health")
        assert st == 200 and h["label"] == "SIMULATED" and h["last_seq"] == 2
        st, topo = s.json("GET", "/api/topology")
        assert st == 200 and any(n["id"] == "risk_governor" for n in topo["nodes"])
        st, cfg = s.json("GET", "/api/config")
        assert cfg["label"] == "SIMULATED" and cfg["window"]["entry_start"] == "09:20:00"
        st, snap = s.json("GET", "/api/snapshot")
        assert snap["last_seq"] == 2 and snap["latest"]["TICK"]["data"]["spot"] == "24801"
        assert [e["data"]["text"] for e in snap["log"]] == ["hello"]
        assert snap["session"] == []  # no SESSION event published yet: nothing to replay
        st, rp = s.json("GET", "/api/replay")
        assert rp["days"][0]["name"] == "d1"
        assert s.json("GET", "/api/replay/d1")[1]["events"][0]["simulated"] is True
        assert s.json("GET", "/api/replay/nope")[0] == 400


def test_stream_catch_up_from_after_and_last_event_id() -> None:
    bus = EventBus()
    bus.publish([_ev(i) for i in range(5)])
    with serve(bus) as s:
        st, hdr, raw = s.request("GET", "/api/stream?after=2&once=1")
        assert st == 200 and hdr["content-type"].startswith("text/event-stream")
        evs = _parse_sse(raw.decode())
        assert [e["seq"] for e in evs] == [3, 4, 5] and all(e["simulated"] for e in evs)
        _, _, raw = s.request("GET", "/api/stream?once=1", headers={"Last-Event-ID": "4"})
        assert [e["seq"] for e in _parse_sse(raw.decode())] == [5]
        assert s.request("GET", "/api/stream?after=x&once=1")[0] == 400


def test_stream_pushes_new_events_live_with_heartbeats() -> None:
    bus = EventBus()
    with serve(bus, heartbeat=0.1) as s:
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
        c.request("GET", "/api/stream?after=0")
        r = c.getresponse()
        assert r.status == 200
        threading.Timer(0.35, lambda: bus.publish([_ev(1), _ev(2)])).start()
        buf = ""
        deadline = time.monotonic() + 8
        while len(_parse_sse(buf)) < 2 and time.monotonic() < deadline:
            buf += r.fp.readline().decode()
        assert [e["seq"] for e in _parse_sse(buf)] == [1, 2]
        assert ": heartbeat" in buf  # idle keep-alives before the events arrived
        c.close()


def test_frontend_missing_shows_build_hint_and_static_is_confined(tmp_path: Path) -> None:
    with serve() as s:
        st, _, body = s.request("GET", "/")
        assert st == 200 and b"npm run build" in body
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<p>SIMULATED</p>")
    (tmp_path / "assets" / "a.js").write_text("console.log(1)")
    (tmp_path.parent / "secret.txt").write_text("x")
    with serve(static_dir=tmp_path) as s:
        assert s.request("GET", "/")[2] == b"<p>SIMULATED</p>"
        st, hdr, _ = s.request("GET", "/assets/a.js")
        assert st == 200 and "javascript" in hdr["content-type"]
        assert s.request("GET", "/assets/../../secret.txt")[0] == 404
        assert s.request("GET", "/assets/%2e%2e/%2e%2e/secret.txt")[0] == 404


def test_server_refuses_non_loopback_bind() -> None:
    from project100c.observability.dashboard.server import DashboardApp, KillGate

    from .helpers import StubKill, StubReplays

    app = DashboardApp(EventBus(), KillGate(StubKill()), StubReplays(), {}, Path("."))
    with pytest.raises(DashboardError, match="localhost"):
        make_server(app, host="0.0.0.0", port=0)

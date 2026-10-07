"""Jarvis on the host's stream (observability/dashboard/hostfeed.py): a replay day's private status, translated."""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from project100c.observability.dashboard.events import DashboardError, DashEvent, EventBus, EventKind
from project100c.observability.dashboard.hostfeed import (
    HOST_LABEL,
    PROXY_HEADER,
    HostFeedRunner,
    HostTranslator,
    http_fetcher,
)
from project100c.observability.dashboard.server import DashboardApp, KillGate, make_server
from project100c.observability.dashboard.simulator import Kernel
from project100c.ops import host_main as hm
from project100c.ops.status_server import StatusBoard, serve_status

ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "configs" / "host" / "host.toml"
SECRET = "s" * 12 + "-test-only-" + "t" * 12  # built here, never a real value


def _replay_statuses(tmp: Path, every: int = 40) -> list[dict[str, Any]]:
    base = hm.load_host_config(CFG, state_dir=tmp / "state")
    c = dataclasses.replace(
        base, status_port=0, webhook_port=0, replay=dataclasses.replace(base.replay, rehearse_orders=False)
    )
    h = hm.Host(c, root=ROOT, env={}, forbid_under=tmp / "never", serve=False)
    out: list[dict[str, Any]] = []
    try:
        for _ in range(2000 // every):
            h.run(max_steps=every)
            out.append(h.board.private())
    finally:
        h.close()
    return out


def test_a_replay_day_becomes_the_dashboards_event_kinds(tmp_path: Path, kernel: Kernel) -> None:
    statuses = _replay_statuses(tmp_path)
    tr = HostTranslator(kernel)
    evs: list[DashEvent] = [e for s in statuses for e in tr.all_events(s)]
    kinds = [e.kind for e in evs]
    assert kinds[0] is EventKind.SESSION and kinds.count(EventKind.SESSION) == 1
    s0 = evs[0].data
    assert s0["scenario"] == "host-20261005" and "real money OFF" in s0["notice"] and "SHADOW" in s0["notice"]
    ticks = [e for e in evs if e.kind is EventKind.TICK]
    assert len(ticks) > 30 and all(e.simulated for e in ticks)  # replay: synthetic, so never marked real
    assert "SIMULATED" in str(ticks[-1].data["source"])
    assert all(e.simulated for e in evs)  # nothing from a replay day is real
    (strat,) = [e for e in evs if e.kind is EventKind.STRATEGIES]
    rows = strat.data["rows"]
    assert len(rows) >= 10 and {r["stage"] for r in rows} == {"RESEARCH"} and strat.data["shadow_only"]
    phases = [e.data["phase"] for e in evs if e.kind is EventKind.WINDOW]
    assert "ENTRY_ALLOWED" in phases and len(phases) == len(set(phases))
    (kills,) = [e for e in evs if e.kind is EventKind.KILLS]
    assert kills.data["switches"] and not any(s["latched"] for s in kills.data["switches"])
    assert any(e.kind is EventKind.REGIME and e.data["tags"] for e in evs)
    shadow = [e for e in evs if e.kind is EventKind.LOG and "SHADOW signal" in e.data["text"]]
    assert shadow and all("not traded" in e.data["text"] for e in shadow)
    assert len(shadow) == len(statuses[-1]["shadow_signals_today"])  # each signal once
    assert not any(e.kind in (EventKind.ORDER, EventKind.FILL, EventKind.INTENT, EventKind.DECISION) for e in evs)
    for e in evs:
        json.loads(e.to_json())  # every payload serialises
    assert tr.all_events(statuses[-1]) == []  # the same reading twice publishes nothing new


def test_only_the_upstox_feed_marks_events_real_and_the_paper_broker_never(kernel: Kernel) -> None:
    status: dict[str, Any] = {
        "public": {"mode": "PAPER", "feed": "Upstox Feed V3: HEALTHY", "last_step_at": "2026-10-06T10:01:05+05:30"},
        "day": "2026-10-06",
        "nav": "10000",
        "market": {"bar_start": "2026-10-06T10:00:00+05:30", "open": "25000", "high": "25010", "low": "24990",
                   "close": "25005", "vix": "12.5", "source": "Upstox Feed V3", "real_data": True},
        "regime": {"ts": "2026-10-06T10:01:00+05:30", "classifier": "RC-x", "status": "UNVALIDATED", "warmup": False},
        "regime_tags": ["TREND_UP"],
        "positions": {"NSE_FO|1": "65"},
        "open_orders": 1,
        "kill_detail": [{"id": "DAILY_LOSS_KILL", "scope": "", "reason": "test", "at": "2026-10-06T10:00:00+05:30"}],
        "recent_alerts": [["2026-10-06T10:00:30+05:30", "WARNING", "feed stale 12 s"]],
    }  # fmt: skip
    evs = HostTranslator(kernel).all_events(status)
    by = {e.kind: e for e in evs}
    assert not by[EventKind.TICK].simulated and not by[EventKind.REGIME].simulated
    assert by[EventKind.POSITION].simulated and by[EventKind.POSITION].data["open"]
    assert by[EventKind.SESSION].simulated and by[EventKind.KILLS].simulated
    assert [s["latched"] for s in by[EventKind.KILLS].data["switches"] if s["id"] == "DAILY_LOSS_KILL"] == [True]
    assert any(e.kind is EventKind.LOG and "feed stale" in e.data["text"] for e in evs)
    assert "SIMULATED" in HOST_LABEL  # so the frontend's indicator can never show LIVE (model/mode.ts)


def test_the_runner_is_read_only_loopback_only_and_survives_a_missing_host(kernel: Kernel) -> None:
    with pytest.raises(DashboardError, match="loopback"):
        http_fetcher("https://api.example.com/private/status", SECRET)
    bus = EventBus()

    def down() -> dict[str, Any]:
        raise ConnectionRefusedError

    r = HostFeedRunner(bus, kernel, down)
    with pytest.raises(DashboardError, match="Telegram /kill"):
        r.kill_port().manual_master_kill("x", "owner")
    assert r.poll_once() == 0 and r.poll_once() == 0
    (warn,) = bus.since(0)
    assert warn.kind is EventKind.LOG and "unreachable" in warn.data["text"]  # said once, not every poll


def test_end_to_end_over_loopback_http(kernel: Kernel) -> None:
    board = StatusBoard()
    board.publish(
        {"mode": "PAPER", "real_money": "OFF", "last_step_at": "2026-10-06T09:30:00+05:30", "feed": "none"},
        {"day": "2026-10-06", "positions": {}, "open_orders": 0, "nav": "10000"},
    )
    st = serve_status(board, 0, proxy_secret=SECRET)
    url = f"http://127.0.0.1:{st.server_address[1]}/private/status"
    bus = EventBus()
    runner = HostFeedRunner(bus, kernel, http_fetcher(url, SECRET))
    app = DashboardApp(bus, KillGate(runner.kill_port()), _NoReplays(), {}, ROOT / "missing", label=HOST_LABEL)
    srv = make_server(app, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert runner.poll_once() > 0
        with pytest.raises(urllib.error.HTTPError, match="404"):  # a wrong secret: the private path "does not exist"
            http_fetcher(url, "wrong-" + SECRET)()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(base + "/api/snapshot", timeout=5) as resp:
            snap = json.loads(resp.read())
        assert snap["label"] == HOST_LABEL and snap["latest"]["SESSION"]["data"]["scenario"] == "host-20261006"
        req = urllib.request.Request(base + "/api/health", headers={PROXY_HEADER: SECRET})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert json.loads(resp.read())["label"] == HOST_LABEL
    finally:
        srv.shutdown()
        srv.server_close()
        st.shutdown()
        st.server_close()


class _NoReplays:
    def catalogue(self) -> list[dict[str, Any]]:
        return []

    def day(self, name: str) -> list[dict[str, Any]]:
        raise DashboardError("no replays in this test")

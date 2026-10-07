"""python -m project100c.observability.dashboard [--mode live|replay] [--port 8765] [--speed 30] [--source sim|host]

Starts the read-only dashboard server on 127.0.0.1 with its SIMULATED live stream (``--source sim``, the default)
or with the paper/shadow host's stream (``--source host``: polls the host's private status on loopback with the
proxy's shared header from ``P100C_PROXY_SHARED``; see hostfeed.py)."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, time
from pathlib import Path

from project100c.observability.dashboard.events import EventBus
from project100c.observability.dashboard.hostfeed import HOST_LABEL, HostFeedRunner, http_fetcher
from project100c.observability.dashboard.live import LiveRunner
from project100c.observability.dashboard.replays import ReplayLibrary
from project100c.observability.dashboard.server import DashboardApp, KillGate, make_server
from project100c.observability.dashboard.simulator import REPO, Kernel


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m project100c.observability.dashboard", description=__doc__)
    ap.add_argument("--mode", choices=("live", "replay"), default="live", help="which view the page opens in")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--speed", type=float, default=30.0, help="live: simulated seconds per wall second")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2026, 10, 5), help="live: first SIMULATED day")
    ap.add_argument("--join", type=time.fromisoformat, default=None, help="live: fast-forward day 1 to HH:MM IST")
    ap.add_argument("--static", type=Path, default=REPO / "dashboard" / "dist", help="built frontend directory")
    ap.add_argument("--source", choices=("sim", "host"), default="sim", help="SIMULATED stream or the host's")
    ap.add_argument("--host-status", default="http://127.0.0.1:8780/private/status", help="host: status URL")
    ap.add_argument("--poll", type=float, default=2.0, help="host: seconds between polls")
    a = ap.parse_args(argv)
    if "DHAN_ACCESS_TOKEN" in os.environ:
        print("refusing to start: DHAN_ACCESS_TOKEN is set; the dashboard never touches a broker", file=sys.stderr)
        return 2
    kernel = Kernel.load()
    bus = EventBus()
    runner: LiveRunner | HostFeedRunner
    if a.source == "host":
        secret = os.environ.get("P100C_PROXY_SHARED", "")
        if len(secret) < 24:
            print("refusing to start: --source host needs P100C_PROXY_SHARED (24+ chars)", file=sys.stderr)
            return 2
        runner = HostFeedRunner(bus, kernel, http_fetcher(a.host_status, secret), poll_s=a.poll)
        label = HOST_LABEL
    else:
        runner = LiveRunner(bus, kernel, speed=a.speed, start=a.start, join_at=a.join)
        label = "SIMULATED"
    app = DashboardApp(
        bus,
        KillGate(runner.kill_port()),
        ReplayLibrary(kernel),
        {"window": kernel.window_json(), "limits_version": kernel.limits.version, "speed": a.speed, "source": a.source},
        a.static,
        mode=a.mode,
        label=label,
    )
    srv = make_server(app, port=a.port)
    runner.start_thread()
    print(f"Project 100C dashboard ({label}, read-only) on http://127.0.0.1:{a.port}/  mode={a.mode}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping.set()
        runner.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

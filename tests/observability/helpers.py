from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project100c.observability.dashboard.events import DashboardError, EventBus
from project100c.observability.dashboard.server import DashboardApp, KillGate, KillOnlyPort, make_server


@dataclass
class StubKill:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def manual_master_kill(self, reason: str, requested_by: str) -> dict[str, Any]:
        self.calls.append((reason, requested_by))
        return {"latched": len(self.calls) == 1, "already_latched": len(self.calls) > 1}


class StubReplays:
    def catalogue(self) -> list[dict[str, Any]]:
        return [{"name": "d1", "day": "2026-10-05", "title": "SIMULATED", "seed": 1}]

    def day(self, name: str) -> list[dict[str, Any]]:
        if name != "d1":
            raise DashboardError(f"no replay named {name!r}")
        return [{"seq": 1, "kind": "TICK", "simulated": True}]


@dataclass
class Running:
    app: DashboardApp
    port: int

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def json(self, method: str, path: str, obj: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        body = None if obj is None else json.dumps(obj).encode()
        h = {"Content-Type": "application/json", **(headers or {})}
        st, _, raw = self.request(method, path, body, h)
        return st, json.loads(raw)


@contextmanager
def serve(
    bus: EventBus | None = None,
    port_obj: KillOnlyPort | None = None,
    static_dir: Path | None = None,
    heartbeat: float = 0.2,
) -> Iterator[Running]:
    app = DashboardApp(
        bus or EventBus(),
        KillGate(port_obj or StubKill()),
        StubReplays(),
        {"window": {"entry_start": "09:20:00"}},
        static_dir or Path("/nonexistent-dist"),
        sse_heartbeat_s=heartbeat,
    )
    srv = make_server(app, port=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield Running(app, srv.server_address[1])
    finally:
        app.stopping.set()
        app.bus.close()
        srv.shutdown()
        srv.server_close()

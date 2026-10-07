"""The dashboard's HTTP server (stdlib only; binds 127.0.0.1).

READ-ONLY BY CONSTRUCTION:
* ``ROUTES`` is the complete list of what the server answers. Anything else is 404; a known path with the wrong
  method is 405; PUT/DELETE/PATCH/etc. are 501 for every path.
* The only POST routes are the two MANUAL_MASTER_KILL steps (arm -> confirm). There is no order, trade, modify or
  cancel endpoint, and this module does not import the broker or the kernel runtime; it is handed a kill-only port.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from project100c.observability.dashboard.events import SIMULATED_LABEL, DashboardError, EventBus, EventKind
from project100c.observability.dashboard.topology import topology_json

CONFIRM_WORD = "MANUAL_MASTER_KILL"
CONFIRM_HEADER = "X-P100C-Confirm"
ARM_TTL_S = 30.0
MAX_BODY = 4096


class KillOnlyPort(Protocol):
    def manual_master_kill(self, reason: str, requested_by: str) -> dict[str, Any]: ...


class ReplaySource(Protocol):
    def catalogue(self) -> list[dict[str, Any]]: ...

    def day(self, name: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str  # exact path, or a prefix ending in "/" when prefix=True
    handler: str
    prefix: bool = False
    doc: str = ""


_SNAPSHOT_SKIP = frozenset({EventKind.CHAIN, EventKind.RISK})  # in "latest"; the bulkiest kinds of a day

ROUTES: tuple[Route, ...] = (
    Route("GET", "/", "index", doc="the dashboard (built frontend)"),
    Route("GET", "/assets/", "asset", prefix=True, doc="frontend static assets"),
    Route("GET", "/favicon.svg", "asset_root", doc="icon"),
    Route("GET", "/api/health", "health", doc="liveness"),
    Route("GET", "/api/routes", "routes", doc="this table"),
    Route("GET", "/api/topology", "topology", doc="brain nodes and edges"),
    Route("GET", "/api/config", "config", doc="window times, limits, labels"),
    Route("GET", "/api/snapshot", "snapshot", doc="latest event of each kind + log tail + the day so far"),
    Route("GET", "/api/stream", "stream", doc="SSE event stream (?after=seq or Last-Event-ID)"),
    Route("GET", "/api/replay", "replay_list", doc="SIMULATED replay days"),
    Route("GET", "/api/replay/", "replay_day", prefix=True, doc="one SIMULATED day's events"),
    Route("POST", "/api/kill/arm", "kill_arm", doc="step 1 of MANUAL_MASTER_KILL: get a 30 s nonce"),
    Route("POST", "/api/kill/confirm", "kill_confirm", doc="step 2: confirm with nonce + typed word + header"),
)


def match_route(method: str, path: str) -> tuple[Route | None, bool]:
    """(route, path_known). path_known is True if some route matches the path under any method."""
    known = False
    for r in ROUTES:
        hit = path.startswith(r.path) and len(path) > len(r.path) if r.prefix else path == r.path
        if hit:
            known = True
            if r.method == method:
                return r, True
    return None, known


@dataclass
class KillGate:
    """Two-step confirm: arm returns a single-use nonce; confirm must present it within ARM_TTL_S, together with the
    typed word and a custom header (so a plain cross-site form post cannot trigger it)."""

    port: KillOnlyPort
    now: Callable[[], float] = time.monotonic
    _armed: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def arm(self) -> dict[str, Any]:
        nonce = secrets.token_urlsafe(18)
        with self._lock:
            t = self.now()
            self._armed = {k: v for k, v in self._armed.items() if v > t}
            self._armed[nonce] = t + ARM_TTL_S
        return {"nonce": nonce, "expires_in_s": ARM_TTL_S, "confirm_word": CONFIRM_WORD}

    def confirm(self, nonce: str, word: str, reason: str) -> dict[str, Any]:
        with self._lock:
            exp = self._armed.pop(nonce, None)  # single use, even when the rest fails
        if exp is None or exp < self.now():
            raise DashboardError("kill not armed or the arm expired: arm again")
        if word != CONFIRM_WORD:
            raise DashboardError(f"type {CONFIRM_WORD} exactly to confirm")
        return self.port.manual_master_kill(reason or "owner pressed MANUAL_MASTER_KILL on the dashboard", "owner")


@dataclass
class DashboardApp:
    bus: EventBus
    kill: KillGate
    replays: ReplaySource
    config: dict[str, Any]
    static_dir: Path
    mode: str = "live"
    sse_heartbeat_s: float = 10.0
    label: str = SIMULATED_LABEL  # the host stream says what it is (hostfeed.HOST_LABEL; still contains SIMULATED)
    stopping: threading.Event = field(default_factory=threading.Event)


class Handler(BaseHTTPRequestHandler):
    server_version = "P100C-Dashboard/0.1"
    app: DashboardApp  # set on the subclass made by make_server

    # ---- plumbing
    def log_message(self, format: str, *args: Any) -> None:  # quiet by default
        return

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def _error(self, status: HTTPStatus, msg: str) -> None:
        self._json(status, {"error": msg, "status": int(status)})

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        route, known = match_route(method, url.path)
        if route is None:
            if known:
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, f"{method} not allowed on {url.path}")
            else:
                self._error(HTTPStatus.NOT_FOUND, f"no such endpoint: {url.path} (this dashboard is read-only)")
            return
        try:
            getattr(self, "h_" + route.handler)(url.path, parse_qs(url.query))
        except DashboardError as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._error(HTTPStatus.NOT_IMPLEMENTED, "HEAD not supported")

    def _refuse(self) -> None:
        self._error(HTTPStatus.NOT_IMPLEMENTED, f"{self.command} is not supported: this dashboard is read-only")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _refuse

    def _discard_body(self) -> None:
        """Drain a bounded unexpected body before closing a read-only streaming response."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise DashboardError("request body too large")
        if n:
            self.rfile.read(n)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise DashboardError("request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise DashboardError(f"body is not JSON: {e}") from e
        if not isinstance(obj, dict):
            raise DashboardError("body must be a JSON object")
        return obj

    # ---- static
    def _static(self, rel: str) -> None:
        root = self.app.static_dir.resolve()
        p = (root / rel).resolve()
        if root not in p.parents and p != root:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not p.is_file():
            if rel == "index.html":
                msg = (
                    "<h1>Project 100C dashboard: frontend not built</h1><p>Run <code>cd dashboard && npm install "
                    "&& npm run build</code>, then reload. The API is up: <a href=/api/routes>/api/routes</a>.</p>"
                )
                self._send(HTTPStatus.OK, msg.encode(), "text/html; charset=utf-8")
                return
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, p.read_bytes(), ctype)

    def h_index(self, path: str, q: dict[str, list[str]]) -> None:
        self._static("index.html")

    def h_asset(self, path: str, q: dict[str, list[str]]) -> None:
        self._static(path.lstrip("/"))

    def h_asset_root(self, path: str, q: dict[str, list[str]]) -> None:
        self._static(path.lstrip("/"))

    # ---- API
    def h_health(self, path: str, q: dict[str, list[str]]) -> None:
        self._json(200, {"ok": True, "mode": self.app.mode, "label": self.app.label, "last_seq": self.app.bus.last_seq})

    def h_routes(self, path: str, q: dict[str, list[str]]) -> None:
        self._json(200, [{"method": r.method, "path": r.path, "prefix": r.prefix, "doc": r.doc} for r in ROUTES])

    def h_topology(self, path: str, q: dict[str, list[str]]) -> None:
        self._json(200, topology_json())

    def h_config(self, path: str, q: dict[str, list[str]]) -> None:
        self._json(200, {**self.app.config, "mode": self.app.mode, "label": self.app.label})

    def h_snapshot(self, path: str, q: dict[str, list[str]]) -> None:
        bus = self.app.bus
        self._json(
            200,
            {
                "last_seq": bus.last_seq,
                "latest": bus.snapshot(),
                "log": [e.as_dict() for e in bus.tail(EventKind.LOG, 80)],
                # the day so far, minus the two bulkiest kinds whose latest value is already in "latest"
                "session": [e.as_dict() for e in bus.current_session(_SNAPSHOT_SKIP)],
                "label": self.app.label,
            },
        )

    def h_stream(self, path: str, q: dict[str, list[str]]) -> None:
        self._discard_body()
        after_raw = (q.get("after") or [self.headers.get("Last-Event-ID") or ""])[0]
        try:
            after = int(after_raw) if after_raw else self.app.bus.last_seq
        except ValueError as e:
            raise DashboardError(f"bad 'after': {after_raw!r}") from e
        once = (q.get("once") or ["0"])[0] == "1"  # tests: send what is there and close
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b"retry: 2000\n: SIMULATED event stream (read-only)\n\n")
        self.wfile.flush()
        bus = self.app.bus
        while not self.app.stopping.is_set():
            evs = bus.since(after, timeout=0 if once else self.app.sse_heartbeat_s, limit=500)
            if evs:
                chunk = "".join(f"id: {e.seq}\nevent: dash\ndata: {e.to_json()}\n\n" for e in evs)
                self.wfile.write(chunk.encode())
                after = evs[-1].seq
            elif once or bus.closed:
                break
            else:
                self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            if once and len(evs) < 500:
                break

    def h_replay_list(self, path: str, q: dict[str, list[str]]) -> None:
        self._json(200, {"days": self.app.replays.catalogue(), "label": SIMULATED_LABEL})

    def h_replay_day(self, path: str, q: dict[str, list[str]]) -> None:
        name = path.removeprefix("/api/replay/")
        self._json(200, {"name": name, "events": self.app.replays.day(name), "label": SIMULATED_LABEL})

    def h_kill_arm(self, path: str, q: dict[str, list[str]]) -> None:
        self._require_confirm_header()
        self._json(200, self.app.kill.arm())

    def h_kill_confirm(self, path: str, q: dict[str, list[str]]) -> None:
        self._require_confirm_header()
        b = self._body()
        res = self.app.kill.confirm(str(b.get("nonce", "")), str(b.get("confirm", "")), str(b.get("reason", "")))
        self._json(200, {**res, "switch": CONFIRM_WORD, "label": SIMULATED_LABEL})

    def _require_confirm_header(self) -> None:
        if self.headers.get(CONFIRM_HEADER) != CONFIRM_WORD:
            raise DashboardError(f"missing {CONFIRM_HEADER} header")


def make_server(app: DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise DashboardError("the dashboard binds to localhost only")
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv

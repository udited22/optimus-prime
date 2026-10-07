"""The host's status page (docs/engineering/security.md §dashboard publishing): a public read-only view and a private
detail view.

Two audiences, one small standard-library HTTP server bound to loopback (the reverse proxy is the only way in):

* **Public** (``GET /``, ``GET /status.json``): whether the system is up and what it is doing, nothing that
  describes money. Mode, "real money: OFF", market phase and next event, token-gate state, feed health, whether
  a kill or halt is latched (yes/no only), last step time and the honesty labels. No positions, P&L, NAV, order
  IDs, account or instrument details, kill reasons or strategy names. `StatusBoard.publish` enforces this with an
  allow-list: any other key handed to the public view is dropped.
* **Private** (``GET /private/status``): positions, paper P&L, open orders, kill reasons, shadow signals. Served
  only when the request carries ``X-P100C-Proxy-Auth`` equal to the shared secret (``P100C_PROXY_SHARED``,
  compared in constant time). The reverse proxy adds that header only after the owner passed its own
  authentication (Caddy ``basic_auth``) and strips any copy sent by the client. Without
  a configured secret (or with a wrong one) the private path answers 404, so its existence is not revealed.
* **Health** (``GET /healthz``): 200 while the host loop has stepped within ``max_step_age`` seconds, else 503.
  The container health check and the proxy use it. It carries no detail.

Every response has ``Cache-Control: no-store`` and a restrictive content-security policy; anything but GET is 405.
Request lines are not logged.
"""

from __future__ import annotations

import hmac
import html
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROXY_HEADER = "X-P100C-Proxy-Auth"
MIN_SECRET_LEN = 24
PUBLIC_KEYS = frozenset(
    {
        "service",
        "mode",
        "real_money",
        "phase",
        "next_event",
        "gate",
        "feed",
        "kill_latched",
        "halted",
        "steps",
        "last_step_at",
        "started_at",
        "labels",
    }
)
_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"),
)


@dataclass
class StatusBoard:
    """The latest status, written by the host loop and read by the server threads."""

    max_step_age: float = 30.0
    _public: dict[str, Any] = field(default_factory=dict)
    _private: dict[str, Any] = field(default_factory=dict)
    _beat: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, public: Mapping[str, Any], private: Mapping[str, Any] | None = None) -> None:
        pub = {k: v for k, v in public.items() if k in PUBLIC_KEYS}  # allow-list: nothing else is ever public
        with self._lock:
            self._public = json.loads(json.dumps(pub, default=str))
            if private is not None:
                self._private = json.loads(json.dumps(dict(private), default=str))

    def heartbeat(self) -> None:
        with self._lock:
            self._beat = time.monotonic()

    def healthy(self) -> bool:
        with self._lock:
            return self._beat is not None and time.monotonic() - self._beat <= self.max_step_age

    def public(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._public)

    def private(self) -> dict[str, Any]:
        with self._lock:
            return {"public": dict(self._public), **self._private}


def render_public_html(status: Mapping[str, Any]) -> str:
    def row(k: str, v: Any) -> str:
        return f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"

    rows = "".join(row(k, v) for k, v in status.items() if k != "labels")
    labels = "".join(f"<li>{html.escape(str(x))}</li>" for x in status.get("labels") or ())
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta http-equiv=refresh content=15>"
        "<meta name=viewport content='width=device-width,initial-scale=1'><title>Project 100C status</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}"
        "th{text-align:left;padding-right:1.5rem}td,th{padding:.2rem 0}</style></head><body>"
        "<h1>Project 100C: host status</h1><p><strong>Paper / shadow mode. Real money is OFF.</strong> "
        "This page shows whether the system is running; it shows no positions, P&amp;L or account details.</p>"
        f"<table>{rows}</table><ul>{labels}</ul></body></html>"
    )


def handle(
    board: StatusBoard, method: str, path: str, headers: Mapping[str, str], proxy_secret: str | None
) -> tuple[int, str, bytes]:
    """The whole policy, free of HTTP: (status code, content type, body)."""

    def js(code: int, obj: object) -> tuple[int, str, bytes]:
        return code, "application/json", json.dumps(obj, sort_keys=True).encode()

    if method not in ("GET", "HEAD"):
        return js(405, {"error": "read-only"})
    p = path.split("?", 1)[0]
    if p == "/healthz":
        ok = board.healthy()
        return js(200 if ok else 503, {"ok": ok})
    if p == "/status.json":
        return js(200, board.public())
    if p in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", render_public_html(board.public()).encode()
    if p == "/private/status":
        got = {k.lower(): v for k, v in headers.items()}.get(PROXY_HEADER.lower(), "")
        if proxy_secret and len(proxy_secret) >= MIN_SECRET_LEN and hmac.compare_digest(got, proxy_secret):
            return js(200, board.private())
    return js(404, {"error": "not found"})


def serve_status(
    board: StatusBoard, port: int, *, proxy_secret: str | None, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    """Start the status server in a daemon thread (loopback only; the reverse proxy publishes it)."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the status server listens on loopback only; publish it through the reverse proxy")
    if proxy_secret is not None and len(proxy_secret) < MIN_SECRET_LEN:
        raise ValueError(f"P100C_PROXY_SHARED must be at least {MIN_SECRET_LEN} characters")

    class Handler(BaseHTTPRequestHandler):
        server_version = "p100c"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _do(self, method: str) -> None:
            code, ctype, body = handle(board, method, self.path, dict(self.headers.items()), proxy_secret)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in _SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            self._do("GET")

        def do_HEAD(self) -> None:
            self._do("HEAD")

        def do_POST(self) -> None:
            self._do("POST")

        def do_PUT(self) -> None:
            self._do("PUT")

        def do_DELETE(self) -> None:
            self._do("DELETE")

    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

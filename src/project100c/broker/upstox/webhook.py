"""Receiver for Upstox's Notifier Webhook: the daily access token arrives here after the owner approves it
(docs/engineering/alerts-and-daily-token.md).

`NotifierReceiver.handle()` is the whole policy, free of HTTP so it is easy to test; `serve()` wraps it in a
standard-library HTTP server. TLS is either terminated here (pass an `ssl.SSLContext` built from the host's
certificate) or by a reverse proxy on the same host; plain HTTP must then listen on 127.0.0.1 only.

Rules (fail closed):

* Only ``POST`` to the configured path; the path should carry an unguessable segment (defence in depth: Upstox
  does not sign the payload). Everything else is 404 or 405 and changes nothing.
* JSON bodies of at most 4 KB, at most `max_requests_per_min` requests a minute.
* The payload must pass `session_from_webhook` (message type, this app's client ID, Bearer, validity window,
  03:30 IST rule). Then the token gate is told the expiry and the session is handed to `on_session`, in memory.
* The token is **never written anywhere**: not to disk, the journal, a log line or an HTTP response. Responses
  carry no detail beyond accepted or refused.
"""

from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from project100c.broker.upstox.auth import session_from_webhook
from project100c.broker.upstox.credentials import UpstoxSession
from project100c.errors import BrokerError
from project100c.ops.daily_gate import TokenGate

MAX_BODY = 4096


@dataclass
class NotifierReceiver:
    expected_client_id: str
    path: str
    gate: TokenGate
    on_session: Callable[[UpstoxSession], None]
    clock: Callable[[], datetime]
    max_requests_per_min: int = 20
    accepted: int = 0
    refused: int = 0
    _recent: list[datetime] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.path.startswith("/") or len(self.path) < 16:
            raise BrokerError("webhook path must start with / and carry an unguessable segment (>= 16 chars)")

    def handle(self, method: str, path: str, content_type: str, body: bytes) -> tuple[int, dict[str, str]]:
        now = self.clock()
        with self._lock:
            if path.split("?", 1)[0] != self.path:
                return 404, {"status": "not found"}  # not counted: a scanner must not block the real callback
            self._recent = [t for t in self._recent if now - t < timedelta(minutes=1)]
            if len(self._recent) >= self.max_requests_per_min:
                self.refused += 1
                return 429, {"status": "refused"}
            self._recent.append(now)
            if method != "POST":
                return 405, {"status": "method not allowed"}
            if not content_type.lower().startswith("application/json") or len(body) > MAX_BODY:
                self.refused += 1
                return 400, {"status": "refused"}
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
                session = session_from_webhook(payload, expected_client_id=self.expected_client_id, now=now)
            except (ValueError, BrokerError):
                self.refused += 1
                return 400, {"status": "refused"}
            if not self.gate.on_token(session.expires_at, now):
                self.refused += 1
                return 409, {"status": "refused"}
            self.on_session(session)
            self.accepted += 1
            return 200, {"status": "accepted"}


def serve(
    receiver: NotifierReceiver, host: str, port: int, *, ssl_context: ssl.SSLContext | None = None
) -> ThreadingHTTPServer:
    """Start the receiver in a daemon thread and return the server (call ``shutdown()`` to stop)."""
    if ssl_context is None and host not in ("127.0.0.1", "localhost", "::1"):
        raise BrokerError("plain HTTP only on loopback: terminate TLS here or in a local reverse proxy")

    class Handler(BaseHTTPRequestHandler):
        server_version = "p100c"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            pass  # never log requests: the body carries the token

        def _do(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, MAX_BODY + 1)) if n > 0 else b""
            code, out = receiver.handle(method, self.path, self.headers.get("Content-Type", ""), body)
            raw = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            self._do("POST")

        def do_GET(self) -> None:
            self._do("GET")

        def do_PUT(self) -> None:
            self._do("PUT")

    srv = ThreadingHTTPServer((host, port), Handler)
    if ssl_context is not None:
        srv.socket = ssl_context.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

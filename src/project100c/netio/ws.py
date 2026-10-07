"""A minimal RFC 6455 WebSocket client on the standard library (no third-party dependency).

Scope: what a market-data feed needs. Client-masked text and binary frames, fragmented messages, ping/pong,
close, a size limit, and timeouts. TLS verifies the server certificate with the system trust store; plain ``ws://``
is allowed only to localhost (tests). Nothing here logs the URL, which may carry a one-time auth code.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

from project100c.errors import Project100CError

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 8 * 1024 * 1024


class WsError(Project100CError):
    """Handshake or protocol failure."""


class WsClosed(WsError):
    """The connection is closed (by either side or by a socket error)."""


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


@dataclass
class WsConnection:
    sock: socket.socket
    closed: bool = False
    pending: bytes = b""  # bytes that arrived with the handshake response

    def _send_frame(self, op: int, payload: bytes) -> None:
        if self.closed:
            raise WsClosed("send on a closed connection")
        n = len(payload)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(head + mask + masked)
        except OSError as e:
            self.closed = True
            raise WsClosed(f"send failed: {type(e).__name__}") from None

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BIN, data)

    def _read_exact(self, n: int) -> bytes:
        buf, self.pending = self.pending[:n], self.pending[n:]
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except TimeoutError:
                if buf:
                    raise WsError("timed out in the middle of a frame") from None
                raise
            except OSError as e:
                self.closed = True
                raise WsClosed(f"recv failed: {type(e).__name__}") from None
            if not chunk:
                self.closed = True
                raise WsClosed("peer closed the socket")
            buf += chunk
        return buf

    def recv(self, timeout_s: float) -> bytes | None:
        """The next complete data message, or None if nothing arrived within `timeout_s`. Control frames are
        handled here (ping is answered, close raises `WsClosed`)."""
        if self.closed:
            raise WsClosed("recv on a closed connection")
        self.sock.settimeout(timeout_s)
        parts: list[bytes] = []
        total = 0
        while True:
            try:
                b0, b1 = self._read_exact(2)
            except TimeoutError:
                if parts:
                    raise WsError("timed out in the middle of a fragmented message") from None
                return None
            self.sock.settimeout(max(timeout_s, 5.0))  # a started frame must finish
            fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", self._read_exact(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", self._read_exact(8))
            if n > MAX_MESSAGE or total + n > MAX_MESSAGE:
                self.close()
                raise WsError("message too large")
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(n) if n else b""
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.closed = True
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                raise WsClosed(f"closed by peer (code {code})")
            if op not in (OP_CONT, OP_TEXT, OP_BIN):
                self.close()
                raise WsError(f"unknown opcode {op}")
            parts.append(data)
            total += n
            if fin:
                return b"".join(parts)

    def close(self) -> None:
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except WsClosed:
                pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def ws_connect(url: str, *, timeout_s: float = 10.0, headers: dict[str, str] | None = None) -> WsConnection:
    u = urlsplit(url)
    host = u.hostname or ""
    if u.scheme == "ws" and host not in ("127.0.0.1", "localhost"):
        raise WsError("plain ws:// is allowed only to localhost")
    if u.scheme not in ("ws", "wss"):
        raise WsError("not a WebSocket URL")
    port = u.port or (443 if u.scheme == "wss" else 80)
    try:
        raw = socket.create_connection((host, port), timeout=timeout_s)
    except OSError as e:
        raise WsClosed(f"connect failed: {type(e).__name__}") from None
    sock: socket.socket = raw
    if u.scheme == "wss":
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n"
    )
    try:
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk or len(resp) > 16384:
                raise WsError("bad handshake response")
            resp += chunk
    except OSError as e:
        sock.close()
        raise WsClosed(f"handshake failed: {type(e).__name__}") from None
    head, _, rest = resp.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    if not lines[0].startswith("HTTP/1.1 101") or hdrs.get("sec-websocket-accept") != accept_key(key):
        sock.close()
        raise WsError(f"handshake refused: {lines[0][:40]}")
    return WsConnection(sock, pending=rest)

"""A small RFC 6455 WebSocket server on the standard library, for feed-client tests (localhost only)."""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time
from collections.abc import Callable


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


class ServerConn:
    def __init__(self, sock: socket.socket, accept_override: str | None) -> None:
        self.sock = sock
        self.received: list[tuple[int, bytes]] = []
        self.alive = True
        req = b""
        while b"\r\n\r\n" not in req:
            req += sock.recv(4096)
        hdrs = {}
        for line in req.decode().split("\r\n")[1:]:
            k, _, v = line.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        self.path = req.decode().split(" ")[1]
        key = hdrs["sec-websocket-key"]
        acc = (
            accept_override
            or base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        )
        sock.sendall(
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode()
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _exact(self, n: int) -> bytes:
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise ConnectionError
            b += c
        return b

    def _read(self) -> None:
        try:
            while True:
                b0, b1 = self._exact(2)
                n = b1 & 0x7F
                if n == 126:
                    (n,) = struct.unpack("!H", self._exact(2))
                elif n == 127:
                    (n,) = struct.unpack("!Q", self._exact(8))
                mask = self._exact(4) if b1 & 0x80 else b""
                data = self._exact(n)
                if mask:
                    data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
                self.received.append((b0 & 0x0F, data))
        except (ConnectionError, OSError):
            self.alive = False

    def frame(self, op: int, payload: bytes, *, fin: bool = True) -> None:
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | op])
        if n < 126:
            head += bytes([n])
        elif n < 1 << 16:
            head += bytes([126]) + struct.pack("!H", n)
        else:
            head += bytes([127]) + struct.pack("!Q", n)
        self.sock.sendall(head + payload)

    def send(self, payload: bytes) -> None:
        self.frame(0x2, payload)

    def drop(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class FakeWsServer:
    def __init__(self, *, accept_override: str | None = None) -> None:
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.conns: list[ServerConn] = []
        self.accept_override = accept_override
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(ServerConn(s, self.accept_override))

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/market-data-feeder/v3/feeds?requestId=r1&code=one-time"

    def close(self) -> None:
        for c in self.conns:
            c.drop()
        self.srv.close()

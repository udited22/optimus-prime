"""Minimal HTTP transport seam. Production uses ``UrllibTransport``; tests use ``ScriptedTransport`` with
recorded or fake responses, so no test ever touches the network.

Transports never log, and exceptions never include request headers, so an auth header cannot leak.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from project100c.errors import EndpointNotAllowedError, TransportError


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None

    def __repr__(self) -> str:  # never print header values (they may carry a token)
        return f"HttpRequest({self.method} {self.url}, headers={sorted(self.headers)}, body={len(self.body or b'')}B)"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def send(self, request: HttpRequest, *, timeout_s: float) -> HttpResponse: ...


class UrllibTransport:
    """Real HTTPS transport (stdlib). Only ``https`` URLs on ``allowed_hosts`` are sent."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self._hosts = allowed_hosts

    def send(self, request: HttpRequest, *, timeout_s: float) -> HttpResponse:
        parts = urlsplit(request.url)
        if parts.scheme != "https" or parts.hostname not in self._hosts:
            raise EndpointNotAllowedError(f"refusing to send to {parts.scheme}://{parts.hostname}")
        req = urllib.request.Request(
            request.url, data=request.body, method=request.method, headers=dict(request.headers)
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return HttpResponse(int(resp.status), resp.read(), {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as e:
            body = e.read() if e.fp is not None else b""
            return HttpResponse(int(e.code), body, {k.lower(): v for k, v in (e.headers or {}).items()})
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise TransportError(f"{request.method} {parts.hostname}{parts.path}: {type(e).__name__}: {e}") from None


Responder = Callable[[HttpRequest], HttpResponse]


class ScriptedTransport:
    """Test transport: a responder function (or a fixed script) decides each response; all requests are kept.

    Raising ``TransportError`` from the responder simulates a network failure.
    """

    def __init__(self, responder: Responder | Sequence[HttpResponse]) -> None:
        if callable(responder):
            self._fn: Responder = responder
        else:
            script = list(responder)

            def _next(_: HttpRequest) -> HttpResponse:
                if not script:
                    raise AssertionError("ScriptedTransport: script exhausted")
                return script.pop(0)

            self._fn = _next
        self.requests: list[HttpRequest] = []
        self.timeouts: list[float] = []

    def send(self, request: HttpRequest, *, timeout_s: float) -> HttpResponse:
        self.requests.append(request)
        self.timeouts.append(timeout_s)
        return self._fn(request)

"""HTTP transport for the Upstox adapter, injectable so every test runs without a network.

`HttpTransport` is the only class in the codebase that opens a socket to Upstox. It never logs headers or bodies,
and it maps socket-level failures onto the broker error taxonomy: a timeout is `BrokerTimeoutError` (the request's
fate is UNKNOWN, so the gateway reconciles before any retry) and a refused or reset connection is
`BrokerDisconnectedError`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from project100c.errors import BrokerDisconnectedError, BrokerTimeoutError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: dict[str, Any]


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_s: float,
    ) -> HttpResponse: ...


class HttpTransport:
    """stdlib urllib transport. A non-JSON body becomes ``{"_raw": <first 200 chars>}`` so error mapping still
    sees the HTTP status."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_s: float,
    ) -> HttpResponse:
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise BrokerDisconnectedError("refusing a non-HTTPS broker URL")
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        data: bytes | None = None
        hdrs = dict(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(full, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return HttpResponse(resp.status, _decode(resp.read()))
        except urllib.error.HTTPError as e:
            return HttpResponse(e.code, _decode(e.read()))
        except TimeoutError as e:
            raise BrokerTimeoutError(f"{method} {urllib.parse.urlsplit(url).path} timed out") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise BrokerTimeoutError(f"{method} {urllib.parse.urlsplit(url).path} timed out") from e
            raise BrokerDisconnectedError(f"{method} {urllib.parse.urlsplit(url).path}: {e.reason}") from e
        except (ConnectionError, OSError) as e:
            raise BrokerDisconnectedError(f"{method} {urllib.parse.urlsplit(url).path}: {type(e).__name__}") from e


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        v = json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": raw[:200].decode(errors="replace")}
    return v if isinstance(v, dict) else {"_raw": str(v)[:200]}

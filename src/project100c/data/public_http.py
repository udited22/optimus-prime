"""Keyless, read-only HTTP GET for public market-data sources (Binance archive, Stooq, CBOE, ...).

Market-agnostic: it knows nothing about any vendor's payloads. It adds what every source needs on top of the
``HttpTransport`` seam: the shared ``RateLimiter`` (polite pacing plus a persisted daily cap), bounded retries
with exponential backoff for 429 / 5xx / network errors (honouring Retry-After), and an explicit
``None`` for HTTP 404 so callers record "not published" instead of guessing. No credential is ever attached.
"""

from __future__ import annotations

from dataclasses import dataclass

from project100c.data.http import HttpRequest, HttpResponse, HttpTransport
from project100c.data.ratelimit import RateLimiter
from project100c.errors import TransportError, VendorRateLimitError, VendorRequestError, VendorServerError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    server_max_attempts: int
    rate_limit_max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float
    timeout_s: float

    def delay(self, n: int) -> float:
        return float(min(self.backoff_cap_s, self.backoff_base_s * (2 ** (n - 1))))


def _retry_after(resp: HttpResponse) -> float | None:
    v = resp.headers.get("retry-after")
    if v is None:
        return None
    try:
        s = float(v)
    except ValueError:
        return None
    return s if 0 <= s <= 3600 else None


@dataclass(slots=True)
class GetResult:
    response: HttpResponse
    attempts: int


class PublicGetter:
    """GET only. Returns the 200 response, ``None`` for 404, and raises typed errors otherwise."""

    def __init__(self, *, transport: HttpTransport, limiter: RateLimiter, policy: RetryPolicy) -> None:
        self._t = transport
        self._lim = limiter
        self._p = policy
        self.requests = 0

    def get(self, url: str, *, accept: str = "*/*") -> GetResult | None:
        req = HttpRequest("GET", url, {"Accept": accept, "User-Agent": "project100c-research/1 (data only)"})
        server_n = rate_n = attempts = 0
        while True:
            self._lim.acquire()
            attempts += 1
            self.requests += 1
            try:
                resp = self._t.send(req, timeout_s=self._p.timeout_s)
            except TransportError as e:
                server_n += 1
                if server_n >= self._p.server_max_attempts:
                    raise TransportError(f"GET {url}: gave up after {attempts} attempts: {e}") from None
                self._lim.backoff(self._p.delay(server_n))
                continue
            if resp.status == 200:
                return GetResult(resp, attempts)
            if resp.status == 404:
                return None
            if resp.status in (418, 429):
                rate_n += 1
                if rate_n >= self._p.rate_limit_max_attempts:
                    raise VendorRateLimitError(f"GET {url}: HTTP {resp.status}", http_status=resp.status, code=None)
                self._lim.backoff(max(self._p.delay(rate_n), _retry_after(resp) or 0.0))
                continue
            if resp.status >= 500:
                server_n += 1
                if server_n >= self._p.server_max_attempts:
                    raise VendorServerError(
                        f"GET {url}: HTTP {resp.status} after {attempts} attempts", http_status=resp.status, code=None
                    )
                self._lim.backoff(self._p.delay(server_n))
                continue
            snippet = resp.body[:200].decode("utf-8", "replace")
            raise VendorRequestError(f"GET {url}: HTTP {resp.status}: {snippet}", http_status=resp.status, code=None)

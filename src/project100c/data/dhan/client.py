"""Dhan Data API client: HISTORICAL DATA ONLY (OD-011).

The endpoint allowlist below is the complete set of Dhan paths this package can reach. There is no code path
for any trading, portfolio or account-changing endpoint, and tests/data/test_dhan_data_only.py enforces that
statically (source scan) and dynamically (every URL a full job sends is in the allowlist).

Error handling follows the Dhan annexure (S59); nothing is swallowed:

* auth (DH-901, 807-810, HTTP 401) -> VendorAuthError, subscription/account (DH-902, DH-903, 806)
  -> VendorSubscriptionError, static IP not whitelisted (DH-911) -> VendorAuthError: the job stops.
  DH-902 arrives as HTTP 401 with ``errorType`` ``Invalid_Access`` even when ``/profile`` reports the data plan
  ``Active`` (seen on 2-Oct-2026), which is why the preflight also makes one tiny data call (``data_probe``).
* rate limit (HTTP 429, DH-904, 805) -> exponential backoff honouring Retry-After, then VendorRateLimitError.
* server/network (HTTP 5xx, DH-908, DH-909, DH-910, 800, TransportError) -> limited retries, then
  VendorServerError / TransportError.
* bad request (DH-905, 804, 811-814, other 4xx) -> VendorRequestError (not retried).
* no data (DH-907) -> a reply with ``no_data=True``; the job records NO_DATA explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from project100c.data.dhan.config import DhanConfig
from project100c.data.dhan.credentials import DhanCredentials
from project100c.data.http import HttpRequest, HttpResponse, HttpTransport
from project100c.data.ratelimit import RateLimiter
from project100c.errors import (
    EndpointNotAllowedError,
    TransportError,
    VendorAPIError,
    VendorAuthError,
    VendorRateLimitError,
    VendorRequestError,
    VendorResponseError,
    VendorServerError,
    VendorSubscriptionError,
)


class DataEndpoint(StrEnum):
    ROLLING_OPTION = "charts/rollingoption"  # expired options, rolling by ATM offset (S31)
    INTRADAY = "charts/intraday"  # 1/5/15/25/60-min candles (S57)
    HISTORICAL = "charts/historical"  # daily candles (S57)
    PROFILE = "profile"  # read-only preflight: token validity + data-plan status (S60)


# The complete allowlist: endpoint -> HTTP method. Nothing else can be called.
ALLOWED_ENDPOINTS: Mapping[DataEndpoint, str] = MappingProxyType(
    {
        DataEndpoint.ROLLING_OPTION: "POST",
        DataEndpoint.INTRADAY: "POST",
        DataEndpoint.HISTORICAL: "POST",
        DataEndpoint.PROFILE: "GET",
    }
)

_AUTH = frozenset({"DH-901", "807", "808", "809", "810"})
_IP = frozenset({"DH-911"})  # static IP invalid / not whitelisted (annexure); an account setting, never retried
_SUBSCRIPTION = frozenset({"DH-902", "DH-903", "806"})
_RATE = frozenset({"DH-904", "805"})
_SERVER = frozenset({"DH-908", "DH-909", "DH-910", "800"})
_NO_DATA = frozenset({"DH-907"})
_REQUEST = frozenset({"DH-905", "DH-906", "804", "811", "812", "813", "814"})


class Outcome(StrEnum):
    OK = "OK"
    NO_DATA = "NO_DATA"
    AUTH = "AUTH"
    IP = "IP"
    SUBSCRIPTION = "SUBSCRIPTION"
    RATE = "RATE"
    SERVER = "SERVER"
    REQUEST = "REQUEST"


def loads_decimal(body: bytes) -> Any:
    """JSON with every non-integer number as Decimal (never float)."""
    return json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON number {name}")


def _error_fields(parsed: Any) -> tuple[str | None, str]:
    if not isinstance(parsed, dict):
        return None, ""
    code = parsed.get("errorCode")
    msg = parsed.get("errorMessage") or parsed.get("message") or ""
    remarks = parsed.get("remarks")
    if code is None and isinstance(remarks, dict):  # older envelope: {"status": "failure", "remarks": {...}}
        code = remarks.get("error_code") or remarks.get("errorCode")
        msg = msg or remarks.get("error_message") or remarks.get("errorMessage") or ""
    if code is None and parsed.get("status") == "failure":
        code = "UNKNOWN"
    return (None if code is None else str(code).strip()), str(msg)


def classify(resp: HttpResponse) -> tuple[Outcome, str | None, str, Any]:
    """(outcome, vendor code, vendor message, parsed JSON or None)."""
    parsed: Any = None
    try:
        parsed = loads_decimal(resp.body) if resp.body else None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        parsed = None
    code, msg = _error_fields(parsed)
    if resp.status == 200 and code is None:
        if parsed is None:
            raise VendorResponseError("HTTP 200 with a body that is not valid JSON")
        return Outcome.OK, None, "", parsed
    if code in _NO_DATA:
        return Outcome.NO_DATA, code, msg, parsed
    if code in _IP:
        return Outcome.IP, code, msg, parsed
    if code in _AUTH or (code is None and resp.status == 401):
        return Outcome.AUTH, code, msg, parsed
    if code in _SUBSCRIPTION:
        return Outcome.SUBSCRIPTION, code, msg, parsed
    if code in _RATE or resp.status == 429:
        return Outcome.RATE, code, msg, parsed
    if code in _SERVER or resp.status >= 500:
        return Outcome.SERVER, code, msg, parsed
    if code in _REQUEST or 400 <= resp.status < 500:
        return Outcome.REQUEST, code, msg, parsed
    if resp.status == 200:  # 200 with an unknown error envelope: treat as a request problem, never as data
        return Outcome.REQUEST, code, msg, parsed
    return Outcome.SERVER, code, msg, parsed


@dataclass(frozen=True, slots=True)
class DhanReply:
    endpoint: DataEndpoint
    payload: Mapping[str, Any]
    http_status: int
    body: bytes
    parsed: Any
    attempts: int
    fetched_at_utc: str
    no_data: bool
    vendor_code: str | None = None
    vendor_message: str = ""


def _retry_after(resp: HttpResponse) -> float | None:
    v = resp.headers.get("retry-after")
    if v is None:
        return None
    try:
        s = float(v)
    except ValueError:
        return None
    return s if 0 <= s <= 3600 else None


class DhanDataClient:
    def __init__(
        self,
        *,
        credentials: DhanCredentials,
        config: DhanConfig,
        transport: HttpTransport,
        limiter: RateLimiter,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._cred = credentials
        self._cfg = config
        self._transport = transport
        self._limiter = limiter
        self._wall = wall_clock

    def _url(self, endpoint: DataEndpoint) -> tuple[str, str]:
        if not isinstance(endpoint, DataEndpoint) or endpoint not in ALLOWED_ENDPOINTS:
            raise EndpointNotAllowedError(f"endpoint {endpoint!r} is not in the Dhan data-only allowlist")
        return ALLOWED_ENDPOINTS[endpoint], f"{self._cfg.api_base.rstrip('/')}/{endpoint.value}"

    def _delay(self, n: int) -> float:
        base, cap = float(self._cfg.backoff_base_seconds), float(self._cfg.backoff_cap_seconds)
        return float(min(cap, base * (2 ** (n - 1))))

    def call(self, endpoint: DataEndpoint, payload: Mapping[str, Any] | None = None) -> DhanReply:
        method, url = self._url(endpoint)
        if method == "GET" and payload:
            raise EndpointNotAllowedError(f"{endpoint.value} takes no payload")
        body = None if method == "GET" else json.dumps(dict(payload or {}), sort_keys=True).encode()
        headers = {"Accept": "application/json", **self._cred.auth_headers()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = HttpRequest(method, url, headers, body)
        rate_n = server_n = attempts = 0
        while True:
            self._limiter.acquire()
            attempts += 1
            try:
                resp = self._transport.send(req, timeout_s=float(self._cfg.http_timeout_seconds))
            except TransportError as e:
                server_n += 1
                if server_n >= self._cfg.retry_server_max_attempts:
                    raise TransportError(
                        self._cred.redact(f"{endpoint.value}: gave up after {attempts} attempts: {e}")
                    ) from None
                self._limiter.backoff(self._delay(server_n))
                continue
            outcome, code, msg, parsed = classify(resp)
            msg = self._cred.redact(msg)
            where = f"{endpoint.value}: HTTP {resp.status} code={code} {msg}".strip()
            if outcome in (Outcome.OK, Outcome.NO_DATA):
                return DhanReply(
                    endpoint,
                    dict(payload or {}),
                    resp.status,
                    resp.body,
                    parsed,
                    attempts,
                    self._wall().astimezone(UTC).isoformat(),
                    outcome is Outcome.NO_DATA,
                    code,
                    msg,
                )
            if outcome is Outcome.RATE:
                rate_n += 1
                if rate_n >= self._cfg.retry_rate_limit_max_attempts:
                    raise VendorRateLimitError(
                        f"{where} (after {attempts} attempts)", http_status=resp.status, code=code
                    )
                self._limiter.backoff(max(self._delay(rate_n), _retry_after(resp) or 0.0))
                continue
            if outcome is Outcome.SERVER:
                server_n += 1
                if server_n >= self._cfg.retry_server_max_attempts:
                    raise VendorServerError(f"{where} (after {attempts} attempts)", http_status=resp.status, code=code)
                self._limiter.backoff(self._delay(server_n))
                continue
            err: type[VendorAPIError] = {
                Outcome.AUTH: VendorAuthError,
                Outcome.IP: VendorAuthError,
                Outcome.SUBSCRIPTION: VendorSubscriptionError,
                Outcome.REQUEST: VendorRequestError,
            }[outcome]
            hint = ""
            if outcome is Outcome.AUTH:
                hint = " (tokens generated on web.dhan.co are valid for 24 hours; re-export DHAN_ACCESS_TOKEN)"
            elif outcome is Outcome.IP:
                hint = " (static IP not whitelisted for this account; fix it in the Dhan web console)"
            elif outcome is Outcome.SUBSCRIPTION:
                hint = (
                    " (is the Dhan Data API subscription active? If /profile says dataPlan=Active, regenerate the "
                    "token on web.dhan.co after the subscription is active, or contact Dhan support)"
                )
            raise err(where + hint, http_status=resp.status, code=code)

    def profile(self) -> Mapping[str, Any]:
        """Read-only preflight. Returns the profile JSON; raises if the data plan is not active."""
        reply = self.call(DataEndpoint.PROFILE)
        if not isinstance(reply.parsed, dict):
            raise VendorResponseError("profile: expected a JSON object")
        plan = reply.parsed.get("dataPlan")
        if plan is not None and str(plan).lower() != "active":
            raise VendorSubscriptionError(f"profile: dataPlan={plan!r}", http_status=reply.http_status, code=None)
        return {k: v for k, v in reply.parsed.items() if k in ("tokenValidity", "dataPlan", "dataValidity")}

    def data_probe(self, today: date) -> int:
        """One tiny DATA call (NIFTY index 1-minute candles for the last few days) so a token that passes
        ``/profile`` but has no data entitlement (DH-902) fails the preflight, not the first chunk.

        Raises like ``call`` (auth/subscription/IP errors stop). Returns the number of bars (0 is fine: a holiday
        stretch). ``today`` is the IST date; the window is [today-5, today].
        """
        payload = {
            "securityId": self._cfg.nifty_index_security_id,
            "exchangeSegment": "IDX_I",
            "instrument": "INDEX",
            "interval": "1",
            "oi": False,
            "fromDate": f"{(today - timedelta(days=5)).isoformat()} 09:00:00",
            "toDate": f"{today.isoformat()} 16:00:00",
        }
        reply = self.call(DataEndpoint.INTRADAY, payload)
        if reply.no_data:
            return 0
        if not isinstance(reply.parsed, dict) or not isinstance(reply.parsed.get("timestamp"), list):
            raise VendorResponseError("data probe: expected candle arrays")
        return len(reply.parsed["timestamp"])

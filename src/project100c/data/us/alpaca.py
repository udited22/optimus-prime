"""Alpaca Market Data API v2 historical stock bars (READY, needs a free key; DATA ONLY).

Free "Basic" plan (https://docs.alpaca.markets/docs/about-market-data-api, read 3-Oct-2026): US stocks and ETFs,
history since 2016, 200 historical calls/minute, real-time only from IEX, and the consolidated (SIP) feed for
anything older than the latest 15 minutes. Endpoint: GET /v2/stocks/bars (symbols, timeframe, start, end, limit
<= 10000, adjustment, feed, page_token; https://docs.alpaca.markets/reference/stockbars). Authentication is the
``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` header pair; the key is created in the Alpaca web dashboard
(a paper-only account is enough) and exported in the shell that runs the job. It is never written or logged.

Only this one GET endpoint exists here: no account, no order, no position endpoint.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import pyarrow as pa

from project100c.data.http import HttpRequest, HttpTransport
from project100c.data.public_http import RetryPolicy
from project100c.data.ratelimit import RateLimiter
from project100c.data.us.parse import PRICE, TS, check_symbol
from project100c.errors import (
    MissingCredentialError,
    TransportError,
    VendorAuthError,
    VendorRateLimitError,
    VendorRequestError,
    VendorResponseError,
    VendorServerError,
)

ENV_KEY_ID = "ALPACA_API_KEY_ID"
ENV_SECRET_KEY = "ALPACA_API_SECRET_KEY"
BARS_PATH = "/v2/stocks/bars"
ALPACA_PARSER_VERSION = "alpaca-bars-2026-10-03.1"
_HEADER_SAFE = re.compile(r"^[\x21-\x7e]+$")
NO_KEY_HELP = (
    f"{ENV_KEY_ID} / {ENV_SECRET_KEY} are not set, so nothing was sent. To enable the Alpaca source: sign up at "
    "https://app.alpaca.markets (a free paper-trading account is enough; the Basic market-data plan is free), "
    "open 'API Keys' in the dashboard, generate a key pair, and export both variables in the shell that runs the job."
)
Timeframe = Literal["1Min", "1Day"]
Feed = Literal["sip", "iex"]


@dataclass(frozen=True, slots=True)
class AlpacaCredentials:
    _key_id: str = field(repr=False)
    _secret: str = field(repr=False)

    def __repr__(self) -> str:
        return "AlpacaCredentials(<redacted>)"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self._key_id, "APCA-API-SECRET-KEY": self._secret}

    def redact(self, text: str) -> str:
        for v in (self._key_id, self._secret):
            text = text.replace(v, "<redacted>")
        return text

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AlpacaCredentials:
        k, s = (env.get(ENV_KEY_ID) or "").strip(), (env.get(ENV_SECRET_KEY) or "").strip()
        if not k or not s:
            raise MissingCredentialError(NO_KEY_HELP)
        if not (_HEADER_SAFE.match(k) and _HEADER_SAFE.match(s)):
            raise MissingCredentialError(f"{ENV_KEY_ID}/{ENV_SECRET_KEY} contain whitespace or control characters")
        return cls(k, s)


def bars_url(
    base: str,
    symbols: Sequence[str],
    *,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    feed: Feed,
    adjustment: str = "raw",
    page_token: str | None = None,
    limit: int = 10000,
) -> str:
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("start/end must be aware and end > start")
    if adjustment not in ("raw", "split", "dividend", "all"):
        raise ValueError(f"bad adjustment {adjustment!r}")
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be 1..10000")
    q: dict[str, str] = {
        "symbols": ",".join(check_symbol(s) for s in symbols),
        "timeframe": timeframe,
        "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limit": str(limit),
        "adjustment": adjustment,
        "feed": feed,
        "sort": "asc",
    }
    if page_token:
        q["page_token"] = page_token
    return f"{base}{BARS_PATH}?{urlencode(q)}"


def _ts(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as e:
        raise VendorResponseError(f"bad bar timestamp {s!r}: {e}") from None


def parse_bars_page(body: bytes) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    try:
        doc = json.loads(body, parse_float=Decimal)
        bars = doc["bars"] or {}
        token = doc.get("next_page_token")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise VendorResponseError(f"bars page: {e}") from None
    if not isinstance(bars, dict):
        raise VendorResponseError("bars page: 'bars' is not an object")
    return bars, (str(token) if token else None)


def bars_table(symbol: str, rows: Sequence[Mapping[str, Any]], tz: ZoneInfo) -> pa.Table:
    """One symbol's bars -> the same columns as the Yahoo minute/daily parts (plus count and vwap)."""
    ts = [_ts(str(r["t"])) for r in rows]

    def d(k: str) -> list[Decimal]:
        out = []
        for r in rows:
            v = r[k]
            if isinstance(v, bool) or not isinstance(v, int | Decimal):
                raise VendorResponseError(f"{symbol}: non-numeric {k} {v!r}")
            out.append(Decimal(v))
        return out

    return pa.table(
        {
            "ts": pa.array(ts, TS),
            "session_date": pa.array([t.astimezone(tz).date() for t in ts], pa.date32()),
            "symbol": pa.array([symbol] * len(rows), pa.string()),
            "open": pa.array(d("o"), PRICE),
            "high": pa.array(d("h"), PRICE),
            "low": pa.array(d("l"), PRICE),
            "close": pa.array(d("c"), PRICE),
            "volume": pa.array([int(r["v"]) for r in rows], pa.int64()),
            "count": pa.array([int(r["n"]) for r in rows], pa.int64()),
            "vwap": pa.array(d("vw"), PRICE),
        }
    )


@dataclass(slots=True)
class BarsResult:
    tables: dict[str, pa.Table]
    pages: list[bytes]
    requests: int


class AlpacaBarsClient:
    """GET /v2/stocks/bars with pagination, pacing, bounded retries. Keys never appear in errors."""

    def __init__(
        self,
        *,
        base: str,
        transport: HttpTransport,
        limiter: RateLimiter,
        policy: RetryPolicy,
        credentials: AlpacaCredentials,
        tz: ZoneInfo,
    ) -> None:
        if urlsplit(base).scheme != "https":
            raise ValueError("https only")
        self._base = base.rstrip("/")
        self._t = transport
        self._lim = limiter
        self._p = policy
        self._c = credentials
        self._tz = tz

    def _get(self, url: str) -> bytes:
        req = HttpRequest("GET", url, {"Accept": "application/json", **self._c.headers()})
        server_n = rate_n = 0
        while True:
            self._lim.acquire()
            try:
                resp = self._t.send(req, timeout_s=self._p.timeout_s)
            except TransportError as e:
                server_n += 1
                if server_n >= self._p.server_max_attempts:
                    raise TransportError(self._c.redact(f"GET {BARS_PATH}: {e}")) from None
                self._lim.backoff(self._p.delay(server_n))
                continue
            if resp.status == 200:
                return resp.body
            snippet = self._c.redact(resp.body[:200].decode("utf-8", "replace"))
            if resp.status in (401, 403):
                raise VendorAuthError(f"Alpaca HTTP {resp.status}: {snippet}", http_status=resp.status, code=None)
            if resp.status == 429:
                rate_n += 1
                if rate_n >= self._p.rate_limit_max_attempts:
                    raise VendorRateLimitError("Alpaca HTTP 429", http_status=429, code=None)
                self._lim.backoff(self._p.delay(rate_n))
                continue
            if resp.status >= 500:
                server_n += 1
                if server_n >= self._p.server_max_attempts:
                    raise VendorServerError(f"Alpaca HTTP {resp.status}", http_status=resp.status, code=None)
                self._lim.backoff(self._p.delay(server_n))
                continue
            raise VendorRequestError(f"Alpaca HTTP {resp.status}: {snippet}", http_status=resp.status, code=None)

    def bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        feed: Feed = "sip",
        adjustment: str = "raw",
        max_pages: int = 10_000,
    ) -> BarsResult:
        rows: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        pages: list[bytes] = []
        token: str | None = None
        for _ in range(max_pages):
            url = bars_url(
                self._base, symbols, timeframe=timeframe, start=start, end=end, feed=feed, adjustment=adjustment,
                page_token=token,
            )  # fmt: skip
            body = self._get(url)
            pages.append(body)
            got, token = parse_bars_page(body)
            for sym, items in got.items():
                if sym not in rows:
                    raise VendorResponseError(f"bars page: unexpected symbol {sym!r}")
                rows[sym].extend(items)
            if token is None:
                return BarsResult({s: bars_table(s, r, self._tz) for s, r in rows.items()}, pages, len(pages))
        raise VendorResponseError(f"bars: more than {max_pages} pages")

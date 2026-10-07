"""Upstox Market Data Feed V3 client: live quotes for paper and live trading, with gap and staleness detection.

Verified against Upstox's documentation on 3-Oct-2026: ``GET /v3/feed/market-data-feed/authorize`` returns a
single-use ``wss://`` URL (``data.authorized_redirect_uri``); after connecting, a subscription
``{"guid", "method": "sub", "data": {"mode", "instrumentKeys"}}`` selects instruments, and every message is a
protobuf ``FeedResponse`` (decoded by `feed_proto`). Modes: ``ltpc``, ``option_greeks``, ``full`` (5 depth
levels), ``full_d30``. UNVERIFIED: that the subscription must be a *binary* frame (the official Python SDK sends
it that way; this client does too), the per-connection instrument limits, and whether the server sends anything
during a quiet market.

Design:

* **Non-blocking for the host loop.** `poll()` drains what has arrived (bounded) and returns the latest quote of
  every instrument that changed. A dropped connection is retried on later polls with backoff (1, 2, 5, 10, 30 s);
  each attempt authorizes again, because the URL's code is single-use.
* **Gaps are recorded, never hidden.** A disconnect opens a `FeedGap` (from the last message to the first message
  after reconnecting). Silence longer than `silence_gap` on an open connection is also a gap. The first message
  after a reconnect is the server's snapshot (``initial_feed``).
* **Staleness is explicit.** `stale_keys(now)` lists subscribed instruments with no update for `stale_after`;
  `healthy(now)` is False when disconnected or silent. Consumers (PaperBroker, DQ) refuse stale quotes.
* **Quotes are conservative.** A zero price or size in the book is "no quote on that side" (None), never zero.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from project100c.broker.upstox.credentials import UpstoxSession
from project100c.broker.upstox.feed_proto import FeedDecodeError, FeedResponse, InstrumentFeed, decode_feed_response
from project100c.broker.upstox.transport import HttpTransport, Transport
from project100c.errors import BrokerError, DataSourceError
from project100c.market_types import Quote
from project100c.netio.ws import WsClosed, WsConnection, WsError, ws_connect

AUTHORIZE_PATH = "/v3/feed/market-data-feed/authorize"
MODES = frozenset({"ltpc", "option_greeks", "full", "full_d30"})
BACKOFF_S = (1, 2, 5, 10, 30)


@dataclass
class FeedGap:
    start: datetime
    end: datetime | None
    reason: str

    @property
    def seconds(self) -> float | None:
        return None if self.end is None else (self.end - self.start).total_seconds()


def _px(v: float) -> Decimal | None:
    return Decimal(str(v)) if v > 0 else None


def _ms(v: int) -> datetime:
    return datetime.fromtimestamp(v / 1000, tz=UTC)


def to_quote(key: str, f: InstrumentFeed, current_ts: int, receive_ts: datetime) -> Quote | None:
    """A decoded feed entry -> `Quote`. Returns None if there is nothing usable."""
    lt = f.ltpc
    ts_ms = lt.ltt if lt is not None and lt.ltt > 0 else current_ts
    exch = _ms(ts_ms) if ts_ms > 0 else receive_ts
    ltp = _px(lt.ltp) if lt is not None else None
    bid = ask = None
    bq = aq = None
    if f.depth:
        top = f.depth[0]
        bid, ask = _px(top.bid_p), _px(top.ask_p)
        bq = top.bid_q if bid is not None and top.bid_q > 0 else None
        aq = top.ask_q if ask is not None and top.ask_q > 0 else None
        if bq is None:
            bid = None
        if aq is None:
            ask = None
        if bid is not None and ask is not None and bid > ask:
            bid = ask = bq = aq = None  # crossed book from the feed: refuse both sides
    if bid is None and ask is None and ltp is None:
        return None
    oi = int(f.oi) if f.oi is not None and f.oi >= 0 else None
    return Quote(key, exch, receive_ts, bid, ask, bq, aq, ltp, oi, is_option=f.kind != "index")


@dataclass
class UpstoxFeed:
    session: UpstoxSession
    clock: Callable[[], datetime]
    transport: Transport = field(default_factory=HttpTransport)
    connector: Callable[[str], WsConnection] = field(default=lambda url: ws_connect(url))
    base_url: str = "https://api.upstox.com"
    mode: str = "full"
    stale_after: timedelta = timedelta(seconds=5)
    silence_gap: timedelta = timedelta(seconds=3)
    max_messages_per_poll: int = 500
    recv_timeout_s: float = 0.05
    max_consecutive_decode_errors: int = 5
    subscribed: set[str] = field(default_factory=set)
    gaps: list[FeedGap] = field(default_factory=list)
    segment_status: dict[str, str] = field(default_factory=dict)
    latest: dict[str, Quote] = field(default_factory=dict)
    decode_errors: int = 0
    connects: int = 0
    _ws: WsConnection | None = None
    _last_msg: datetime | None = None
    _last_update: dict[str, datetime] = field(default_factory=dict)
    _attempt: int = 0
    _next_try: datetime | None = None
    _open_gap: FeedGap | None = None
    _bad_in_row: int = 0

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise DataSourceError(f"unknown feed mode {self.mode!r}")

    # -- connection ----------------------------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def subscribe(self, keys: Iterable[str]) -> None:
        new = set(keys) - self.subscribed
        self.subscribed |= new
        if new and self.connected:
            self._send_sub(sorted(new))

    def _send_sub(self, keys: list[str]) -> None:
        assert self._ws is not None
        msg = {"guid": uuid.uuid4().hex, "method": "sub", "data": {"mode": self.mode, "instrumentKeys": keys}}
        self._ws.send_binary(json.dumps(msg).encode())

    def _authorize(self) -> str:
        resp = self.transport.request(
            "GET",
            self.base_url + AUTHORIZE_PATH,
            headers={"Accept": "application/json", "Authorization": f"Bearer {self.session.access_token}"},
            timeout_s=5.0,
        )
        data = resp.body.get("data") if isinstance(resp.body, dict) else None
        url = data.get("authorized_redirect_uri") if isinstance(data, dict) else None
        if resp.status != 200 or not isinstance(url, str) or not url.startswith(("wss://", "ws://")):
            raise DataSourceError(f"feed authorize failed: HTTP {resp.status}")
        return url

    def _open_gap_now(self, now: datetime, reason: str) -> None:
        if self._open_gap is None:
            self._open_gap = FeedGap(self._last_msg or now, None, reason)
            self.gaps.append(self._open_gap)

    def _drop(self, now: datetime, reason: str) -> None:
        if self._ws is not None:
            self._ws.close()
        self._ws = None
        self._open_gap_now(now, reason)
        delay = BACKOFF_S[min(self._attempt, len(BACKOFF_S) - 1)]
        self._attempt += 1
        self._next_try = now + timedelta(seconds=delay)

    def connect(self, now: datetime) -> bool:
        if not self.session.valid_at(now):
            self._open_gap_now(now, "SESSION_EXPIRED")
            return False
        try:
            url = self._authorize()
            self._ws = self.connector(url)
            self.connects += 1
            if self.subscribed:
                self._send_sub(sorted(self.subscribed))
        except (WsError, BrokerError, DataSourceError) as e:
            self._drop(now, f"CONNECT_FAILED {type(e).__name__}")
            return False
        self._bad_in_row = 0
        return True

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
        self._ws = None

    # -- data ------------------------------------------------------------------------------------------------
    def _on_message(self, r: FeedResponse, now: datetime) -> dict[str, Quote]:
        if self._open_gap is not None:
            self._open_gap.end = now
            self._open_gap = None
            self._attempt = 0
        elif self._last_msg is not None and now - self._last_msg > self.silence_gap:
            self.gaps.append(FeedGap(self._last_msg, now, "SILENT"))
        self._last_msg = now
        self.segment_status.update(r.segment_status)
        out: dict[str, Quote] = {}
        for key, f in r.feeds.items():
            q = to_quote(key, f, r.current_ts, now)
            if q is not None:
                out[key] = q
                self.latest[key] = q
                self._last_update[key] = now
        return out

    def poll(self) -> dict[str, Quote]:
        now = self.clock()
        if not self.connected:
            if self._next_try is None or now >= self._next_try:
                self.connect(now)
            if not self.connected:
                return {}
        assert self._ws is not None
        batch: dict[str, Quote] = {}
        for _ in range(self.max_messages_per_poll):
            try:
                raw = self._ws.recv(self.recv_timeout_s)
            except WsClosed:
                self._drop(now, "DISCONNECTED")
                break
            except WsError:
                self._drop(now, "PROTOCOL_ERROR")
                break
            if raw is None:
                break
            try:
                r = decode_feed_response(raw)
            except FeedDecodeError:
                self.decode_errors += 1
                self._bad_in_row += 1
                if self._bad_in_row >= self.max_consecutive_decode_errors:
                    self._drop(now, "DECODE_ERRORS")
                    break
                continue
            self._bad_in_row = 0
            batch.update(self._on_message(r, self.clock()))
        return batch

    def stale_keys(self, now: datetime) -> set[str]:
        return {
            k
            for k in self.subscribed
            if now - self._last_update.get(k, datetime.min.replace(tzinfo=UTC)) > self.stale_after
        }

    def healthy(self, now: datetime) -> bool:
        return self.connected and self._last_msg is not None and now - self._last_msg <= self.stale_after

"""Upstox REST adapter: the `BrokerAdapter` protocol over Upstox's order, portfolio and funds APIs (K-04).

Venue specifics live here and nowhere else (OD-017 rule 4). The core never imports this module; the composition
root hands an `UpstoxBroker` to the `ExecutionGateway`, which adds idempotency, throttling and error counting.

Verified against Upstox's public documentation on 3-Oct-2026: order placement v3 (``POST /v3/order/place`` on the
HFT host, tag at most 40 characters), the order book (``GET /v2/order/retrieve-all``, field names and the
``complete``/``rejected``/``cancelled``/``open``/``trigger pending`` statuses) and Exit-All
(``POST /v2/order/positions/exit``, MARKET orders with market price protection, at most 10 positions, HTTP 207 on
partial success, UDAPI1111 when there is nothing to exit). UNVERIFIED (from memory, to be confirmed in the sandbox
before go-live): modify and cancel v3, the trades, positions and funds endpoints and their field names, the
intermediate order statuses, and every error code other than those named above.

What it will not do: MARKET, SL-M or IOC orders (unrepresentable in `OrderRequest`), delivery product, AMO, or
auto-slicing. It refuses to send outside the order window before any network I/O, and it never retries a place:
a timeout or 5xx means the order's fate is UNKNOWN and the gateway resolves it from the order book.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from project100c.broker.types import (
    BrokerOrder,
    BrokerPosition,
    ExitAllResult,
    Fill,
    OrderEvent,
    OrderEventKind,
    OrderRequest,
    OrderStatus,
    OrderType,
)
from project100c.broker.upstox.credentials import UpstoxSession
from project100c.broker.upstox.transport import HttpResponse, HttpTransport, Transport
from project100c.core_types import OrderSide
from project100c.errors import BrokerDisconnectedError, BrokerError, BrokerRejectError, BrokerTimeoutError
from project100c.execution.ids import broker_tag
from project100c.sessions import IST

EXIT_ALL_TAG = "EXIT_ALL"
FOREIGN_TAG = "FOREIGN"
MAX_TAG_LEN = 40  # UDAPI1119
# Errors that mean the session or the host is unusable until a human acts: treated as a disconnect so the
# broker-error kill rules fire, never retried.
DISCONNECT_CODES = frozenset({"UDAPI1154", "UDAPI100050", "UDAPI100069"})  # static IP; invalid token (UNVERIFIED)
NO_POSITIONS = "UDAPI1111"
_STATUS = {
    "complete": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.CANCELLED,
    "trigger pending": OrderStatus.TRIGGER_PENDING,
    "open": OrderStatus.OPEN,
}
_ACKED_PENDING = frozenset({"modify pending", "cancel pending", "modify validation pending", "modified"})


@dataclass(frozen=True, slots=True)
class UpstoxConfig:
    base_url: str = "https://api.upstox.com"
    hft_base_url: str = "https://api-hft.upstox.com"
    product: str = "I"  # intraday only (mandate: flat by 15:15)
    exit_all_segment: str = "NSE_FO"
    timeout_s: float = 3.0
    algo_name: str | None = None  # optional X-Algo-Name header

    def __post_init__(self) -> None:
        if self.product != "I":
            raise BrokerError("only the intraday product is allowed (mandate: no overnight positions)")
        if self.timeout_s <= 0 or self.timeout_s > 10:
            raise BrokerError("timeout_s must be in (0, 10]")


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _ts(v: Any, fallback: datetime) -> datetime:
    if not isinstance(v, str) or not v:
        return fallback
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        return fallback


def map_status(raw: str, filled: int, exchange_order_id: str) -> OrderStatus:
    """Upstox order status -> `OrderStatus`. Unknown or in-flight statuses map to a non-terminal state, so the
    gateway keeps watching rather than assuming an outcome."""
    s = raw.strip().lower()
    st = _STATUS.get(s)
    if st is OrderStatus.OPEN or s in _ACKED_PENDING:
        return OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.OPEN
    if st is not None:
        return st
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    return OrderStatus.OPEN if exchange_order_id else OrderStatus.PENDING_ACK


class UpstoxBroker:
    def __init__(
        self,
        session: UpstoxSession,
        *,
        clock: Callable[[], datetime],
        transport: Transport | None = None,
        config: UpstoxConfig | None = None,
        order_window: Callable[[datetime], bool] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._t: Transport = transport or HttpTransport()
        self.config = config or UpstoxConfig()
        self._window = order_window
        self._connected = True
        self._req_by_cid: dict[str, OrderRequest] = {}
        self._cid_by_tag: dict[str, str] = {}
        self._cid_by_oid: dict[str, str] = {}
        self._mods: dict[str, int] = {}
        self._exit_all_oids: set[str] = set()
        self._last: dict[str, BrokerOrder] = {}
        self._seen_trades: set[str] = set()
        self.unknown_statuses: set[str] = set()

    # -- plumbing -------------------------------------------------------------------------------------------
    def remember(self, requests: list[OrderRequest]) -> None:
        """Re-register today's requests from the journal after a restart (the broker tag is a one-way hash)."""
        for r in requests:
            self._req_by_cid[r.client_order_id] = r
            self._cid_by_tag[broker_tag(r.client_order_id)] = r.client_order_id

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Authorization": f"Bearer {self._session.access_token}"}
        if self.config.algo_name:
            h["X-Algo-Name"] = self.config.algo_name
        return h

    def _call(
        self,
        method: str,
        path: str,
        *,
        hft: bool = False,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        fate_unknown_on_5xx: bool = False,
    ) -> HttpResponse:
        now = self._clock()
        if not self._session.valid_at(now):
            self._connected = False
            raise BrokerDisconnectedError("SESSION_EXPIRED: today's Upstox token is not valid (approve a new one)")
        base = self.config.hft_base_url if hft else self.config.base_url
        try:
            resp = self._t.request(
                method,
                base + path,
                headers=self._headers(),
                params=params,
                json_body=body,
                timeout_s=self.config.timeout_s,
            )
        except BrokerDisconnectedError:
            self._connected = False
            raise
        self._raise_for(resp, fate_unknown_on_5xx)
        self._connected = True
        return resp

    def _raise_for(self, resp: HttpResponse, fate_unknown: bool) -> None:
        b = resp.body
        raw_errs = b.get("errors")
        errs: list[Any] = raw_errs if isinstance(raw_errs, list) else []
        codes = [str(e.get("errorCode") or e.get("error_code") or "") for e in errs if isinstance(e, dict)]
        msg = "; ".join(f"{c}: {e.get('message', '')}" for c, e in zip(codes, errs, strict=False)) or str(resp.status)
        if resp.status in (200, 207) and b.get("status") in ("success", "partial_success"):
            return
        if resp.status == 207:
            return  # partial: the caller reads errors[]
        if resp.status == 401 or DISCONNECT_CODES.intersection(codes):
            self._connected = False
            raise BrokerDisconnectedError(f"AUTH/HOST {msg}")
        if resp.status == 429:
            raise BrokerRejectError(f"RATE_LIMITED {msg}")
        if resp.status >= 500:
            if fate_unknown:
                raise BrokerTimeoutError(f"HTTP {resp.status}: order fate unknown, reconcile before retrying")
            raise BrokerTimeoutError(f"HTTP {resp.status}")
        if NO_POSITIONS in codes:
            return  # only Exit-All sends this; it means already flat
        raise BrokerRejectError(msg)

    # -- BrokerAdapter --------------------------------------------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected and self._session.valid_at(self._clock())

    def place(self, req: OrderRequest) -> str:
        now = self._clock()
        if self._window is not None and not self._window(now):
            raise BrokerRejectError("OUTSIDE_ORDER_WINDOW (refused before sending)")
        tag = broker_tag(req.client_order_id)
        if len(tag) > MAX_TAG_LEN:
            raise BrokerError(f"broker tag longer than {MAX_TAG_LEN} characters")
        self.remember([req])
        body = {
            "quantity": req.qty,
            "product": self.config.product,
            "validity": "DAY",
            "price": float(req.price),
            "tag": tag,
            "instrument_token": req.instrument_key,
            "order_type": str(req.order_type),
            "transaction_type": str(req.side),
            "disclosed_quantity": 0,
            "trigger_price": float(req.trigger_price) if req.trigger_price is not None else 0,
            "is_amo": False,
            "slice": False,
        }
        resp = self._call("POST", "/v3/order/place", hft=True, body=body, fate_unknown_on_5xx=True)
        data = resp.body.get("data") or {}
        ids = data.get("order_ids") if isinstance(data, dict) else None
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
            raise BrokerTimeoutError(f"place returned no single order id ({ids!r}): fate unknown")
        self._cid_by_oid[ids[0]] = req.client_order_id
        return ids[0]

    def _request_for(self, broker_order_id: str) -> OrderRequest:
        cid = self._cid_by_oid.get(broker_order_id)
        if cid is None or cid not in self._req_by_cid:
            self.orders()
            cid = self._cid_by_oid.get(broker_order_id)
        if cid is None or cid not in self._req_by_cid:
            raise BrokerRejectError(f"UNKNOWN_ORDER {broker_order_id}: not placed by this system")
        return self._req_by_cid[cid]

    def modify(self, broker_order_id: str, *, price: Decimal, trigger_price: Decimal | None = None) -> None:
        req = self._request_for(broker_order_id)
        body = {
            "order_id": broker_order_id,
            "quantity": req.qty,
            "validity": "DAY",
            "price": float(price),
            "order_type": str(req.order_type),
            "disclosed_quantity": 0,
            "trigger_price": float(trigger_price) if trigger_price is not None else 0,
        }
        self._call("PUT", "/v3/order/modify", hft=True, body=body)
        self._mods[broker_order_id] = self._mods.get(broker_order_id, 0) + 1

    def cancel(self, broker_order_id: str) -> None:
        self._call("DELETE", "/v3/order/cancel", hft=True, params={"order_id": broker_order_id})

    def _row_request(self, row: Mapping[str, Any], cid: str, tag: str) -> OrderRequest:
        side = OrderSide.BUY if str(row.get("transaction_type", "")).upper() == "BUY" else OrderSide.SELL
        qty = int(row.get("quantity") or 0) or 1
        price = _dec(row.get("price"))
        if price <= 0:
            price = _dec(row.get("average_price"))
        if price <= 0:
            price = Decimal("0.05")
        trig = _dec(row.get("trigger_price"))
        is_sl = str(row.get("order_type", "")).upper() in ("SL", "SL-M") and trig > 0
        key = str(row.get("instrument_token", ""))
        try:
            if is_sl:
                return OrderRequest(cid, key, side, qty, OrderType.SL, price, trig, tag=tag)
        except BrokerError:
            pass
        return OrderRequest(cid, key, side, qty, OrderType.LIMIT, price, tag=tag)

    def _map_order(self, row: Mapping[str, Any], now: datetime) -> BrokerOrder:
        oid = str(row.get("order_id", ""))
        btag = str(row.get("tag") or "")
        cid = self._cid_by_tag.get(btag) or self._cid_by_oid.get(oid)
        req: OrderRequest | None = self._req_by_cid.get(cid) if cid else None
        if req is None:
            if btag == EXIT_ALL_TAG or oid in self._exit_all_oids:
                req = self._row_request(row, f"EXIT-ALL-{oid}", EXIT_ALL_TAG)
            else:
                req = self._row_request(row, f"FOREIGN-{oid}", FOREIGN_TAG)
        self._cid_by_oid[oid] = req.client_order_id
        filled = int(row.get("filled_quantity") or 0)
        raw = str(row.get("status", ""))
        if raw.strip().lower() not in _STATUS and raw.strip().lower() not in _ACKED_PENDING:
            self.unknown_statuses.add(raw)
        status = map_status(raw, filled, str(row.get("exchange_order_id") or ""))
        avg = _dec(row.get("average_price"))
        trig = _dec(row.get("trigger_price"))
        return BrokerOrder(
            broker_order_id=oid,
            request=req,
            status=status,
            filled_qty=filled,
            avg_fill_price=avg if filled > 0 and avg > 0 else None,
            reject_reason=(str(row.get("status_message") or "") or None) if status is OrderStatus.REJECTED else None,
            updated=_ts(row.get("exchange_timestamp") or row.get("order_timestamp"), now),
            price=_dec(row.get("price")) if _dec(row.get("price")) > 0 else req.price,
            trigger_price=trig if trig > 0 else None,
            modifications=self._mods.get(oid, 0),
        )

    def orders(self) -> list[BrokerOrder]:
        now = self._clock()
        rows = self._call("GET", "/v2/order/retrieve-all").body.get("data") or []
        return [self._map_order(r, now) for r in rows if isinstance(r, dict)]

    def trades(self) -> list[Fill]:
        now = self._clock()
        rows = self._call("GET", "/v2/order/trades/get-trades-for-day").body.get("data") or []
        out: list[Fill] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            oid = str(r.get("order_id", ""))
            if oid not in self._cid_by_oid:
                self.orders()
            cid = self._cid_by_oid.get(oid, f"FOREIGN-{oid}")
            out.append(
                Fill(
                    trade_id=str(r.get("trade_id", "")),
                    broker_order_id=oid,
                    client_order_id=cid,
                    instrument_key=str(r.get("instrument_token", "")),
                    side=OrderSide.BUY if str(r.get("transaction_type", "")).upper() == "BUY" else OrderSide.SELL,
                    qty=int(r.get("quantity") or 0),
                    price=_dec(r.get("average_price")),
                    ts=_ts(r.get("exchange_timestamp") or r.get("order_timestamp"), now),
                )
            )
        return out

    def positions(self) -> list[BrokerPosition]:
        rows = self._call("GET", "/v2/portfolio/short-term-positions").body.get("data") or []
        out: list[BrokerPosition] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append(
                BrokerPosition(
                    instrument_key=str(r.get("instrument_token", "")),
                    net_qty=int(r.get("quantity") or 0),
                    buy_qty=int(r.get("day_buy_quantity") or 0),
                    sell_qty=int(r.get("day_sell_quantity") or 0),
                    buy_value=_dec(r.get("day_buy_value")),
                    sell_value=_dec(r.get("day_sell_value")),
                )
            )
        return out

    def funds(self) -> Decimal:
        data = self._call("GET", "/v2/user/get-funds-and-margin", params={"segment": "SEC"}).body.get("data")
        eq = data.get("equity") if isinstance(data, dict) else None
        if not isinstance(eq, dict) or "available_margin" not in eq:
            raise BrokerError("funds response has no equity.available_margin (UNVERIFIED field name)")
        return _dec(eq["available_margin"])

    def poll_events(self) -> list[OrderEvent]:
        """Events derived by diffing the order book and trade book against the previous poll."""
        now = self._clock()
        cur = {o.broker_order_id: o for o in self.orders()}
        ev: list[OrderEvent] = []
        for oid, o in cur.items():
            prev = self._last.get(oid)
            cid = o.request.client_order_id
            if prev is None or prev.status is OrderStatus.PENDING_ACK:
                if o.status is not OrderStatus.PENDING_ACK and o.status is not OrderStatus.REJECTED:
                    ev.append(OrderEvent(OrderEventKind.ACK, oid, cid, now))
            if (
                prev is not None
                and prev.status is OrderStatus.TRIGGER_PENDING
                and o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED)
            ):
                ev.append(OrderEvent(OrderEventKind.TRIGGERED, oid, cid, now))
            if prev is not None and (prev.price != o.price or prev.trigger_price != o.trigger_price):
                ev.append(OrderEvent(OrderEventKind.MODIFIED, oid, cid, now))
            changed = prev is None or prev.status is not o.status
            if changed and o.status is OrderStatus.REJECTED:
                ev.append(OrderEvent(OrderEventKind.REJECT, oid, cid, now, reason=o.reject_reason))
            if changed and o.status is OrderStatus.CANCELLED:
                ev.append(OrderEvent(OrderEventKind.CANCELLED, oid, cid, now))
        for f in self.trades():
            if f.trade_id not in self._seen_trades:
                self._seen_trades.add(f.trade_id)
                ev.append(OrderEvent(OrderEventKind.FILL, f.broker_order_id, f.client_order_id, f.ts, fill=f))
        self._last = cur
        return ev

    def exit_all(self) -> ExitAllResult:
        """Cancel every open order first (an exited position must not leave a live SELL SL behind, which could
        open a short), then ask the broker to exit all ``NSE_FO`` positions. Upstox sends MARKET orders with
        market price protection, so `pricing_verified` stays False until checked in the sandbox."""
        now = self._clock()
        cancelled: list[str] = []
        failed: set[str] = set()
        for o in self.orders():
            if o.status in (
                OrderStatus.OPEN,
                OrderStatus.TRIGGER_PENDING,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.PENDING_ACK,
            ):
                try:
                    self.cancel(o.broker_order_id)
                    cancelled.append(o.broker_order_id)
                except BrokerError:
                    failed.add(o.request.instrument_key)
        resp = self._call("POST", "/v2/order/positions/exit", params={"segment": self.config.exit_all_segment})
        data = resp.body.get("data") or {}
        ids = [str(i) for i in (data.get("order_ids") or [])] if isinstance(data, dict) else []
        self._exit_all_oids.update(ids)
        for e in resp.body.get("errors") or []:
            if isinstance(e, dict) and str(e.get("errorCode") or e.get("error_code") or "") != "UDAPI1111":
                failed.add(str(e.get("instrument_key") or e.get("instrumentKey") or "UNKNOWN"))
        return ExitAllResult(now, tuple(ids), tuple(cancelled), tuple(sorted(failed)), pricing_verified=False)

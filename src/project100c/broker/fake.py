"""Deterministic, scriptable fake broker (backlog K-08).

Simulates an exchange-backed broker for one or more instruments: acks (optionally delayed), limit and
stop-limit matching against a settable top of book (fills can be partial when the book is shallow), funds
checks (upfront premium), rejects, disconnects, lost acks (timeouts), delayed fills, dropped order-update
events and phantom positions. No randomness, no wall clock: everything is driven by an injected clock and by
explicit set_quote()/tick() calls, so failure-injection tests are reproducible.

The fake keeps matching while "disconnected" (the exchange keeps working when our link drops); only the API
calls fail. This is what makes disconnect scenarios dangerous and worth testing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from project100c.broker.types import (
    TERMINAL,
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
from project100c.core_types import OrderSide
from project100c.errors import BrokerDisconnectedError, BrokerError, BrokerRejectError, BrokerTimeoutError


@dataclass(frozen=True, slots=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int
    ask_qty: int


@dataclass
class FaultPlan:
    """Scripted faults. Mutate between steps to inject failures at precise moments."""

    reject_next_places: list[str] = field(default_factory=list)  # reasons, consumed FIFO
    reject_next_modifies: list[str] = field(default_factory=list)
    reject_next_cancels: list[str] = field(default_factory=list)
    timeout_next_places: list[bool] = field(default_factory=list)  # True = order accepted but ack lost
    disconnected_windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    ack_delay: timedelta = timedelta(0)
    fill_delay: timedelta = timedelta(0)  # an order cannot fill until ack + fill_delay
    max_fill_qty_per_match: int | None = None  # force partial fills
    drop_events: bool = False  # order-update stream silently loses events (polling stays authoritative)
    # every fill is reported twice (duplicate fill messages, docs/architecture/backtesting.md §8.4)
    duplicate_fills: bool = False
    reverse_events: bool = False  # order updates delivered out of order
    max_orders_per_second: int = 10  # Upstox non-registered algo limit (S48)
    exit_all_error: BrokerError | None = None  # Exit-All call itself fails (raised, nothing done)
    exit_all_skip: set[str] = field(default_factory=set)  # instruments the broker fails to exit (partial Exit-All)


@dataclass
class _Order:
    oid: str
    req: OrderRequest
    status: OrderStatus
    placed: datetime
    acked_at: datetime
    price: Decimal
    trigger: Decimal | None
    triggered: bool = False
    filled: int = 0
    value: Decimal = Decimal(0)
    reject_reason: str | None = None
    updated: datetime | None = None
    modifications: int = 0


class FakeBroker:
    paper_venue = True  # simulated: no real money, so a paper-venue Governor may route PAPER intents here

    def __init__(self, clock: Callable[[], datetime], *, funds: Decimal, faults: FaultPlan | None = None) -> None:
        if not isinstance(funds, Decimal) or funds < 0:
            raise BrokerError("funds must be a non-negative Decimal")
        self._clock = clock
        self._cash = funds
        self.faults = faults if faults is not None else FaultPlan()
        self._quotes: dict[str, Quote] = {}
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._fills: list[Fill] = []
        self._events: list[OrderEvent] = []
        self._phantom: dict[str, int] = {}
        self._seq = 0
        self._trade_seq = 0
        self._recent_places: list[datetime] = []
        self.session_valid = True

    # ---------------- connectivity ----------------
    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise BrokerError("fake broker clock returned a naive datetime")
        return now

    def is_connected(self) -> bool:
        now = self._now()
        return self.session_valid and not any(a <= now < b for a, b in self.faults.disconnected_windows)

    def _require_link(self) -> None:
        if not self.is_connected():
            raise BrokerDisconnectedError("fake broker: link down / session invalid (request NOT sent)")

    # ---------------- market simulation ----------------
    def set_quote(
        self, instrument_key: str, bid: Decimal | None, ask: Decimal | None, *, bid_qty: int = 6500, ask_qty: int = 6500
    ) -> None:
        self._quotes[instrument_key] = Quote(bid, ask, bid_qty, ask_qty)
        self.tick()

    def add_phantom_position(self, instrument_key: str, qty: int) -> None:
        """A position the broker reports that our journal never saw (reconciliation-kill tests)."""
        self._phantom[instrument_key] = self._phantom.get(instrument_key, 0) + qty

    def tick(self) -> None:
        """Advance acks, triggers and matching to the current clock time."""
        now = self._now()
        for o in self._orders.values():
            if o.status is OrderStatus.PENDING_ACK and now >= o.acked_at:
                o.status = OrderStatus.TRIGGER_PENDING if o.req.order_type is OrderType.SL else OrderStatus.OPEN
                o.updated = now
                self._emit(OrderEventKind.ACK, o, now)
            if o.status in TERMINAL or o.status is OrderStatus.PENDING_ACK:
                continue
            q = self._quotes.get(o.req.instrument_key)
            if q is None:
                continue
            if o.req.order_type is OrderType.SL and not o.triggered:
                hit = (
                    o.req.side is OrderSide.SELL and q.bid is not None and o.trigger is not None and q.bid <= o.trigger
                ) or (
                    o.req.side is OrderSide.BUY and q.ask is not None and o.trigger is not None and q.ask >= o.trigger
                )
                if not hit:
                    continue
                o.triggered = True
                o.status = OrderStatus.OPEN if o.filled == 0 else OrderStatus.PARTIALLY_FILLED
                o.updated = now
                self._emit(OrderEventKind.TRIGGERED, o, now)
            if now < o.acked_at + self.faults.fill_delay:
                continue
            self._match(o, q, now)

    def _match(self, o: _Order, q: Quote, now: datetime) -> None:
        remaining = o.req.qty - o.filled
        if o.req.side is OrderSide.BUY:
            if q.ask is None or q.ask > o.price or q.ask_qty <= 0:
                return
            px, avail = q.ask, q.ask_qty
        else:
            if q.bid is None or q.bid < o.price or q.bid_qty <= 0:
                return
            px, avail = q.bid, q.bid_qty
        qty = min(remaining, avail)
        if self.faults.max_fill_qty_per_match is not None:
            qty = min(qty, self.faults.max_fill_qty_per_match)
        if qty <= 0:
            return
        if o.req.side is OrderSide.BUY:
            self._cash -= px * qty
        else:
            self._cash += px * qty
        o.filled += qty
        o.value += px * qty
        o.status = OrderStatus.FILLED if o.filled == o.req.qty else OrderStatus.PARTIALLY_FILLED
        o.updated = now
        self._trade_seq += 1
        f = Fill(
            f"FT-{self._trade_seq:06d}", o.oid, o.req.client_order_id, o.req.instrument_key, o.req.side, qty, px, now
        )
        self._fills.append(f)
        self._emit(OrderEventKind.FILL, o, now, fill=f)

    def _emit(
        self, kind: OrderEventKind, o: _Order, now: datetime, *, fill: Fill | None = None, reason: str | None = None
    ) -> None:
        if self.faults.drop_events:
            return
        self._events.append(OrderEvent(kind, o.oid, o.req.client_order_id, now, fill, reason))

    # ---------------- API ----------------
    def place(self, req: OrderRequest) -> str:
        self._require_link()
        now = self._now()
        if req.client_order_id in self._by_client:  # idempotent retry: never a duplicate order
            return self._by_client[req.client_order_id]
        self._recent_places = [t for t in self._recent_places if now - t < timedelta(seconds=1)]
        if len(self._recent_places) >= self.faults.max_orders_per_second:
            raise BrokerRejectError("RATE_LIMIT: orders per second exceeded")
        self._recent_places.append(now)
        timeout_accepted: bool | None = None
        if self.faults.timeout_next_places:
            timeout_accepted = self.faults.timeout_next_places.pop(0)
            if not timeout_accepted:
                raise BrokerTimeoutError("place timed out (order NOT accepted by the fake broker)")
        reason: str | None = None
        if self.faults.reject_next_places:
            reason = self.faults.reject_next_places.pop(0)
        elif req.instrument_key not in self._quotes:
            reason = "UNKNOWN_INSTRUMENT"
        elif req.side is OrderSide.BUY and req.price * req.qty > self._cash - self._committed_buy_value():
            reason = "RMS_INSUFFICIENT_FUNDS"
        self._seq += 1
        oid = f"FB-{self._seq:06d}"
        o = _Order(
            oid,
            req,
            OrderStatus.PENDING_ACK,
            now,
            now + self.faults.ack_delay,
            req.price,
            req.trigger_price,
            updated=now,
        )
        self._orders[oid] = o
        self._by_client[req.client_order_id] = oid
        if reason is not None:
            o.status, o.reject_reason = OrderStatus.REJECTED, reason
            self._emit(OrderEventKind.REJECT, o, now, reason=reason)
            if timeout_accepted is None:
                raise BrokerRejectError(reason)
        self.tick()
        if timeout_accepted:
            raise BrokerTimeoutError("place timed out (order WAS accepted; ack lost)")
        return oid

    def _committed_buy_value(self) -> Decimal:
        return sum(
            (
                o.price * (o.req.qty - o.filled)
                for o in self._orders.values()
                if o.req.side is OrderSide.BUY and o.status not in TERMINAL
            ),
            Decimal(0),
        )

    def _live(self, broker_order_id: str) -> _Order:
        o = self._orders.get(broker_order_id)
        if o is None:
            raise BrokerRejectError(f"UNKNOWN_ORDER {broker_order_id}")
        if o.status in TERMINAL:
            raise BrokerRejectError(f"ORDER_NOT_OPEN {broker_order_id} ({o.status})")
        return o

    def modify(self, broker_order_id: str, *, price: Decimal, trigger_price: Decimal | None = None) -> None:
        self._require_link()
        if self.faults.reject_next_modifies:
            raise BrokerRejectError(self.faults.reject_next_modifies.pop(0))
        o = self._live(broker_order_id)
        new_req = dataclasses.replace(
            o.req, price=price, trigger_price=trigger_price if o.req.order_type is OrderType.SL else None
        )
        o.price, o.trigger, o.req = new_req.price, new_req.trigger_price, new_req  # re-validated by __post_init__
        o.modifications += 1
        now = self._now()
        o.updated = now
        self._emit(OrderEventKind.MODIFIED, o, now)
        self.tick()

    def cancel(self, broker_order_id: str) -> None:
        self._require_link()
        if self.faults.reject_next_cancels:
            raise BrokerRejectError(self.faults.reject_next_cancels.pop(0))
        o = self._live(broker_order_id)
        now = self._now()
        o.status, o.updated = OrderStatus.CANCELLED, now
        self._emit(OrderEventKind.CANCELLED, o, now)

    def orders(self) -> list[BrokerOrder]:
        self._require_link()
        return [
            BrokerOrder(
                o.oid,
                o.req,
                o.status,
                o.filled,
                (o.value / o.filled) if o.filled else None,
                o.reject_reason,
                o.updated or o.placed,
                o.price,
                o.trigger,
                o.modifications,
            )
            for o in self._orders.values()
        ]

    def trades(self) -> list[Fill]:
        self._require_link()
        if self.faults.duplicate_fills:
            return [f for f in self._fills for _ in (0, 1)]
        return list(self._fills)

    def positions(self) -> list[BrokerPosition]:
        self._require_link()
        agg: dict[str, list[Decimal | int]] = {}
        for f in self._fills:
            a = agg.setdefault(f.instrument_key, [0, 0, Decimal(0), Decimal(0)])
            if f.side is OrderSide.BUY:
                a[0] = int(a[0]) + f.qty
                a[2] = Decimal(a[2]) + f.price * f.qty
            else:
                a[1] = int(a[1]) + f.qty
                a[3] = Decimal(a[3]) + f.price * f.qty
        for k, q in self._phantom.items():
            a = agg.setdefault(k, [0, 0, Decimal(0), Decimal(0)])
            a[0] = int(a[0]) + q
        return [
            BrokerPosition(k, int(a[0]) - int(a[1]), int(a[0]), int(a[1]), Decimal(a[2]), Decimal(a[3]))
            for k, a in sorted(agg.items())
        ]

    def funds(self) -> Decimal:
        self._require_link()
        return self._cash

    def exit_all(self) -> ExitAllResult:
        """Simulated broker Exit-All (OD-007): cancel open orders, then close every net long at the bid.

        Real Upstox pricing / order type is UNVERIFIED; the fake models a marketable exit at the touch, which can
        fail (no bid) or be skipped by fault injection. Never 'succeeds' silently: failures are listed.
        """
        self._require_link()
        now = self._now()
        if self.faults.exit_all_error is not None:
            raise self.faults.exit_all_error
        cancelled: list[str] = []
        for o in self._orders.values():
            if o.status not in TERMINAL:
                o.status, o.updated = OrderStatus.CANCELLED, now
                cancelled.append(o.oid)
                self._emit(OrderEventKind.CANCELLED, o, now, reason="EXIT_ALL")
        exits: list[str] = []
        failed: list[str] = []
        for p in self.positions():
            if p.net_qty <= 0:
                continue
            q = self._quotes.get(p.instrument_key)
            if p.instrument_key in self.faults.exit_all_skip or q is None or q.bid is None or q.bid <= 0:
                failed.append(p.instrument_key)
                continue
            self._seq += 1
            oid = f"FB-{self._seq:06d}"
            req = OrderRequest(
                f"EXIT-ALL-{oid}", p.instrument_key, OrderSide.SELL, p.net_qty, OrderType.LIMIT, q.bid, tag="EXIT_ALL"
            )
            o = _Order(oid, req, OrderStatus.OPEN, now, now, q.bid, None, updated=now)
            self._orders[oid] = o
            self._by_client[req.client_order_id] = oid
            self._emit(OrderEventKind.ACK, o, now)
            self._match(o, q, now)
            if o.status is not OrderStatus.FILLED:
                failed.append(p.instrument_key)
            exits.append(oid)
        return ExitAllResult(now, tuple(exits), tuple(cancelled), tuple(failed))

    def poll_events(self) -> list[OrderEvent]:
        self._require_link()
        out, self._events = self._events, []
        if self.faults.duplicate_fills:
            out = [e for e in out for _ in ((0, 1) if e.kind is OrderEventKind.FILL else (0,))]
        return out[::-1] if self.faults.reverse_events else out

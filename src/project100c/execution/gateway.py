"""The execution gateway: the only object that talks to a broker adapter (docs/architecture/execution-engine.md §10.6,
K-07).

``ExecutionGateway`` wraps any ``BrokerAdapter`` and is itself a ``BrokerAdapter``, so the kernel runtime runs the
same code path against a real broker, the paper simulator or the fake broker. It adds, independently of the
runtime's own checks:

* **idempotency** -- a client order ID is sent at most once. A repeat ``place`` returns the broker order ID it
  already has (no resend); after a timeout it first looks the ID up in the broker's order book; a rejected or
  unresolvable ID can never be reused (a new attempt needs a new ID, ``execution.ids``);
* **long only (OD-006)** -- a SELL is refused unless the broker's own position covers it after every working sell
  (a second, broker-truth check behind the Governor's mandate and the runtime's closable-quantity check);
* **rate limit** -- a token bucket, 2 orders/s sustained and a burst of 5 (docs/architecture/execution-engine.md §10.5;
well below the
  exchange's 10/s), refused locally rather than at the broker; at most 20 modifications per order;
* **UNKNOWN blocks the instrument** -- while an order's fate is unknown after a timeout, no new order for that
  instrument is sent (docs/architecture/execution-engine.md §10.4);
* **order state** -- one ``OrderFSM`` per client order ID, advanced by sends, acks, fills and the order book;
* **error accounting** -- order-path and read-path errors in a row, and all errors in a window (OD-017);
* **Exit-All** -- at most one successful broker Exit-All per trading day (OD-007);
* **RiskTicket check (K-07)** -- with a ``ticket_signer`` (the Governor's), an ENTRY order is sent only with an
  attached ticket that is correctly signed, unexpired at the gateway clock, single-use and for that instrument,
  side, quantity and no higher a price; otherwise ``TicketRejectedError`` (the runtime latches SYSTEM_INTEGRITY).
  Protective stops and exits are risk-reducing and need no ticket.

No credentials live here: an adapter is constructed (with its own credentials) outside the core and passed in.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from project100c.broker.types import (
    TERMINAL,
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    ExitAllResult,
    Fill,
    OrderEvent,
    OrderRequest,
    OrderStatus,
)
from project100c.core_types import OrderSide
from project100c.errors import (
    BrokerDisconnectedError,
    BrokerError,
    BrokerRejectError,
    BrokerTimeoutError,
    TicketRejectedError,
)
from project100c.execution.order_fsm import OrderEvt, OrderFSM, OrderState
from project100c.kernel.state import OrderKind
from project100c.kernel.tickets import TicketSigner, ticket_mismatch
from project100c.sessions import IST

if TYPE_CHECKING:
    from project100c.kernel.governor import RiskTicket

ENTRY_TAG = str(OrderKind.ENTRY)


class ExecutionGateway:
    def __init__(
        self,
        broker: BrokerAdapter,
        clock: Callable[[], datetime],
        *,
        rate_per_s: int = 2,
        burst: int = 5,
        max_modifications: int = 20,
        error_window: timedelta = timedelta(minutes=15),
        ticket_signer: TicketSigner | None = None,
    ) -> None:
        if not 1 <= rate_per_s <= burst < 10:
            raise ValueError("need 1 <= rate_per_s <= burst < 10 (the exchange limit is 10 orders/s)")
        self._b = broker
        self._clock = clock
        self._rate, self._burst = rate_per_s, burst
        self._tokens = float(burst)
        self._refilled: datetime | None = None
        self._max_mods = max_modifications
        self._mods: dict[str, int] = {}
        self._window = error_window
        self._fsm: dict[str, OrderFSM] = {}
        self._req: dict[str, OrderRequest] = {}
        self._oid: dict[str, str] = {}  # client_order_id -> broker_order_id
        self._errors: list[datetime] = []
        self.consecutive_order_errors = 0
        self.consecutive_read_errors = 0
        self._exit_all_day: date | None = None
        self._exit_all_result: ExitAllResult | None = None
        self._signer = ticket_signer
        self._tickets: dict[str, RiskTicket] = {}

    # ------------------------------------------------------------------ introspection
    @property
    def paper_venue(self) -> bool:
        return getattr(self._b, "paper_venue", False) is True

    def order_state(self, client_order_id: str) -> OrderState | None:
        f = self._fsm.get(client_order_id)
        return f.state if f else None

    def fsm(self, client_order_id: str) -> OrderFSM | None:
        return self._fsm.get(client_order_id)

    def errors_in_window(self, now: datetime) -> int:
        self._errors = [t for t in self._errors if now - t <= self._window]
        return len(self._errors)

    @property
    def consecutive_errors(self) -> int:
        return max(self.consecutive_order_errors, self.consecutive_read_errors)

    # ------------------------------------------------------------------ error accounting
    def _order_err(self) -> None:
        self._errors.append(self._clock())
        self.consecutive_order_errors += 1

    def _read_err(self) -> None:
        self._errors.append(self._clock())
        self.consecutive_read_errors += 1

    # ------------------------------------------------------------------ RiskTicket (K-07)
    def attach_ticket(self, client_order_id: str, ticket: RiskTicket) -> None:
        """The runtime hands over the Governor's ticket for the order it is about to place."""
        self._tickets[client_order_id] = ticket

    def _check_ticket(self, req: OrderRequest) -> None:
        if self._signer is None or req.tag != ENTRY_TAG:
            return
        t = self._tickets.pop(req.client_order_id, None)  # single use
        why = "no RiskTicket attached" if t is None else ticket_mismatch(self._signer, t, req, self._clock())
        if why:
            raise TicketRejectedError(f"GATEWAY_TICKET: {req.client_order_id}: {why}")

    # ------------------------------------------------------------------ BrokerAdapter
    def is_connected(self) -> bool:
        return self._b.is_connected()

    def place(self, req: OrderRequest) -> str:
        cid = req.client_order_id
        if cid in self._oid:
            return self._oid[cid]  # idempotent: already at the broker, never resent
        f = self._fsm.get(cid)
        if f is not None:
            if f.state is OrderState.UNKNOWN:
                return self._resolve_unknown(f)
            raise BrokerRejectError(f"GATEWAY_DUPLICATE_ID: {cid} was already used ({f.state}); use a new attempt")
        blocked = [
            c
            for c, x in self._fsm.items()
            if x.state is OrderState.UNKNOWN and self._req[c].instrument_key == req.instrument_key
        ]
        self._check_ticket(req)
        if blocked:
            raise BrokerRejectError(f"GATEWAY_UNKNOWN_PENDING: {req.instrument_key} has unresolved order(s) {blocked}")
        if req.side is OrderSide.SELL:
            self._check_closable(req)
        self._take_token()
        f = OrderFSM(cid, req.qty, OrderState.APPROVED)
        self._fsm[cid], self._req[cid] = f, req
        f.apply(OrderEvt.SEND)
        try:
            oid = self._b.place(req)
        except BrokerTimeoutError:
            f.apply(OrderEvt.TIMEOUT)
            self._order_err()
            raise
        except (BrokerRejectError, BrokerDisconnectedError):
            f.apply(OrderEvt.REJECT)  # not accepted (a disconnect means it was never sent)
            self._order_err()
            raise
        except BrokerError:
            f.apply(OrderEvt.TIMEOUT)  # an unclassified failure: the order's fate is unknown
            self._order_err()
            raise
        self.consecutive_order_errors = 0
        self._oid[cid] = oid
        f.apply(OrderEvt.ACK)
        return oid

    def _take_token(self) -> None:
        now = self._clock()
        if self._refilled is not None:
            self._tokens = min(float(self._burst), self._tokens + (now - self._refilled).total_seconds() * self._rate)
        self._refilled = now
        if self._tokens < 1:
            raise BrokerRejectError(f"GATEWAY_RATE_LIMIT: {self._rate}/s sustained, burst {self._burst}")
        self._tokens -= 1

    def _resolve_unknown(self, f: OrderFSM) -> str:
        book = {o.request.client_order_id: o for o in self.orders()}
        bo = book.get(f.client_order_id)
        if bo is None:
            f.apply(OrderEvt.NOT_FOUND)
            raise BrokerRejectError(f"GATEWAY_NOT_FOUND: {f.client_order_id} never reached the broker; new attempt")
        self._oid[f.client_order_id] = bo.broker_order_id
        return bo.broker_order_id

    def _check_closable(self, req: OrderRequest) -> None:
        held = {p.instrument_key: p.net_qty for p in self.positions()}.get(req.instrument_key, 0)
        working = sum(
            o.request.qty - o.filled_qty
            for o in self.orders()
            if o.request.side is OrderSide.SELL
            and o.request.instrument_key == req.instrument_key
            and o.status not in TERMINAL
        )
        if req.qty > held - working:
            raise BrokerRejectError(
                f"GATEWAY_LONG_ONLY: sell {req.qty} of {req.instrument_key} > broker long {held} - working {working}"
            )

    def modify(self, broker_order_id: str, *, price: Decimal, trigger_price: Decimal | None = None) -> None:
        n = self._mods.get(broker_order_id, 0)
        if n >= self._max_mods:
            raise BrokerRejectError(f"GATEWAY_MODIFY_CAP: {n} modifications; cancel and replace instead")
        self._take_token()
        self._mods[broker_order_id] = n + 1
        try:
            self._b.modify(broker_order_id, price=price, trigger_price=trigger_price)
        except BrokerError:
            self._order_err()
            raise
        self.consecutive_order_errors = 0

    def cancel(self, broker_order_id: str) -> None:
        cid = next((c for c, o in self._oid.items() if o == broker_order_id), None)
        f = self._fsm.get(cid) if cid else None
        try:
            self._b.cancel(broker_order_id)
        except BrokerError:
            self._order_err()
            raise
        self.consecutive_order_errors = 0
        if f is not None and f.state in (OrderState.ACKED, OrderState.PARTIALLY_FILLED):
            f.apply(OrderEvt.CANCEL_REQUEST)

    def orders(self) -> list[BrokerOrder]:
        try:
            out = self._b.orders()
        except BrokerError:
            self._read_err()
            raise
        self.consecutive_read_errors = 0
        for bo in out:
            f = self._fsm.get(bo.request.client_order_id)
            if f is not None:
                _sync(f, bo)
                self._oid.setdefault(f.client_order_id, bo.broker_order_id)
        return out

    def trades(self) -> list[Fill]:
        return self._read(self._b.trades)

    def positions(self) -> list[BrokerPosition]:
        return self._read(self._b.positions)

    def funds(self) -> Decimal:
        return self._read(self._b.funds)

    def poll_events(self) -> list[OrderEvent]:
        return self._read(self._b.poll_events)

    def _read[T](self, fn: Callable[[], T]) -> T:
        try:
            v = fn()
        except BrokerError:
            self._read_err()
            raise
        self.consecutive_read_errors = 0
        return v

    def exit_all(self) -> ExitAllResult:
        day = self._clock().astimezone(IST).date()
        if self._exit_all_day == day and self._exit_all_result is not None:
            return self._exit_all_result  # once per trading day: a repeat never re-sends
        try:
            res = self._b.exit_all()
        except BrokerError:
            self._order_err()
            raise
        self.consecutive_order_errors = 0
        self._exit_all_day, self._exit_all_result = day, res
        return res


def _sync(f: OrderFSM, bo: BrokerOrder) -> None:
    """Advance an order's FSM to what the broker's book shows (fills first, then the terminal status)."""
    if f.terminal:
        return
    if bo.status is OrderStatus.REJECTED:
        if f.state is not OrderState.CANCEL_REQUESTED:
            f.apply(OrderEvt.REJECT)
        return
    if f.state in (OrderState.SUBMITTED, OrderState.UNKNOWN):
        f.apply(OrderEvt.ACK)
    delta = bo.filled_qty - f.filled
    if delta > 0:
        f.apply(OrderEvt.PARTIAL_FILL, delta)
    if bo.status is OrderStatus.CANCELLED and not f.terminal:
        f.apply(OrderEvt.CANCELLED)

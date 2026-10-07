"""Broker-agnostic order/fill/position types and the adapter protocol (docs/architecture/execution-engine.md §10.6).

Only LIMIT and SL (stop-limit) orders exist; MARKET, SL-M and IOC are unrepresentable
(docs/architecture/execution-engine.md C7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from project100c.core_types import OrderSide
from project100c.errors import BrokerError


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    SL = "SL"  # stop-limit: becomes a LIMIT at `price` once `trigger_price` is touched


class OrderStatus(StrEnum):
    PENDING_ACK = "PENDING_ACK"  # sent, broker has not acknowledged yet
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"  # SL order waiting for its trigger
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str  # idempotency key: intent_id + leg + attempt
    instrument_key: str
    side: OrderSide
    qty: int
    order_type: OrderType
    price: Decimal
    trigger_price: Decimal | None = None
    tag: str = ""

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise BrokerError("client_order_id is required (idempotency key)")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty <= 0:
            raise BrokerError(f"qty must be a positive int (got {self.qty!r})")
        if not isinstance(self.price, Decimal) or self.price <= 0:
            raise BrokerError(f"price must be a positive Decimal (got {self.price!r})")
        if self.order_type is OrderType.SL:
            if not isinstance(self.trigger_price, Decimal) or self.trigger_price <= 0:
                raise BrokerError("SL order needs a positive Decimal trigger_price")
            if self.side is OrderSide.SELL and self.trigger_price < self.price:
                raise BrokerError("sell SL: trigger must be >= limit price")
            if self.side is OrderSide.BUY and self.trigger_price > self.price:
                raise BrokerError("buy SL: trigger must be <= limit price")
        elif self.trigger_price is not None:
            raise BrokerError("LIMIT order must not carry a trigger_price")


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    broker_order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_qty: int
    avg_fill_price: Decimal | None
    reject_reason: str | None
    updated: datetime
    price: Decimal  # current (possibly modified) limit
    trigger_price: Decimal | None
    modifications: int


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: str
    broker_order_id: str
    client_order_id: str
    instrument_key: str
    side: OrderSide
    qty: int
    price: Decimal
    ts: datetime


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument_key: str
    net_qty: int
    buy_qty: int
    sell_qty: int
    buy_value: Decimal
    sell_value: Decimal


class OrderEventKind(StrEnum):
    ACK = "ACK"
    REJECT = "REJECT"
    TRIGGERED = "TRIGGERED"
    FILL = "FILL"
    MODIFIED = "MODIFIED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    kind: OrderEventKind
    broker_order_id: str
    client_order_id: str
    ts: datetime
    fill: Fill | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExitAllResult:
    """What the broker reported for an Exit-All request (OD-007). The caller must still re-read positions().

    `pricing_verified` stays False until the real Upstox Exit-All pricing has been tested in the sandbox.
    """

    requested_at: datetime
    exit_order_ids: tuple[str, ...]
    cancelled_order_ids: tuple[str, ...]
    failed_instruments: tuple[str, ...]
    pricing_verified: bool = False


class BrokerAdapter(Protocol):
    def is_connected(self) -> bool: ...
    def place(self, req: OrderRequest) -> str: ...
    def modify(self, broker_order_id: str, *, price: Decimal, trigger_price: Decimal | None = None) -> None: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def orders(self) -> list[BrokerOrder]: ...
    def trades(self) -> list[Fill]: ...
    def positions(self) -> list[BrokerPosition]: ...
    def funds(self) -> Decimal: ...
    def poll_events(self) -> list[OrderEvent]: ...
    def exit_all(self) -> ExitAllResult: ...

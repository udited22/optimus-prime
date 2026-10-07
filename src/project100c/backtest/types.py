"""Backtest order/fill records. Only LIMIT and SL-LIMIT exist (no MARKET, no SL-M: S44,
docs/architecture/execution-engine.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from project100c.core_types import OptionRight, OrderSide
from project100c.costs.model import ChargeBreakdown
from project100c.errors import BacktestError


class SimOrderType(StrEnum):
    LIMIT = "LIMIT"
    SL_LIMIT = "SL_LIMIT"


class OrderStatus(StrEnum):
    OPEN = "OPEN"  # accepted; resting (or in flight until active_at)
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PlaceOrder:
    instrument_key: str
    underlying: str
    right: OptionRight | None
    side: OrderSide
    qty: int
    lot_size: int
    order_type: SimOrderType
    limit_price: Decimal
    trigger_price: Decimal | None = None
    tag: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.limit_price, Decimal) or self.limit_price <= 0:
            raise BacktestError("limit_price must be a Decimal > 0")
        if self.order_type is SimOrderType.SL_LIMIT:
            t = self.trigger_price
            if not isinstance(t, Decimal) or t <= 0:
                raise BacktestError("SL_LIMIT needs a Decimal trigger_price > 0")
            if self.side is OrderSide.SELL and self.limit_price > t:
                raise BacktestError("SL_LIMIT SELL needs limit <= trigger")
            if self.side is OrderSide.BUY and self.limit_price < t:
                raise BacktestError("SL_LIMIT BUY needs limit >= trigger")
        elif self.trigger_price is not None:
            raise BacktestError("LIMIT orders take no trigger_price")


@dataclass(frozen=True, slots=True)
class CancelOrder:
    order_id: str


Intent = PlaceOrder | CancelOrder


@dataclass(slots=True)
class SimOrder:
    order_id: str
    spec: PlaceOrder
    submitted_at: datetime
    active_at: datetime  # when the exchange has it (submit + latency)
    status: OrderStatus = OrderStatus.OPEN
    filled_qty: int = 0
    triggered: bool = False
    cancel_at: datetime | None = None

    @property
    def remaining(self) -> int:
        return self.spec.qty - self.filled_qty

    @property
    def live(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    instrument_key: str
    side: OrderSide
    qty: int
    price: Decimal
    ts: datetime  # exchange time of the fill
    visible_at: datetime  # when the strategy learns about it (ts + report latency)
    charges: Decimal
    tag: str
    breakdown: ChargeBreakdown | None = None  # per-component charges (D-05) behind ``charges``
    half_spread: Decimal = Decimal(0)  # synthetic (ASSUMED) half-spread the bar had to clear; 0 = none


@dataclass(frozen=True, slots=True)
class OrderReport:
    order_id: str
    status: OrderStatus
    filled_qty: int
    ts: datetime
    visible_at: datetime
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OptionContract:
    """Terms of one option contract as the backtester knows them (from the lake reader or a test)."""

    instrument_key: str
    underlying: str
    expiry: date
    strike: Decimal
    right: OptionRight
    lot_size: int

    def __post_init__(self) -> None:
        if self.lot_size <= 0 or self.strike <= 0:
            raise BacktestError(f"{self.instrument_key}: lot_size and strike must be > 0")

"""Broker-agnostic types, adapter protocol and the deterministic fake broker (K-08)."""

from project100c.broker.fake import FakeBroker, FaultPlan
from project100c.broker.types import (
    TERMINAL,
    BrokerAdapter,
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

__all__ = [
    "TERMINAL",
    "BrokerAdapter",
    "BrokerOrder",
    "BrokerPosition",
    "ExitAllResult",
    "FakeBroker",
    "FaultPlan",
    "Fill",
    "OrderEvent",
    "OrderEventKind",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
]

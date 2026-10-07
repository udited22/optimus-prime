"""Mandate rule `long_only`. Pure, deterministic, no I/O.

Only BUYING NIFTY calls or puts may open or add to a position. A SELL is permitted only as a
sell-to-close of an existing long in the *same* instrument, and only for a quantity no larger than
(open long quantity - quantity already committed to pending SELL orders). Anything that could leave a
net short position is rejected with reason MANDATE_LONG_ONLY.

This is the first check the Risk Governor runs (K-02a), and the Gateway re-runs it before serialising.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from project100c.core_types import OptionRight, OrderSide
from project100c.errors import KernelInvariantError

__all__ = [
    "ALLOWED_UNDERLYINGS",
    "MandateDecision",
    "MandateOrder",
    "OptionRight",
    "OrderSide",
    "PositionSnapshot",
    "RejectReason",
    "check_long_only",
]


class RejectReason(StrEnum):
    MANDATE_LONG_ONLY = "MANDATE_LONG_ONLY"  # sell-to-open / would go net short
    MANDATE_UNDERLYING = "MANDATE_UNDERLYING"  # not NIFTY
    MANDATE_INSTRUMENT_TYPE = "MANDATE_INSTRUMENT_TYPE"  # not a CE/PE option
    INVALID_QUANTITY = "INVALID_QUANTITY"  # <= 0, non-int, or not a lot multiple


ALLOWED_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY"})


@dataclass(frozen=True, slots=True)
class MandateOrder:
    """The mandate-relevant projection of an order/intent."""

    instrument_key: str
    underlying: str
    right: OptionRight | None  # None => not an option (e.g. a future) => rejected
    side: OrderSide
    quantity: int
    lot_size: int


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Reconciled open quantities (units, signed) and quantities committed to pending SELL orders."""

    open_qty: Mapping[str, int] = field(default_factory=dict)
    pending_sell_qty: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for k, q in self.open_qty.items():
            if isinstance(q, bool) or not isinstance(q, int):
                raise KernelInvariantError(f"open_qty[{k}] is not an int")
            if q < 0:
                raise KernelInvariantError(f"net short position detected in {k} ({q}); violates OD-006")
        for k, q in self.pending_sell_qty.items():
            if isinstance(q, bool) or not isinstance(q, int) or q < 0:
                raise KernelInvariantError(f"pending_sell_qty[{k}] invalid ({q!r})")
            if q > self.open_qty.get(k, 0):
                raise KernelInvariantError(
                    f"pending sells ({q}) exceed open long ({self.open_qty.get(k, 0)}) in {k}; violates OD-006"
                )


@dataclass(frozen=True, slots=True)
class MandateDecision:
    approved: bool
    reasons: tuple[RejectReason, ...]
    is_closing: bool  # True for an approved sell-to-close

    @staticmethod
    def reject(*reasons: RejectReason) -> MandateDecision:
        return MandateDecision(approved=False, reasons=tuple(reasons), is_closing=False)


def check_long_only(order: MandateOrder, positions: PositionSnapshot) -> MandateDecision:
    reasons: list[RejectReason] = []
    q = order.quantity
    if (
        isinstance(q, bool)
        or not isinstance(q, int)
        or q <= 0
        or isinstance(order.lot_size, bool)
        or not isinstance(order.lot_size, int)
        or order.lot_size <= 0
        or q % order.lot_size != 0
    ):
        reasons.append(RejectReason.INVALID_QUANTITY)
    if order.underlying not in ALLOWED_UNDERLYINGS:
        reasons.append(RejectReason.MANDATE_UNDERLYING)
    if order.right is None or not isinstance(order.right, OptionRight):
        reasons.append(RejectReason.MANDATE_INSTRUMENT_TYPE)
    if not isinstance(order.side, OrderSide):
        reasons.append(RejectReason.MANDATE_LONG_ONLY)
        return MandateDecision.reject(*reasons)

    if order.side is OrderSide.SELL:
        open_long = positions.open_qty.get(order.instrument_key, 0)
        pending = positions.pending_sell_qty.get(order.instrument_key, 0)
        closable = open_long - pending
        if RejectReason.INVALID_QUANTITY not in reasons and q > closable:
            reasons.append(RejectReason.MANDATE_LONG_ONLY)
        elif RejectReason.INVALID_QUANTITY in reasons and closable <= 0:
            reasons.append(RejectReason.MANDATE_LONG_ONLY)

    if reasons:
        return MandateDecision.reject(*reasons)
    return MandateDecision(approved=True, reasons=(), is_closing=order.side is OrderSide.SELL)

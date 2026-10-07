"""The order lifecycle as a deterministic state machine (docs/architecture/execution-engine.md §10.2).

intent -> risk check -> submit -> ack -> fill / reject / cancel, plus UNKNOWN after a timeout (resolved by the
client order ID on the next read). Every transition is in ``TRANSITIONS``; anything else raises
``KernelInvariantError`` -- the engine never guesses an order's state. Pure: no clock, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from project100c.errors import KernelInvariantError


class OrderState(StrEnum):
    INTENT = "INTENT"  # a strategy intent, not yet risk-checked
    RISK_REJECTED = "RISK_REJECTED"  # the Governor said no (terminal)
    APPROVED = "APPROVED"  # a valid risk ticket, not yet sent
    SUBMITTED = "SUBMITTED"  # written to the journal and sent; no answer yet
    UNKNOWN = "UNKNOWN"  # the send timed out: the broker may or may not have it
    ACKED = "ACKED"  # the broker holds it (open or trigger-pending)
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FILLED = "FILLED"  # terminal
    REJECTED = "REJECTED"  # broker/exchange reject (terminal)
    CANCELLED = "CANCELLED"  # terminal (possibly after a partial fill)


class OrderEvt(StrEnum):
    APPROVE = "APPROVE"
    RISK_REJECT = "RISK_REJECT"
    SEND = "SEND"
    TIMEOUT = "TIMEOUT"
    ACK = "ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    REJECT = "REJECT"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCELLED = "CANCELLED"
    NOT_FOUND = "NOT_FOUND"  # an UNKNOWN order is absent from the broker's book: it was never accepted


S, E = OrderState, OrderEvt
TERMINAL_STATES = frozenset({S.RISK_REJECTED, S.FILLED, S.REJECTED, S.CANCELLED})
TRANSITIONS: dict[tuple[OrderState, OrderEvt], OrderState] = {
    (S.INTENT, E.APPROVE): S.APPROVED,
    (S.INTENT, E.RISK_REJECT): S.RISK_REJECTED,
    (S.APPROVED, E.SEND): S.SUBMITTED,
    (S.SUBMITTED, E.ACK): S.ACKED,
    (S.SUBMITTED, E.REJECT): S.REJECTED,
    (S.SUBMITTED, E.TIMEOUT): S.UNKNOWN,
    (S.SUBMITTED, E.PARTIAL_FILL): S.PARTIALLY_FILLED,  # a fill can arrive before the ack
    (S.SUBMITTED, E.FILL): S.FILLED,
    (S.UNKNOWN, E.ACK): S.ACKED,
    (S.UNKNOWN, E.REJECT): S.REJECTED,
    (S.UNKNOWN, E.PARTIAL_FILL): S.PARTIALLY_FILLED,
    (S.UNKNOWN, E.FILL): S.FILLED,
    (S.UNKNOWN, E.CANCELLED): S.CANCELLED,
    (S.UNKNOWN, E.NOT_FOUND): S.REJECTED,
    (S.ACKED, E.PARTIAL_FILL): S.PARTIALLY_FILLED,
    (S.ACKED, E.FILL): S.FILLED,
    (S.ACKED, E.REJECT): S.REJECTED,
    (S.ACKED, E.CANCEL_REQUEST): S.CANCEL_REQUESTED,
    (S.ACKED, E.CANCELLED): S.CANCELLED,  # e.g. broker Exit-All or exchange cancel
    (S.PARTIALLY_FILLED, E.PARTIAL_FILL): S.PARTIALLY_FILLED,
    (S.PARTIALLY_FILLED, E.FILL): S.FILLED,
    (S.PARTIALLY_FILLED, E.CANCEL_REQUEST): S.CANCEL_REQUESTED,
    (S.PARTIALLY_FILLED, E.CANCELLED): S.CANCELLED,
    (S.CANCEL_REQUESTED, E.CANCELLED): S.CANCELLED,
    (S.CANCEL_REQUESTED, E.PARTIAL_FILL): S.CANCEL_REQUESTED,  # a fill racing the cancel is still applied
    (S.CANCEL_REQUESTED, E.FILL): S.FILLED,
    (S.CANCEL_REQUESTED, E.REJECT): S.CANCEL_REQUESTED,  # cancel refused (order still live): keep waiting
}


@dataclass
class OrderFSM:
    client_order_id: str
    qty: int
    state: OrderState = OrderState.INTENT
    filled: int = 0
    history: list[tuple[OrderEvt, OrderState]] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def remaining(self) -> int:
        return self.qty - self.filled

    def apply(self, evt: OrderEvt, fill_qty: int = 0) -> OrderState:
        nxt = TRANSITIONS.get((self.state, evt))
        if nxt is None:
            raise KernelInvariantError(f"{self.client_order_id}: illegal transition {self.state} --{evt}-->")
        if evt in (OrderEvt.PARTIAL_FILL, OrderEvt.FILL):
            if fill_qty <= 0:
                raise KernelInvariantError(f"{self.client_order_id}: a fill needs a positive quantity")
            filled = self.filled + fill_qty
            if filled > self.qty:
                raise KernelInvariantError(f"{self.client_order_id}: over-fill {filled} > {self.qty}")
            if evt is OrderEvt.FILL and filled != self.qty:
                raise KernelInvariantError(f"{self.client_order_id}: FILL leaves {self.qty - filled} unfilled")
            if evt is OrderEvt.PARTIAL_FILL and filled == self.qty:
                nxt = OrderState.FILLED  # the last partial completes the order
            self.filled = filled
        elif fill_qty:
            raise KernelInvariantError(f"{self.client_order_id}: {evt} carries no fill quantity")
        self.state = nxt
        self.history.append((evt, nxt))
        return nxt

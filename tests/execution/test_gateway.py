"""ExecutionGateway (K-07) against the deterministic fake broker: idempotency, long-only, rate limit, order
state, error accounting and once-a-day Exit-All."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from project100c.broker import FakeBroker, OrderRequest, OrderType
from project100c.core_types import OrderSide
from project100c.errors import BrokerDisconnectedError, BrokerRejectError, BrokerTimeoutError
from project100c.execution import ExecutionGateway, OrderState
from project100c.sessions import IST

K = "NSE_FO|TEST-CE"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 5, 10, 0, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.t

    def adv(self, s: float) -> None:
        self.t += timedelta(seconds=s)


@pytest.fixture
def rig() -> tuple[ExecutionGateway, FakeBroker, Clock]:
    c = Clock()
    b = FakeBroker(c, funds=D("100000"))
    b.set_quote(K, D("2.95"), D("3.00"))
    return ExecutionGateway(b, c), b, c


def buy(cid: str, px: str = "3.00", qty: int = 65) -> OrderRequest:
    return OrderRequest(cid, K, OrderSide.BUY, qty, OrderType.LIMIT, D(px))


def sell(cid: str, px: str = "2.95", qty: int = 65) -> OrderRequest:
    return OrderRequest(cid, K, OrderSide.SELL, qty, OrderType.LIMIT, D(px))


Rig = tuple[ExecutionGateway, FakeBroker, Clock]


def test_place_is_idempotent(rig: Rig) -> None:
    g, b, _ = rig
    oid = g.place(buy("I1.CE.1"))
    assert g.place(buy("I1.CE.1")) == oid and len(b.orders()) == 1
    g.orders()
    assert g.order_state("I1.CE.1") is OrderState.FILLED


def test_lost_ack_is_resolved_from_the_book_without_a_resend(rig: Rig) -> None:
    g, b, _ = rig
    b.faults.timeout_next_places.append(True)  # accepted, ack lost
    with pytest.raises(BrokerTimeoutError):
        g.place(buy("I1.CE.1"))
    assert g.order_state("I1.CE.1") is OrderState.UNKNOWN
    oid = g.place(buy("I1.CE.1"))  # a retry with the same id resolves; it does not send again
    assert len(b.orders()) == 1 and b.orders()[0].broker_order_id == oid
    assert g.order_state("I1.CE.1") is OrderState.FILLED


def test_timeout_never_accepted_needs_a_new_attempt(rig: Rig) -> None:
    g, b, _ = rig
    b.faults.timeout_next_places.append(False)
    with pytest.raises(BrokerTimeoutError):
        g.place(buy("I1.CE.1"))
    with pytest.raises(BrokerRejectError, match="NOT_FOUND"):
        g.place(buy("I1.CE.1"))
    with pytest.raises(BrokerRejectError, match="DUPLICATE_ID"):
        g.place(buy("I1.CE.1"))
    assert b.orders() == []
    g.place(buy("I1.CE.2"))
    assert len(b.orders()) == 1


def test_a_rejected_id_is_never_reused(rig: Rig) -> None:
    g, b, _ = rig
    b.faults.reject_next_places.append("EXCHANGE: price band")
    with pytest.raises(BrokerRejectError):
        g.place(buy("I1.CE.1"))
    assert g.order_state("I1.CE.1") is OrderState.REJECTED and g.consecutive_order_errors == 1
    with pytest.raises(BrokerRejectError, match="DUPLICATE_ID"):
        g.place(buy("I1.CE.1"))


def test_long_only_sell_needs_a_broker_long(rig: Rig) -> None:
    g, b, _ = rig
    with pytest.raises(BrokerRejectError, match="LONG_ONLY"):
        g.place(sell("S0"))  # nothing held: a sell would open a short
    assert b.orders() == []
    g.place(buy("I1.CE.1"))
    g.place(sell("S1", "3.50"))  # rests (above the bid): 65 of 65 now working
    with pytest.raises(BrokerRejectError, match="LONG_ONLY"):
        g.place(sell("S2", "3.50", qty=1))
    assert g.consecutive_order_errors == 0  # a local refusal is not a broker error


def test_rate_limit_is_a_local_token_bucket(rig: Rig) -> None:
    g, b, c = rig
    for i in range(5):  # burst of 5
        g.place(buy(f"R{i}.CE.1", qty=1))
    with pytest.raises(BrokerRejectError, match="RATE_LIMIT"):
        g.place(buy("R9.CE.1", qty=1))
    assert len(b.orders()) == 5 and g.order_state("R9.CE.1") is None
    c.adv(0.5)  # 2/s sustained: one token back
    g.place(buy("R9.CE.1", qty=1))
    with pytest.raises(BrokerRejectError, match="RATE_LIMIT"):
        g.place(buy("R10.CE.1", qty=1))


def test_modification_cap(rig: Rig) -> None:
    g, _, c = rig
    oid = g.place(buy("M.CE.1", "2.50"))  # rests below the ask
    for i in range(20):
        c.adv(1)
        g.modify(oid, price=D("2.50") + D("0.05") * (i % 2))
    c.adv(1)
    with pytest.raises(BrokerRejectError, match="MODIFY_CAP"):
        g.modify(oid, price=D("2.55"))


def test_unknown_order_blocks_its_instrument(rig: Rig) -> None:
    g, b, _ = rig
    b.faults.timeout_next_places.append(True)
    with pytest.raises(BrokerTimeoutError):
        g.place(buy("U.CE.1"))
    with pytest.raises(BrokerRejectError, match="UNKNOWN_PENDING"):
        g.place(buy("V.CE.1"))
    g.orders()  # the book shows it: resolved
    assert g.order_state("U.CE.1") is OrderState.FILLED
    g.place(buy("V.CE.1"))


def test_error_counters_consecutive_reset_and_window(rig: Rig) -> None:
    g, b, c = rig
    b.faults.reject_next_places.extend(["X"] * 3)
    for i in range(3):
        with pytest.raises(BrokerRejectError):
            g.place(buy(f"E{i}.CE.1"))
    assert g.consecutive_errors == 3 and g.errors_in_window(c.t) == 3
    g.place(buy("OK.CE.1", qty=1))
    assert g.consecutive_errors == 0 and g.errors_in_window(c.t) == 3
    c.adv(15 * 60 + 1)
    assert g.errors_in_window(c.t) == 0
    b.faults.disconnected_windows.append((c.t, c.t + timedelta(seconds=5)))
    with pytest.raises(BrokerDisconnectedError):
        g.positions()
    assert g.consecutive_read_errors == 1 and g.consecutive_errors == 1


def test_cancel_and_partial_fills_follow_the_book(rig: Rig) -> None:
    g, b, _ = rig
    b.set_quote(K, D("2.95"), D("3.00"), ask_qty=20)
    oid = g.place(buy("P1.CE.1"))
    g.orders()
    fsm = g.fsm("P1.CE.1")
    assert fsm is not None and fsm.state is OrderState.PARTIALLY_FILLED and fsm.filled == 20
    g.cancel(oid)
    assert g.order_state("P1.CE.1") is OrderState.CANCEL_REQUESTED
    g.orders()
    assert g.order_state("P1.CE.1") is OrderState.CANCELLED and fsm.filled == 20


def test_exit_all_once_per_day(rig: Rig) -> None:
    g, b, c = rig
    g.place(buy("I1.CE.1"))
    calls: list[int] = []
    real = b.exit_all

    def counting() -> object:
        calls.append(1)
        return real()

    b.exit_all = counting  # type: ignore[method-assign,assignment]
    first = g.exit_all()
    assert g.exit_all() is first and len(calls) == 1
    assert {p.instrument_key: p.net_qty for p in b.positions()}[K] == 0
    c.adv(24 * 3600)
    g.exit_all()
    assert len(calls) == 2


def test_paper_venue_passes_through(rig: Rig) -> None:
    g, _, _ = rig
    assert g.paper_venue is True and g.is_connected()


def test_rate_limit_must_stay_below_the_exchange_limit() -> None:
    c = Clock()
    with pytest.raises(ValueError):
        ExecutionGateway(FakeBroker(c, funds=D(1)), c, rate_per_s=2, burst=10)

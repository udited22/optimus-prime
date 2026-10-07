from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from project100c.broker.types import BrokerOrder, BrokerPosition, OrderRequest, OrderStatus, OrderType
from project100c.core_types import OrderSide
from project100c.errors import KernelInvariantError
from project100c.execution import (
    TERMINAL_STATES,
    TRANSITIONS,
    Discrepancy,
    OrderEvt,
    OrderFSM,
    OrderState,
    broker_tag,
    client_order_id,
    reconcile,
)
from project100c.sessions import IST

T0 = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


# ---------------------------------------------------------------- ids
def test_client_order_ids_are_deterministic_and_distinct() -> None:
    a = client_order_id("I-20261005-0001", "CE", 1)
    assert a == client_order_id("I-20261005-0001", "CE", 1) == "I-20261005-0001.CE.1"
    assert len({a, client_order_id("I-20261005-0001", "PE", 1), client_order_id("I-20261005-0001", "CE", 2)}) == 3


@pytest.mark.parametrize(
    ("intent", "leg", "attempt"), [("", "CE", 1), ("a.b", "CE", 1), ("I1", "ce", 1), ("I1", "CE", 0)]
)
def test_client_order_id_validation(intent: str, leg: str, attempt: int) -> None:
    with pytest.raises(ValueError):
        client_order_id(intent, leg, attempt)


@given(st.text(min_size=1, max_size=80))
def test_broker_tag_is_short_alnum_and_stable(cid: str) -> None:
    t = broker_tag(cid)
    assert t == broker_tag(cid) and len(t) == 20 and t.isalnum() and t.startswith("P1")


def test_broker_tags_differ() -> None:
    tags = {broker_tag(f"I{i}.CE.1") for i in range(5000)}
    assert len(tags) == 5000


# ---------------------------------------------------------------- order state machine
def test_happy_path_and_partial_fills() -> None:
    f = OrderFSM("c1", 65)
    for e in (OrderEvt.APPROVE, OrderEvt.SEND, OrderEvt.ACK):
        f.apply(e)
    assert f.apply(OrderEvt.PARTIAL_FILL, 20) is OrderState.PARTIALLY_FILLED and f.remaining == 45
    assert f.apply(OrderEvt.PARTIAL_FILL, 45) is OrderState.FILLED and f.terminal  # the last partial completes it


def test_timeout_is_unknown_until_resolved() -> None:
    f = OrderFSM("c1", 65, OrderState.APPROVED)
    f.apply(OrderEvt.SEND)
    assert f.apply(OrderEvt.TIMEOUT) is OrderState.UNKNOWN
    assert f.apply(OrderEvt.NOT_FOUND) is OrderState.REJECTED


@pytest.mark.parametrize(
    ("state", "evt"),
    [
        (OrderState.INTENT, OrderEvt.SEND),  # no send without a risk approval
        (OrderState.RISK_REJECTED, OrderEvt.APPROVE),
        (OrderState.FILLED, OrderEvt.CANCEL_REQUEST),
        (OrderState.CANCELLED, OrderEvt.FILL),
        (OrderState.APPROVED, OrderEvt.ACK),
    ],
)
def test_illegal_transitions_raise(state: OrderState, evt: OrderEvt) -> None:
    with pytest.raises(KernelInvariantError):
        OrderFSM("c1", 65, state).apply(evt, 65 if evt is OrderEvt.FILL else 0)


def test_overfill_and_bad_fill_quantities_raise() -> None:
    f = OrderFSM("c1", 65, OrderState.ACKED)
    with pytest.raises(KernelInvariantError):
        f.apply(OrderEvt.PARTIAL_FILL, 66)
    with pytest.raises(KernelInvariantError):
        f.apply(OrderEvt.FILL, 10)  # FILL must complete the order
    with pytest.raises(KernelInvariantError):
        f.apply(OrderEvt.PARTIAL_FILL, 0)
    with pytest.raises(KernelInvariantError):
        f.apply(OrderEvt.CANCEL_REQUEST, 5)


def test_terminal_states_have_no_exits() -> None:
    assert not [k for k in TRANSITIONS if k[0] in TERMINAL_STATES]
    reachable = {OrderState.INTENT} | set(TRANSITIONS.values())
    assert reachable == set(OrderState)


@given(st.lists(st.tuples(st.sampled_from(list(OrderEvt)), st.integers(0, 70)), max_size=30))
def test_random_event_streams_never_corrupt_the_order(events: list[tuple[OrderEvt, int]]) -> None:
    f = OrderFSM("c1", 65)
    for evt, q in events:
        before = (f.state, f.filled)
        qty = q if evt in (OrderEvt.PARTIAL_FILL, OrderEvt.FILL) else 0
        try:
            f.apply(evt, qty)
        except KernelInvariantError:
            assert (f.state, f.filled) == before  # a refused event changes nothing
        assert 0 <= f.filled <= f.qty
        assert (f.state is OrderState.FILLED) <= (f.filled == f.qty)


# ---------------------------------------------------------------- reconciliation
def _bo(cid: str, status: OrderStatus, tag: str = "ENTRY") -> BrokerOrder:
    req = OrderRequest(cid, "K1", OrderSide.BUY, 65, OrderType.LIMIT, D("3"), tag=tag)
    return BrokerOrder("B-" + cid, req, status, 0, None, None, T0, D("3"), None, 0)


def _pos(key: str, net: int) -> BrokerPosition:
    return BrokerPosition(key, net, max(net, 0), max(-net, 0), D(0), D(0))


def test_reconcile_clean() -> None:
    r = reconcile({"K1": 65}, ["c1"], [_pos("K1", 65)], [_bo("c1", OrderStatus.OPEN), _bo("c0", OrderStatus.FILLED)])
    assert r.clean and r.summary() == "clean"


def test_reconcile_finds_every_kind() -> None:
    r = reconcile(
        {"K1": 65, "K2": 65, "K4": 65},
        ["c1", "c9"],
        [_pos("K1", 130), _pos("K3", 65), _pos("K5", -65), _pos("K4", 65)],
        [_bo("c1", OrderStatus.OPEN), _bo("x", OrderStatus.OPEN), _bo("e", OrderStatus.OPEN, tag="EXIT_ALL")],
    )
    kinds = {(f.kind, f.key) for f in r.findings}
    assert kinds == {
        (Discrepancy.POSITION_MISMATCH, "K1"),
        (Discrepancy.MISSING_POSITION, "K2"),
        (Discrepancy.UNEXPECTED_POSITION, "K3"),
        (Discrepancy.BROKER_NET_SHORT, "K5"),
        (Discrepancy.UNKNOWN_BROKER_ORDER, "x"),
        (Discrepancy.MISSING_BROKER_ORDER, "c9"),
    }
    assert not r.clean

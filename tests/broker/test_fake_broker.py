"""K-08 fake broker: acks, fills, partials, rejects, disconnects, timeouts, delayed fills."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from project100c.broker import FakeBroker, OrderEventKind, OrderRequest, OrderStatus, OrderType
from project100c.core_types import OrderSide
from project100c.errors import BrokerDisconnectedError, BrokerError, BrokerRejectError, BrokerTimeoutError
from project100c.sessions import IST

K = "NSE_FO|NIFTY26OCT25000CE"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 1, 10, 0, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.t

    def adv(self, s: float) -> None:
        self.t += timedelta(seconds=s)


def buy(cid: str = "i1-entry-0", qty: int = 65, px: str = "100.00") -> OrderRequest:
    return OrderRequest(cid, K, OrderSide.BUY, qty, OrderType.LIMIT, D(px))


def stop(cid: str = "i1-stop-0", qty: int = 65, trig: str = "80.00", px: str = "78.00") -> OrderRequest:
    return OrderRequest(cid, K, OrderSide.SELL, qty, OrderType.SL, D(px), D(trig))


@pytest.fixture
def env() -> tuple[Clock, FakeBroker]:
    c = Clock()
    b = FakeBroker(c, funds=D("10000"))
    b.set_quote(K, D("99.50"), D("100.00"))
    return c, b


def test_request_validation() -> None:
    with pytest.raises(BrokerError):
        OrderRequest("", K, OrderSide.BUY, 65, OrderType.LIMIT, D(1))
    with pytest.raises(BrokerError):
        OrderRequest("x", K, OrderSide.BUY, 0, OrderType.LIMIT, D(1))
    with pytest.raises(BrokerError):
        OrderRequest("x", K, OrderSide.BUY, 65, OrderType.LIMIT, 1.0)  # type: ignore[arg-type]
    with pytest.raises(BrokerError):
        OrderRequest("x", K, OrderSide.SELL, 65, OrderType.SL, D(80))  # no trigger
    with pytest.raises(BrokerError):
        OrderRequest("x", K, OrderSide.SELL, 65, OrderType.SL, D(80), D(79))  # trigger below limit on a sell stop
    with pytest.raises(BrokerError):
        OrderRequest("x", K, OrderSide.BUY, 65, OrderType.LIMIT, D(80), D(79))
    assert not hasattr(OrderType, "MARKET")


def test_ack_and_marketable_fill_at_ask_with_funds_debit(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    oid = b.place(buy(px="100.50"))
    (o,) = b.orders()
    assert o.broker_order_id == oid and o.status is OrderStatus.FILLED and o.avg_fill_price == D("100.00")
    assert b.funds() == D("10000") - D("6500.00")
    kinds = [e.kind for e in b.poll_events()]
    assert kinds == [OrderEventKind.ACK, OrderEventKind.FILL]
    assert b.poll_events() == []
    (p,) = b.positions()
    assert p.net_qty == 65


def test_idempotent_client_order_id(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    a = b.place(buy())
    assert b.place(buy()) == a
    assert len(b.orders()) == 1 and len(b.trades()) == 1


def test_non_marketable_rests_then_fills_when_market_moves(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.place(buy(px="99.00"))
    assert b.orders()[0].status is OrderStatus.OPEN
    c.adv(1)
    b.set_quote(K, D("98.50"), D("99.00"))
    assert b.orders()[0].status is OrderStatus.FILLED


def test_partial_fills_from_shallow_book_and_fault(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.set_quote(K, D("99.50"), D("100.00"), ask_qty=20)
    b.place(buy())
    o = b.orders()[0]
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == 20
    b.faults.max_fill_qty_per_match = 5
    c.adv(1)
    b.set_quote(K, D("99.50"), D("100.00"), ask_qty=1000)
    assert b.orders()[0].filled_qty == 25
    b.faults.max_fill_qty_per_match = None
    b.tick()
    assert b.orders()[0].status is OrderStatus.FILLED
    assert sum(f.qty for f in b.trades()) == 65


def test_sl_limit_triggers_on_bid_and_can_gap_through(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.place(buy())
    sid = b.place(stop())
    assert next(o for o in b.orders() if o.broker_order_id == sid).status is OrderStatus.TRIGGER_PENDING
    c.adv(1)
    b.set_quote(K, D("75.00"), D("76.00"))  # gaps through trigger AND limit -> triggered, unfilled
    s = next(o for o in b.orders() if o.broker_order_id == sid)
    assert s.status is OrderStatus.OPEN and s.filled_qty == 0
    c.adv(1)
    b.set_quote(K, D("78.50"), D("79.00"))
    s = next(o for o in b.orders() if o.broker_order_id == sid)
    assert s.status is OrderStatus.FILLED and s.avg_fill_price == D("78.50")
    assert b.positions()[0].net_qty == 0


def test_scripted_rejects_and_rms(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.faults.reject_next_places.append("EXCHANGE_REJECT: price band")
    with pytest.raises(BrokerRejectError, match="price band"):
        b.place(buy())
    assert b.orders()[0].status is OrderStatus.REJECTED
    with pytest.raises(BrokerRejectError, match="RMS_INSUFFICIENT_FUNDS"):
        b.place(buy("i2", qty=130))
    with pytest.raises(BrokerRejectError, match="UNKNOWN_INSTRUMENT"):
        b.place(OrderRequest("i3", "NSE_FO|NOPE", OrderSide.BUY, 65, OrderType.LIMIT, D(1)))
    assert {e.kind for e in b.poll_events()} == {OrderEventKind.REJECT}


def test_funds_check_counts_resting_buys(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.place(buy("a", px="90.00"))  # rests, commits 5850
    with pytest.raises(BrokerRejectError, match="RMS"):
        b.place(buy("b", px="90.00"))


def test_disconnect_window_blocks_api_but_exchange_keeps_matching(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.place(buy())
    b.place(stop())
    b.faults.disconnected_windows.append((c.t + timedelta(seconds=1), c.t + timedelta(seconds=30)))
    c.adv(2)
    assert not b.is_connected()
    for call in (b.orders, b.positions, b.funds, b.trades, b.poll_events):
        with pytest.raises(BrokerDisconnectedError):
            call()
    with pytest.raises(BrokerDisconnectedError):
        b.place(buy("x"))
    b.set_quote(K, D("79.00"), D("79.50"))  # stop fires while we are blind
    c.adv(30)
    assert b.is_connected()
    assert b.positions()[0].net_qty == 0


def test_timeouts_lost_ack_vs_not_accepted(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.faults.timeout_next_places.extend([False, True])
    with pytest.raises(BrokerTimeoutError, match="NOT accepted"):
        b.place(buy("t1"))
    assert b.orders() == []
    with pytest.raises(BrokerTimeoutError, match="WAS accepted"):
        b.place(buy("t2"))
    assert len(b.orders()) == 1  # order exists: caller must reconcile by client_order_id, never blind-retry new id
    assert b.place(buy("t2")) == b.orders()[0].broker_order_id  # idempotent retry is safe


def test_delayed_ack_and_delayed_fill(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.faults.ack_delay = timedelta(seconds=2)
    b.faults.fill_delay = timedelta(seconds=5)
    b.place(buy())
    assert b.orders()[0].status is OrderStatus.PENDING_ACK
    c.adv(2)
    b.tick()
    assert b.orders()[0].status is OrderStatus.OPEN
    c.adv(4)
    b.tick()
    assert b.orders()[0].filled_qty == 0
    c.adv(1)
    b.tick()
    assert b.orders()[0].status is OrderStatus.FILLED


def test_modify_cancel_and_their_rejects(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.place(buy())
    sid = b.place(stop())
    b.modify(sid, price=D("82.00"), trigger_price=D("84.00"))
    s = next(o for o in b.orders() if o.broker_order_id == sid)
    assert s.trigger_price == D("84.00") and s.modifications == 1
    with pytest.raises(BrokerError):
        b.modify(sid, price=D("90.00"), trigger_price=D("84.00"))  # invalid: trigger < limit on sell stop
    b.faults.reject_next_modifies.append("MODIFY_LIMIT")
    with pytest.raises(BrokerRejectError):
        b.modify(sid, price=D("82.00"), trigger_price=D("84.00"))
    b.faults.reject_next_cancels.append("CANCEL_REJECT")
    with pytest.raises(BrokerRejectError):
        b.cancel(sid)
    b.cancel(sid)
    with pytest.raises(BrokerRejectError, match="NOT_OPEN"):
        b.cancel(sid)
    with pytest.raises(BrokerRejectError, match="UNKNOWN_ORDER"):
        b.cancel("FB-999999")


def test_dropped_events_polling_stays_authoritative(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.faults.drop_events = True
    b.place(buy())
    assert b.poll_events() == []
    assert b.orders()[0].status is OrderStatus.FILLED


def test_rate_limit_and_phantom_position(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.faults.max_orders_per_second = 2
    b.place(buy("r1", qty=1, px="90"))
    b.place(buy("r2", qty=1, px="90"))
    with pytest.raises(BrokerRejectError, match="RATE_LIMIT"):
        b.place(buy("r3", qty=1, px="90"))
    c.adv(1)
    b.place(buy("r3", qty=1, px="90"))
    b.add_phantom_position(K, 65)
    assert b.positions()[0].net_qty == 65


def test_session_invalid_and_naive_clock() -> None:
    b = FakeBroker(lambda: datetime(2026, 10, 1, 10), funds=D(0))
    with pytest.raises(BrokerError, match="naive"):
        b.is_connected()
    with pytest.raises(BrokerError):
        FakeBroker(lambda: datetime(2026, 10, 1, 10, tzinfo=IST), funds=D(-1))
    b2 = FakeBroker(lambda: datetime(2026, 10, 1, 10, tzinfo=IST), funds=D(0))
    b2.session_valid = False
    with pytest.raises(BrokerDisconnectedError):
        b2.funds()


# ---- Exit-All (OD-007) ----
def test_exit_all_cancels_orders_and_flattens(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.place(buy())
    sid = b.place(stop())
    r = b.exit_all()
    assert r.pricing_verified is False  # UNVERIFIED until the Upstox sandbox test
    assert sid in r.cancelled_order_ids and r.failed_instruments == ()
    assert len(r.exit_order_ids) == 1
    assert all(p.net_qty == 0 for p in b.positions())
    exit_order = next(o for o in b.orders() if o.broker_order_id == r.exit_order_ids[0])
    assert exit_order.request.tag == "EXIT_ALL" and exit_order.avg_fill_price == D("99.50")


def test_exit_all_when_flat_is_noop(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    r = b.exit_all()
    assert r.exit_order_ids == () and r.failed_instruments == ()


def test_exit_all_failures_are_reported(env: tuple[Clock, FakeBroker]) -> None:
    c, b = env
    b.place(buy())
    b.faults.exit_all_error = BrokerRejectError("EXIT_ALL_UNAVAILABLE")
    with pytest.raises(BrokerRejectError):
        b.exit_all()
    b.faults.exit_all_error = None
    b.faults.exit_all_skip.add(K)
    assert b.exit_all().failed_instruments == (K,)
    assert b.positions()[0].net_qty == 65
    b.faults.exit_all_skip.clear()
    b.set_quote(K, None, D("100.00"))  # no bid: cannot exit
    assert b.exit_all().failed_instruments == (K,)
    b.set_quote(K, D("99.00"), D("100.00"), bid_qty=20)  # shallow bid: partial exit
    r = b.exit_all()
    assert r.failed_instruments == (K,) and b.positions()[0].net_qty == 45
    b.faults.disconnected_windows.append((c.t, c.t + timedelta(seconds=5)))
    with pytest.raises(BrokerDisconnectedError):
        b.exit_all()


def test_duplicate_and_out_of_order_updates(env: tuple[Clock, FakeBroker]) -> None:
    _, b = env
    b.faults.duplicate_fills = True
    b.faults.reverse_events = True
    b.place(buy())
    assert len(b.trades()) == 2 and b.trades()[0] == b.trades()[1]
    kinds = [e.kind for e in b.poll_events()]
    assert kinds == [OrderEventKind.FILL, OrderEventKind.FILL, OrderEventKind.ACK]
    assert b.positions()[0].net_qty == 65  # positions are not double-counted

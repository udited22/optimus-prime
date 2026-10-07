"""B-02 unit scenarios: no fill without trade-through; SL-limit gap leaves the order unfilled; volume caps."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from project100c.backtest import BarFillModel, PlaceOrder, QuoteFillModel, SimOrder, SimOrderType
from project100c.core_types import OptionRight, OrderSide
from project100c.errors import BacktestError
from project100c.market_types import Bar, Quote
from tests.backtest.conftest import KEY, at

D = Decimal
M = BarFillModel(interval=timedelta(minutes=1))


def _order(
    side: OrderSide, lim: str, typ: SimOrderType = SimOrderType.LIMIT, trig: str | None = None, qty: int = 65
) -> SimOrder:
    spec = PlaceOrder(KEY, "NIFTY", OptionRight.CE, side, qty, 65, typ, D(lim), None if trig is None else D(trig))
    return SimOrder("O1", spec, at(10, 0), at(10, 0))


def _bar(o: str, h: str, lo: str, c: str, vol: int = 6500, m: int = 1) -> Bar:
    return Bar(KEY, at(10, m), D(o), D(h), D(lo), D(c), vol)


def test_limit_buy_needs_trade_through() -> None:
    o = _order(OrderSide.BUY, "100")
    m = M.match(o, _bar("101", "102", "100", "101"))  # touches 100 only
    assert m is not None and m.qty == 0
    m = M.match(o, _bar("101", "102", "99.95", "100"))
    assert m is not None and m.qty == 65 and m.price == D("100")  # no price improvement


def test_limit_buy_gap_down_still_fills_at_limit_not_open() -> None:
    m = M.match(_order(OrderSide.BUY, "100"), _bar("95", "96", "94", "95"))
    assert m is not None and m.qty == 65 and m.price == D("100")


def test_limit_sell_needs_trade_through() -> None:
    o = _order(OrderSide.SELL, "100")
    assert M.match(o, _bar("99", "100", "98", "99")).qty == 0  # type: ignore[union-attr]
    assert M.match(o, _bar("99", "100.05", "98", "99")).qty == 65  # type: ignore[union-attr]


def test_sl_limit_gap_below_limit_leaves_order_unfilled() -> None:
    o = _order(OrderSide.SELL, "88", SimOrderType.SL_LIMIT, "90")
    m = M.match(o, _bar("85", "87", "84", "86"))
    assert m is not None and m.triggered and m.qty == 0
    o.triggered = True
    assert M.match(o, _bar("86", "88", "85", "87", m=2)).qty == 0  # type: ignore[union-attr]  # touch is not enough
    m2 = M.match(o, _bar("87", "88.05", "86", "88", m=3))
    assert m2 is not None and m2.qty == 65 and m2.price == D("88")


def test_sl_limit_intrabar_trigger_fills_at_limit() -> None:
    m = M.match(_order(OrderSide.SELL, "88", SimOrderType.SL_LIMIT, "90"), _bar("95", "96", "89", "90"))
    assert m is not None and m.triggered and m.qty == 65 and m.price == D("88")


def test_sl_limit_not_triggered() -> None:
    m = M.match(_order(OrderSide.SELL, "88", SimOrderType.SL_LIMIT, "90"), _bar("95", "96", "90.05", "91"))
    assert m is not None and not m.triggered and m.qty == 0


def test_sl_limit_buy_gap_above_limit_unfilled_then_intrabar_fills() -> None:
    o = _order(OrderSide.BUY, "112", SimOrderType.SL_LIMIT, "110")
    m = M.match(o, _bar("115", "116", "114", "115"))
    assert m is not None and m.triggered and m.qty == 0
    m2 = M.match(_order(OrderSide.BUY, "112", SimOrderType.SL_LIMIT, "110"), _bar("105", "111", "104", "110"))
    assert m2 is not None and m2.qty == 65 and m2.price == D("112")


def test_bar_before_order_is_live_is_not_eligible() -> None:
    o = _order(OrderSide.BUY, "100")
    o.active_at = at(10, 1) + timedelta(milliseconds=300)
    assert M.match(o, _bar("95", "96", "94", "95", m=1)) is None  # bar started 10:01:00 < active_at
    assert M.match(o, _bar("95", "96", "94", "95", m=2)) is not None


def test_participation_cap_in_whole_lots() -> None:
    o = _order(OrderSide.BUY, "100", qty=130)
    assert M.match(o, _bar("99", "100", "98", "99", vol=1300)).qty == 130  # type: ignore[union-attr]
    assert M.match(o, _bar("99", "100", "98", "99", vol=900)).qty == 65  # type: ignore[union-attr]  # 90 -> 1 lot
    assert M.match(o, _bar("99", "100", "98", "99", vol=500)).qty == 0  # type: ignore[union-attr]


def test_quote_model_touch_and_size() -> None:
    q = QuoteFillModel()
    o = _order(OrderSide.BUY, "100", qty=130)

    def quote(
        bid: str | None, ask: str | None, bq: int | None = 650, aq: int | None = 100, ltp: str | None = "100"
    ) -> Quote:
        return Quote(
            KEY,
            at(10, 1),
            at(10, 1),
            None if bid is None else D(bid),
            None if ask is None else D(ask),
            bq,
            aq,
            None if ltp is None else D(ltp),
            1000,
        )

    assert q.match(o, quote("99.5", "100.5")).qty == 0  # type: ignore[union-attr]
    m = q.match(o, quote("99.5", "99.9"))
    assert m is not None and m.qty == 65 and m.price == D("99.9")  # displayed 100 units -> 1 lot
    assert q.match(o, quote("99.5", None)).qty == 0  # type: ignore[union-attr]  # one-sided: no fill
    s = _order(OrderSide.SELL, "88", SimOrderType.SL_LIMIT, "90")
    m2 = q.match(s, quote("89.5", "90.5", ltp="89.9"))
    assert m2 is not None and m2.triggered and m2.qty == 65 and m2.price == D("89.5")
    s2 = _order(OrderSide.SELL, "88", SimOrderType.SL_LIMIT, "90")
    m3 = q.match(s2, quote("87", "87.5", ltp="87.2"))
    assert m3 is not None and m3.triggered and m3.qty == 0  # bid below the limit


def test_order_spec_validation() -> None:
    with pytest.raises(BacktestError):
        _order(OrderSide.SELL, "91", SimOrderType.SL_LIMIT, "90")
    with pytest.raises(BacktestError):
        _order(OrderSide.BUY, "89", SimOrderType.SL_LIMIT, "90")
    with pytest.raises(BacktestError):
        _order(OrderSide.BUY, "100", SimOrderType.LIMIT, "90")
    with pytest.raises(BacktestError):
        _order(OrderSide.BUY, "0")
    with pytest.raises(BacktestError):
        BarFillModel(interval=timedelta(minutes=1), participation=D("0"))

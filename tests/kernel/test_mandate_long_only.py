"""OD-006: long options only. Sell-to-open rejected; sell-to-close <= open long - pending sells allowed."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from project100c.errors import KernelInvariantError
from project100c.kernel.mandate import (
    MandateOrder,
    OptionRight,
    OrderSide,
    PositionSnapshot,
    RejectReason,
    check_long_only,
)

LOT = 65
CE = "NSE_FO|NIFTY-CE-22700"
PE = "NSE_FO|NIFTY-PE-22700"


def order(
    side: OrderSide,
    qty: int = LOT,
    key: str = CE,
    right: OptionRight | None = OptionRight.CE,
    underlying: str = "NIFTY",
) -> MandateOrder:
    return MandateOrder(instrument_key=key, underlying=underlying, right=right, side=side, quantity=qty, lot_size=LOT)


FLAT = PositionSnapshot()


def test_buy_call_and_buy_put_allowed_when_flat() -> None:
    assert check_long_only(order(OrderSide.BUY), FLAT).approved
    assert check_long_only(order(OrderSide.BUY, key=PE, right=OptionRight.PE), FLAT).approved


def test_sell_to_open_rejected_when_flat() -> None:
    d = check_long_only(order(OrderSide.SELL), FLAT)
    assert not d.approved and d.reasons == (RejectReason.MANDATE_LONG_ONLY,)


def test_sell_to_close_full_and_partial_allowed() -> None:
    pos = PositionSnapshot(open_qty={CE: 2 * LOT})
    full = check_long_only(order(OrderSide.SELL, 2 * LOT), pos)
    part = check_long_only(order(OrderSide.SELL, LOT), pos)
    assert full.approved and full.is_closing
    assert part.approved and part.is_closing


def test_sell_more_than_open_long_rejected() -> None:
    pos = PositionSnapshot(open_qty={CE: LOT})
    d = check_long_only(order(OrderSide.SELL, 2 * LOT), pos)
    assert not d.approved and RejectReason.MANDATE_LONG_ONLY in d.reasons


def test_pending_sells_count_against_closable_qty() -> None:
    # A protective SL (sell 65) already rests; a second sell of 65 would open a short if both fill.
    pos = PositionSnapshot(open_qty={CE: LOT}, pending_sell_qty={CE: LOT})
    d = check_long_only(order(OrderSide.SELL, LOT), pos)
    assert not d.approved and RejectReason.MANDATE_LONG_ONLY in d.reasons


def test_sell_in_other_instrument_is_sell_to_open() -> None:
    # Long the CE does not permit selling the PE (that would be a short PE = spread/strangle leg).
    pos = PositionSnapshot(open_qty={CE: LOT})
    d = check_long_only(order(OrderSide.SELL, key=PE, right=OptionRight.PE), pos)
    assert not d.approved and RejectReason.MANDATE_LONG_ONLY in d.reasons


def test_spread_short_leg_rejected() -> None:
    # Bull call spread = buy 22700 CE + sell 22800 CE. The short leg is a sell-to-open.
    pos = PositionSnapshot(open_qty={CE: LOT})
    short_leg = order(OrderSide.SELL, key="NSE_FO|NIFTY-CE-22800")
    assert not check_long_only(short_leg, pos).approved


@pytest.mark.parametrize("qty", [0, -65, 64, 100, True])
def test_invalid_quantity_rejected(qty: int) -> None:
    d = check_long_only(order(OrderSide.BUY, qty), FLAT)
    assert not d.approved and RejectReason.INVALID_QUANTITY in d.reasons


def test_non_nifty_and_non_option_rejected() -> None:
    d1 = check_long_only(order(OrderSide.BUY, underlying="BANKNIFTY"), FLAT)
    d2 = check_long_only(order(OrderSide.BUY, right=None, key="NSE_FO|NIFTY-FUT"), FLAT)
    assert RejectReason.MANDATE_UNDERLYING in d1.reasons
    assert RejectReason.MANDATE_INSTRUMENT_TYPE in d2.reasons


def test_net_short_state_is_an_invariant_violation() -> None:
    with pytest.raises(KernelInvariantError):
        PositionSnapshot(open_qty={CE: -LOT})
    with pytest.raises(KernelInvariantError):
        PositionSnapshot(open_qty={CE: LOT}, pending_sell_qty={CE: 2 * LOT})


# ---- property: no sequence of approved orders can create a net short ----
actions = st.lists(
    st.tuples(
        st.sampled_from([OrderSide.BUY, OrderSide.SELL]), st.integers(1, 3), st.sampled_from([CE, PE]), st.booleans()
    ),
    max_size=40,
)


@settings(max_examples=300)
@given(actions)
def test_property_never_net_short(seq: list[tuple[OrderSide, int, str, bool]]) -> None:
    open_qty: dict[str, int] = {}
    pending: dict[str, int] = {}
    for side, lots, key, fill_now in seq:
        right = OptionRight.CE if key == CE else OptionRight.PE
        snap = PositionSnapshot(open_qty=dict(open_qty), pending_sell_qty=dict(pending))
        d = check_long_only(order(side, lots * LOT, key=key, right=right), snap)
        if not d.approved:
            continue
        if side is OrderSide.BUY:
            open_qty[key] = open_qty.get(key, 0) + lots * LOT
        elif fill_now:
            open_qty[key] -= lots * LOT
        else:
            pending[key] = pending.get(key, 0) + lots * LOT
        # Worst case: every pending sell fills.
        for k in open_qty:
            assert open_qty[k] - pending.get(k, 0) >= 0

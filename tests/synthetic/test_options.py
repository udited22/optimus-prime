"""The SYNTHETIC Black-Scholes option chain used by the strategy-library mechanics tests (not market data)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from project100c.core_types import OptionRight
from project100c.synthetic import DayPlan, Segment, SyntheticDay, generate_chain, generate_day, merge_chains, option_key
from project100c.synthetic.options import LOT_SIZE, bs_price

WED = date(2026, 10, 7)
EXP = date(2026, 10, 13)


@pytest.fixture(scope="module")
def day() -> SyntheticDay:
    return generate_day(DayPlan(WED, 3, segments=(Segment(375, 0.001, 12),)))


def test_put_call_parity_holds_for_the_pricer() -> None:
    s, k, t, v = 25_000.0, 25_100.0, 6 / 365, 0.13
    c, p = bs_price(s, k, t, v, True), bs_price(s, k, t, v, False)
    assert c - p == pytest.approx(s - k * math.exp(-0.065 * t), abs=1e-6)


def test_the_chain_covers_atm_plus_minus_n_strikes_for_live_expiries(day: SyntheticDay) -> None:
    ch = generate_chain(day, [date(2026, 10, 6), EXP], strikes_each_side=3)
    assert {c.expiry for c in ch.contracts.values()} == {EXP}  # an expiry before the day is not live
    assert len(ch.contracts) == 7 * 2
    assert all(c.lot_size == LOT_SIZE == 65 for c in ch.contracts.values())
    assert len(ch.bars) == len(ch.contracts) * len(day.index)
    assert "SYNTHETIC" in ch.notes[0]


def test_bars_are_tick_sized_and_consistent(day: SyntheticDay) -> None:
    ch = generate_chain(day, [EXP], strikes_each_side=2)
    for b in ch.bars:
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        assert b.low >= Decimal("0.05")
        assert (b.close / Decimal("0.05")) % 1 == 0


def test_calls_rise_with_the_index_and_puts_fall(day: SyntheticDay) -> None:
    ch = generate_chain(day, [EXP], strikes_each_side=1)
    atm = int(ch.contracts[next(iter(ch.contracts))].strike) + 50  # the middle strike
    ce = [b for b in ch.bars if b.instrument_key == option_key(EXP, atm, OptionRight.CE)]
    pe = [b for b in ch.bars if b.instrument_key == option_key(EXP, atm, OptionRight.PE)]
    up = day.index[-1].close > day.index[0].open
    assert (ce[-1].close > ce[0].open) is up
    assert (pe[-1].close < pe[0].open) is up


def test_option_keys_sort_before_the_index_bars_of_the_same_minute(day: SyntheticDay) -> None:
    # the replay orders a minute's bars by key: the option marks must be visible when the strategy reacts to the
    # index bar of that minute
    key = option_key(EXP, 25_000, OptionRight.CE)
    assert key == "SYNTH|NIFTY13OCT2625000CE"
    assert key < day.index[0].instrument_key


def test_merge_keeps_every_contract_and_bar(day: SyntheticDay) -> None:
    a = generate_chain(day, [EXP], strikes_each_side=1)
    b = generate_chain(day, [date(2026, 10, 20)], strikes_each_side=1)
    m = merge_chains([a, b])
    assert len(m.contracts) == len(a.contracts) + len(b.contracts)
    assert len(m.bars) == len(a.bars) + len(b.bars)

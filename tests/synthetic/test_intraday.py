"""SYNTHETIC generator: deterministic, well-formed bars that follow the scripted plan."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from project100c.sessions.model import IST
from project100c.synthetic import FUT_KEY, INDEX_KEY, VIX_KEY, DayPlan, Segment, generate_day, generate_days

D = date(2026, 10, 6)


def test_same_plan_same_bars_and_different_seed_different_bars() -> None:
    p = DayPlan(D, 7)
    assert generate_day(p) == generate_day(p)
    assert generate_day(p).index != generate_day(DayPlan(D, 8)).index


def test_a_full_session_of_well_formed_one_minute_bars() -> None:
    d = generate_day(DayPlan(D, 1))
    assert len(d.index) == len(d.fut) == len(d.vix) == 375
    assert d.index[0].start.astimezone(IST).time() == time(9, 15)
    assert d.index[-1].start.astimezone(IST).time() == time(15, 29)
    for b in (*d.index, *d.fut):
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
    assert {b.instrument_key for b in d.index} == {INDEX_KEY}
    assert {b.instrument_key for b in d.fut} == {FUT_KEY} and {b.instrument_key for b in d.vix} == {VIX_KEY}
    assert all(b.volume == 0 for b in d.index) and all(b.volume > 0 for b in d.fut)
    assert all(k.startswith("SYNTH|") for k in (INDEX_KEY, FUT_KEY, VIX_KEY))
    assert any("SYNTHETIC" in n for n in d.notes)


def test_the_gap_drift_shock_vix_and_volume_scripts_are_honoured() -> None:
    d = generate_day(
        DayPlan(
            D, 3, prev_close=25000, gap_pct=1.0, segments=(Segment(375, 0.005, 6),), shocks=((200, -1.0),),
            vix_moves=((100, 4.0),), volume_bursts=((50, 60, 5.0),),
        )
    )  # fmt: skip
    assert abs(d.index[0].open / Decimal(25000) - Decimal("1.01")) < Decimal("0.0001")
    assert d.index[-1].close > d.index[0].open * Decimal("1.002")  # +1.875% drift, -1% shock, noise
    assert d.index[200].close < d.index[200].open * Decimal("0.992")  # the -1% shock lands on bar 200
    assert d.vix[110].close - d.vix[90].close > Decimal(3)
    burst = sum(b.volume for b in d.fut[50:60]) / 10
    calm = sum(b.volume for b in d.fut[70:80]) / 10
    assert burst > 3 * calm


def test_range_segments_mean_revert_and_days_chain_their_closes() -> None:
    days = generate_days([DayPlan(D, 1, segments=(Segment(375, 0, 14, revert=0.3),)), DayPlan(date(2026, 10, 7), 2)])
    hi = max(b.high for b in days[0].index)
    lo = min(b.low for b in days[0].index)
    assert (hi - lo) / days[0].index[0].open < Decimal("0.01")
    assert days[1].plan.prev_close == float(days[0].close)
    assert days[1].index[0].open == days[0].close  # no gap scripted

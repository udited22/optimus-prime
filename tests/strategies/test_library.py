"""Mechanics tests for the long-option strategy library (docs/research/strategy-hypotheses.md "Strategy library").

Every scenario is a seeded SYNTHETIC day crafted (and seed-searched) so the setup appears; the Black-Scholes chain is
SYNTHETIC; P&L numbers mean nothing. What is asserted is mechanics: the signal fires for the stated reason, the
regime gate decides, the entry is a BUY LIMIT, every filled leg is protected by an SL-LIMIT stop, the exit reason is
the expected one, nothing is ever sold beyond what is held, and the book is flat at the end. No edge is claimed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, time
from decimal import Decimal
from functools import cache

import pytest

from project100c.backtest.types import SimOrderType
from project100c.calendar.events import load_event_book
from project100c.core_types import OrderSide
from project100c.regime import EventCalendar, ScheduledEvent
from project100c.sessions.model import IST
from project100c.spec.io import load_spec_file
from project100c.spec.models import Lifecycle, Regime
from project100c.strategies.library import PLUGINS, LibraryParams, library_metadata
from project100c.synthetic import DayPlan, Segment
from tests.data.dhan_fakes import REPO
from tests.strategies.library_rig import SPECS, LibRun, rig, run, spec

WED = date(2026, 10, 7)
TUE = date(2026, 10, 6)  # a weekly expiry (configs/calendar/nifty_expiry_rules.toml)
EVENT = EventCalendar(
    version="T", status="UNVERIFIED", events=(ScheduledEvent(date=WED, kind="RBI_MPC", source="SYNTHETIC"),)
)


# ---------------------------------------------------------------------------------------------- scenarios
def gap_go(seed: int = 11, *, shocks: tuple[tuple[int, float], ...] = ()) -> list[DayPlan]:
    return [
        DayPlan(
            WED,
            seed,
            gap_pct=1.0,
            segments=(Segment(30, 0.02, 12), Segment(120, 0.01, 12), Segment(225, 0, 12)),
            shocks=shocks,
        )
    ]


def fbo(seed: int, up: float, down: float) -> list[DayPlan]:
    segs = (Segment(15, 0, 10, revert=0.3), Segment(8, up, 8), Segment(8, down, 8), Segment(344, 0, 10, revert=0.3))
    return [DayPlan(WED, seed, segments=segs)]


def expiry_pm(seed: int = 51) -> list[DayPlan]:
    return [DayPlan(TUE, seed, segments=(Segment(240, 0, 10), Segment(30, 0.02, 12), Segment(105, 0.01, 12)))]


def vix_spike(seed: int) -> list[DayPlan]:
    segs = (Segment(120, 0, 10), Segment(15, -0.04, 25), Segment(240, 0, 25))
    return [DayPlan(WED, seed, segments=segs, vix_open=13, vix_moves=((120, 0.6), (125, 0.6), (130, 0.6)))]


def event_day(seed: int = 51) -> list[DayPlan]:
    return [
        DayPlan(WED, seed, segments=(Segment(45, 0, 8, revert=0.3), Segment(20, 0.025, 14), Segment(310, 0.002, 12)))
    ]


SCENARIOS: dict[str, tuple[str, list[DayPlan], dict[str, object]]] = {
    "gap-go": ("S-GAPGO-001", gap_go(), {}),
    "gap-fade": (
        "S-GAPFADE-001",
        [DayPlan(WED, 7, gap_pct=0.6, segments=(Segment(40, -0.02, 12), Segment(335, 0, 12, revert=0.3)))],
        {},
    ),
    "failed-breakout": ("S-FBO-001", fbo(30, 0.02, -0.03), {}),
    "vwap-continuation": (
        "S-VWAPC-001",
        [
            DayPlan(
                WED,
                43,
                segments=(Segment(60, 0.01, 10), Segment(25, -0.012, 8), Segment(120, 0.012, 10), Segment(170, 0, 10)),
            )
        ],
        {},
    ),
    "vwap-reversion": (
        "S-VWAPMR-001",
        [
            DayPlan(
                WED,
                54,
                segments=(Segment(120, 0, 9, revert=0.3), Segment(10, 0.02, 9), Segment(245, 0, 9, revert=0.4)),
                vix_open=11,
            )
        ],
        {},
    ),
    "compression-breakout": (
        "S-VOLX-001",
        [
            DayPlan(
                WED,
                40,
                segments=(
                    Segment(60, 0, 10),
                    Segment(60, 0, 3, revert=0.5),
                    Segment(5, 0.06, 20),
                    Segment(60, 0.02, 12),
                    Segment(190, 0, 10),
                ),
                shocks=((120, 0.15),),
                volume_bursts=((120, 125, 3.0),),
            )
        ],
        {},
    ),
    "cheap-gamma": (
        "S-IVRV-001",
        [DayPlan(WED, 17, segments=(Segment(90, 0.004, 18), Segment(285, 0.004, 18)), vix_open=12)],
        {},
    ),
    "expiry-afternoon": ("S-EXP0-001", expiry_pm(), {}),
    "vix-straddle": ("S-VIXSTR-001", vix_spike(53), {"max_lots": 2}),
    "event-breakout": ("S-EVTBO-001", event_day(), {"events": EVENT}),
    "lunch-compression": (
        "S-LUNCH-001",
        [
            DayPlan(
                WED,
                53,
                segments=(
                    Segment(165, 0.002, 12),
                    Segment(60, 0, 2, revert=0.6),
                    Segment(5, 0.05, 20),
                    Segment(145, 0.01, 12),
                ),
            )
        ],
        {},
    ),
}

#: scenario -> (signal reason, exit reason)
EXPECTED = {
    "gap-go": ("GAP_UP_AND_GO", "TARGET"),
    "gap-fade": ("GAP_UP_FADE", "INVALIDATED_GAP_RESUMED"),
    "failed-breakout": ("FAILED_UPSIDE_BREAK", "TARGET_RANGE_MID"),
    "vwap-continuation": (None, "INVALIDATED_VWAP_LOST"),
    "vwap-reversion": (None, "TARGET_VWAP"),
    "compression-breakout": ("COMPRESSION_BREAK_UP", "TARGET"),
    "cheap-gamma": ("CHEAP_GAMMA_UP", "INVALIDATED_DIRECTION"),
    "expiry-afternoon": (None, "STOP"),
    "vix-straddle": (None, "TARGET"),
    "event-breakout": (None, "INVALIDATED_BACK_IN_RANGE"),
    "lunch-compression": (None, "TARGET"),
}


@cache
def scenario(name: str) -> LibRun:
    sid, plans, kw = SCENARIOS[name]
    return run(sid, plans, **kw)  # type: ignore[arg-type]


def assert_sound(lr: LibRun) -> None:
    """Invariants for every run: flat at the end, never short, BUY-only entries, stops on every filled leg."""
    res = lr.result
    assert res.flat_at_end
    held: dict[str, int] = defaultdict(int)
    for f in sorted(res.fills, key=lambda x: x.ts):
        held[f.instrument_key] += f.qty if f.side is OrderSide.BUY else -f.qty
        assert held[f.instrument_key] >= 0, f"sold beyond the held quantity: {f}"
        assert f.ts.astimezone(IST).time() < time(15, 0)
        if f.side is OrderSide.BUY:
            assert time(9, 20) <= f.ts.astimezone(IST).time() <= time(14, 1)
            assert "ENTRY" in f.tag
    orders = list(res.orders.values())
    for o in orders:
        assert o.spec.right is not None  # options only; the library never trades the index or futures
        if o.spec.side is OrderSide.BUY:
            assert o.spec.order_type is SimOrderType.LIMIT
    for t in lr.trades:
        for leg in t["legs_detail"]:
            if leg["entry_fill"] is not None:
                stops = [o for o in orders if o.spec.instrument_key == leg["key"] and o.spec.tag.endswith("|STOP")]
                assert stops, f"filled leg {leg['key']} was never protected"
                assert all(o.spec.order_type is SimOrderType.SL_LIMIT and o.spec.side is OrderSide.SELL for o in stops)
                assert leg["stop_trigger"] < leg["entry_fill"]


# ---------------------------------------------------------------------------------------------- the library
def test_every_library_spec_loads_as_research_with_a_plugin() -> None:
    files = sorted(p.stem for p in SPECS.glob("S-*.yaml"))
    assert set(PLUGINS) == set(files) - {"S-ORB-001", "S-ORB-002"}  # H01 and H01b: strategies/orb_h01*
    for sid in files:
        sp = load_spec_file(SPECS / f"{sid}.yaml")
        assert sp.status is Lifecycle.RESEARCH
        assert sp.falsification_criteria
        assert len(sp.cost_sensitivity) >= 20
        assert {Regime.NO_EDGE, Regime.ABNORMAL_MARKET} <= sp.prohibited_regimes
        assert all(leg.side == "BUY" for leg in sp.legs)  # long options only (OD-001)


def test_the_shipped_orb_spec_is_the_test_fixture() -> None:
    shipped = load_spec_file(SPECS / "S-ORB-001.yaml")
    fixture = load_spec_file(REPO / "tests" / "fixtures" / "spec" / "S-ORB-001.yaml")
    assert shipped == fixture


@pytest.mark.parametrize("sid", sorted(PLUGINS))
def test_params_come_from_the_spec(sid: str) -> None:
    p = LibraryParams.from_spec(spec(sid))
    sp = spec(sid)
    assert p.time_exit == sp.exit.time_exit
    assert p.entry_end <= time(14, 0)
    assert all(isinstance(v, Decimal) for v in p.signal.values())


# ---------------------------------------------------------------------------------------------- each strategy
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_strategy_trades_its_setup_and_exits_for_the_stated_reason(name: str) -> None:
    lr = scenario(name)
    assert_sound(lr)
    want_signal, want_exit = EXPECTED[name]
    closed = [t for t in lr.trades if t["outcome"] == "CLOSED"]
    # the scripted setup is the first trade; a style capped above 1 a day (docs/research/strategy-hypotheses.md entry
    # caps) may re-enter
    assert 1 <= len(closed) <= spec(SCENARIOS[name][0]).entry.max_entries_per_day, lr.trades
    t = closed[0]
    if want_signal is not None:
        assert t["reason"] == want_signal
    assert t["exit_reason"] == want_exit
    assert t["regime_check"] == "OK"
    assert t["classifier_status"] == "UNVALIDATED"
    legs = t["legs_detail"]
    assert all(leg["entry_fill"] is not None and leg["exit_fill"] is not None for leg in legs)
    assert t["risk_at_stop"] <= Decimal("0.02") * lr.result.starting_cash


def test_the_straddle_buys_both_legs_and_needs_two_lots() -> None:
    t = next(t for t in scenario("vix-straddle").trades if t["outcome"] == "CLOSED")
    assert sorted(t["rights"]) == ["CE", "PE"]
    assert len(t["legs_detail"]) == 2
    shipped = run("S-VIXSTR-001", vix_spike(53))  # as shipped: OD-013 lets a long straddle hold 2 lots
    assert_sound(shipped)
    assert shipped.result.rejects == 0 and shipped.trades[0]["exit_reason"] != "LEG_REJECTED"
    capped = run("S-VIXSTR-001", vix_spike(53), max_lots=1)  # under the plain 1-lot cap
    assert_sound(capped)
    assert capped.result.rejects >= 1
    t1 = capped.trades[0]
    assert t1["exit_reason"] == "LEG_REJECTED"  # the second leg was refused, so the first is closed
    assert "MAX_LOTS" in {leg["outcome"] for leg in t1["legs_detail"]}


def test_a_straddle_leg_that_never_fills_closes_the_other() -> None:
    lr = run("S-VIXSTR-001", vix_spike(52), max_lots=2)
    assert_sound(lr)
    assert lr.trades[0]["exit_reason"] == "LEG_UNFILLED"


def test_compression_breakout_confirms_with_futures_volume_when_supplied() -> None:
    t = scenario("compression-breakout").trades[0]
    assert t["facts"]["confirmation"] == "FUT_VOLUME"


def test_vwap_continuation_records_its_vwap_basis() -> None:
    t = scenario("vwap-continuation").trades[0]
    assert t["facts"]["vwap_basis"] in ("FUT_VOLUME", "INDEX_TWAP")


def test_the_event_breakout_trades_a_verified_date_from_the_shipped_calendar() -> None:
    book = load_event_book(REPO / "configs" / "calendar" / "events.yaml")
    cal = book.to_calendar(rig().clock.calendar)  # 2026-10-07: RBI MPC
    lr = run("S-EVTBO-001", event_day(), events=cal)
    assert_sound(lr)
    assert [t["exit_reason"] for t in lr.trades if t["outcome"] == "CLOSED"] == ["INVALIDATED_BACK_IN_RANGE"]


def test_the_event_breakout_needs_a_listed_event() -> None:
    off = run("S-EVTBO-001", event_day())  # same tape, no event in the calendar
    assert_sound(off)
    assert not [t for t in off.trades if t["outcome"] == "CLOSED"]


# ---------------------------------------------------------------------------------------------- the base
def test_the_regime_gate_refuses_a_signal_in_the_wrong_regime() -> None:
    gated = run("S-FBO-001", fbo(31, 0.03, -0.04))
    assert_sound(gated)
    assert gated.strategy.counters.get("REGIME_NOT_ALLOWED", 0) >= 1
    assert not gated.result.fills
    refused = gated.strategy.signals[0]
    assert refused["outcome"] == "REGIME_NOT_ALLOWED"
    assert "TRENDING_UP" in refused["regime_tags"]
    ungated = run("S-FBO-001", fbo(31, 0.03, -0.04), gate=False)  # research switch: the same signal goes through
    assert ungated.strategy.counters.get("ENTRY_SENT") == 1
    assert library_metadata(ungated.strategy)["regime_gate"] is False


def test_a_regime_change_invalidates_an_open_trade() -> None:
    lr = run("S-GAPGO-001", gap_go(shocks=((40, 0.85),)))  # a violent bar -> ABNORMAL_MARKET (sticky)
    assert_sound(lr)
    assert lr.trades[0]["exit_reason"] == "INVALIDATED_REGIME_CHANGE"


def test_an_entry_that_runs_away_is_left_unfilled() -> None:
    lr = run("S-FBO-001", fbo(31, 0.02, -0.03))
    assert_sound(lr)
    t = lr.trades[0]
    assert t["outcome"] == "ENTRY_UNFILLED"
    assert t["legs_detail"][0]["entry_attempts"] >= 2  # it chased before giving up
    assert not lr.result.fills


def test_from_the_hands_off_time_the_engine_flatten_owns_the_position() -> None:
    lr = run("S-GAPGO-001", gap_go(), hands_off_from=time(10, 0))
    assert_sound(lr)
    t = lr.trades[0]
    assert t["exit_reason"] == "ENGINE_FLATTEN"
    assert t["legs_detail"][0]["outcome"] == "FLATTENED"
    sells = [f for f in lr.result.fills if f.side is OrderSide.SELL]
    assert [f.tag for f in sells] == ["ENGINE_FLATTEN"]
    assert sells[0].ts.astimezone(IST).time() >= time(14, 50)


def test_a_working_entry_is_cancelled_at_the_hands_off_time() -> None:
    lr = run("S-EXP0-001", expiry_pm(), hands_off_from=time(13, 43))  # the signal is at 13:42
    assert_sound(lr)
    assert lr.trades[0]["outcome"] == "ENTRY_UNFILLED"
    assert not lr.result.fills


def test_a_trade_that_does_not_fit_the_risk_budget_is_not_sent() -> None:
    lr = run("S-GAPGO-001", gap_go(), nav=Decimal(50_000))  # 2% = INR 1,000 < one lot's risk at the stop
    assert lr.strategy.counters.get("NO_TRADE_RISK", 0) >= 1
    assert not lr.result.orders


def test_runs_are_deterministic_and_labelled() -> None:
    a, b = run("S-GAPGO-001", gap_go()), scenario("gap-go")
    assert a.result.ledger_hash == b.result.ledger_hash
    meta = library_metadata(a.strategy)
    assert "SYNTHETIC inputs" in meta["labels"]  # type: ignore[operator]
    assert meta["classifier_status"] == "UNVALIDATED"
    assert meta["deviations"]


def test_index_decision_bar_comes_after_the_same_minutes_option_bars() -> None:
    """Regression: with the Dhan label NIFTY-INDEX ('-' sorts before the 'NIFTY|...' option keys), ties broken by key
    put the index bar first, and every library entry was skipped as NO_OPTION_BAR. decision_keys fixes the order."""
    base = scenario("gap-go")
    plans = SCENARIOS["gap-go"][1]
    raw = run("S-GAPGO-001", plans, index_key="NIFTY-INDEX")
    assert raw.strategy.counters.get("NO_OPTION_BAR", 0) > 0 and not raw.result.fills
    fixed = run("S-GAPGO-001", plans, index_key="NIFTY-INDEX", decision_keys=("NIFTY-INDEX",))
    assert "NO_OPTION_BAR" not in fixed.strategy.counters
    assert [(f.ts, f.price, f.qty) for f in fixed.result.fills] == [(f.ts, f.price, f.qty) for f in base.result.fills]
    assert fixed.result.net_pnl == base.result.net_pnl

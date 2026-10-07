"""Mechanics tests for the volatility-round plug-ins (H32-H34) and the library-base features they need: the
premium-aware (budget) stop and the residual-position guard. SYNTHETIC tapes and chains; P&L numbers mean nothing; no
edge is claimed."""

from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal

from project100c.sessions.model import IST
from project100c.strategies.library import LibraryParams
from project100c.strategies.library.base import Session
from project100c.strategies.library.signals_vol import high_vol
from project100c.synthetic import DayPlan, Segment, generate_days
from tests.strategies.library_rig import run, spec
from tests.strategies.test_library import assert_sound
from tests.strategies.test_library_intel import dayvol_plans

WED = date(2026, 10, 7)
TUE = date(2026, 10, 6)  # a weekly expiry
TURBULENT = {"h26": Decimal(2)}
CALM = {"h26": Decimal(0), "h25": Decimal(2), "h25_from": Decimal(15)}


def busy(day: date, seed: int = 3) -> list[DayPlan]:
    return [DayPlan(day, seed, segments=(Segment(60, 0.01, 25), Segment(120, -0.01, 25), Segment(195, 0.005, 25)))]


def test_the_new_specs_read_their_exits_from_the_spec() -> None:
    hold = LibraryParams.from_spec(spec("S-VOLHOLD-001"))
    assert hold.budget_stop_min_pct == 20 and hold.stop_pct == 50 and not hold.stop_points and hold.target_r is None
    assert LibraryParams.from_spec(spec("S-EXPVOL-001")).dte_min == 0


def test_high_vol_reads_any_of_the_three_labels_point_in_time() -> None:
    s = Session(WED, None, features={"h25": Decimal(0), "h25_from": Decimal(15)})
    s.idx = list(generate_days(busy(WED))[0].index[:10])  # 09:15-09:25: H25 not known yet
    assert high_vol(s) == ()
    s.idx = list(generate_days(busy(WED))[0].index[:20])
    assert high_vol(s) == ("H25_TERM_LOW",)
    assert high_vol(Session(WED, None, features=TURBULENT)) == ("H26_TURBULENT",)
    assert high_vol(Session(WED, None, features=CALM)) == ()


def test_the_hold_straddle_uses_the_widest_stop_that_fits_the_budget() -> None:
    lr = run("S-VOLHOLD-001", busy(WED), nav=Decimal(400_000), daily_features={WED: TURBULENT})
    assert_sound(lr)
    t = next(t for t in lr.trades if t["outcome"] == "CLOSED")
    assert sorted(t["rights"]) == ["CE", "PE"] and t["facts"]["high_vol"] == "H26_TURBULENT"
    assert Decimal(20) <= t["stop_pct"] <= Decimal(50) and t["risk_at_stop"] <= Decimal(8000)
    for leg in t["legs_detail"]:
        want = (leg["entry_fill"] * (1 - t["stop_pct"] / 100) / Decimal("0.05")).to_integral_value(
            rounding="ROUND_FLOOR"
        )
        assert leg["stop_trigger"] == want * Decimal("0.05")  # the protective stop sits at the premium-aware %
    assert time(9, 31) <= t["at"].astimezone(IST).time() <= time(9, 46)


def test_no_label_no_straddle_and_a_too_small_budget_is_no_trade() -> None:
    calm = run("S-VOLHOLD-001", busy(WED), nav=Decimal(400_000), daily_features={WED: CALM})
    assert not calm.trades and not calm.strategy.signals
    tiny = run("S-VOLHOLD-001", busy(WED), nav=Decimal(10_000), daily_features={WED: TURBULENT})
    assert not tiny.trades and tiny.strategy.counters.get("NO_TRADE_STOP_TOO_TIGHT", 0) >= 1


def test_the_expiry_straddle_buys_the_expiring_contract_on_expiry_day_only() -> None:
    lr = run("S-EXPVOL-001", busy(TUE), nav=Decimal(400_000), daily_features={TUE: TURBULENT})
    assert_sound(lr)
    t = next(t for t in lr.trades if t["outcome"] == "CLOSED")
    assert t["expiry"] == TUE and t["exit_reason"] in ("TIME_EXIT", "STOP")
    assert not run("S-EXPVOL-001", busy(WED), nav=Decimal(400_000), daily_features={WED: TURBULENT}).trades


def test_the_cheap_vol_straddle_needs_the_label_and_the_ratio() -> None:
    plans = dayvol_plans()
    last = plans[-1].day
    on = run("S-VOLCHEAP-001", plans, nav=Decimal(2_000_000), chain_last_only=True, daily_features={last: TURBULENT})
    assert_sound(on)
    t = next(t for t in on.trades if t["outcome"] == "CLOSED")
    assert t["reason"] == "VOL_CHEAP_HIGH_FORECAST" and Decimal(t["facts"]["ratio"]) >= 1
    off = run("S-VOLCHEAP-001", plans, nav=Decimal(2_000_000), chain_last_only=True, daily_features={last: CALM})
    assert not off.trades


def test_a_residual_carried_overnight_is_never_stacked_on_by_the_next_trade() -> None:
    """Regression (S-VOLHOLD-001, 7-Aug-2024 in the lake): the engine could not flatten a straddle leg before 15:00
    (no print into the flatten window), the position carried (RESIDUAL_AT_CLOSE), and the next day's trade picked the
    same contract; the base then read the contract's net position as its own fill and crashed in ``_protect``.
    Positions are per contract, so a new trade must refuse a contract that is already held."""
    thu = WED + timedelta(days=1)
    plans = [busy(WED)[0], DayPlan(thu, 3, gap_pct=0.45, segments=(Segment(375, 0.0, 5),))]

    def no_late_prints(b: object) -> bool:
        st = b.start.astimezone(IST)  # type: ignore[attr-defined]
        return bool(st.date() == WED and st.time() >= time(14, 46))

    lr = run("S-VOLHOLD-001", plans, nav=Decimal(400_000), daily_features={WED: TURBULENT, thu: TURBULENT},
             drop_option_bars=no_late_prints)  # fmt: skip
    first = lr.trades[0]
    assert first["day"] == WED
    held = {lg["key"] for lg in first["legs_detail"] if lg["entry_fill"] is not None and lg["exit_fill"] is None}
    assert held, "the scenario needs a residual leg"
    later = [s for s in lr.strategy.signals if s["day"] == thu]
    assert later and any(s.get("outcome") == "NO_TRADE_RESIDUAL_POSITION" for s in later)
    for t in lr.trades[1:]:
        assert not held & set(t["legs"])  # nothing stacked on the residual

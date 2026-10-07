"""Paper loop on SIMULATED days: library strategies -> allocator -> Governor (paper venue, regime gate on) ->
kernel runtime -> fake broker. Mechanics only: the days are seeded SYNTHETIC scenarios; no P&L here means anything."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from datetime import date, time
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from project100c.broker import OrderType
from project100c.calendar.events import load_event_book
from project100c.core_types import OptionRight, OrderSide
from project100c.paper import PaperKernel, PaperLoop, PaperRun
from project100c.sessions import IST
from project100c.spec.models import Lifecycle
from project100c.synthetic import DayPlan, Segment
from tests.data.dhan_fakes import CONFIGS
from tests.strategies.library_rig import spec
from tests.strategies.test_library import SCENARIOS

LIBRARY = ("S-GAPGO-001", "S-GAPFADE-001", "S-FBO-001", "S-VWAPC-001", "S-VWAPMR-001", "S-VOLX-001", "S-IVRV-001",
           "S-EXP0-001", "S-VIXSTR-001", "S-EVTBO-001", "S-LUNCH-001")  # fmt: skip
NAV = Decimal(1_000_000)  # large enough that a 1-lot NIFTY option fits 2% of NAV (mechanics, not the canary NAV)


@cache
def kernel() -> PaperKernel:
    return PaperKernel.load(CONFIGS)


def loop(tmp: Path, ids: tuple[str, ...] = LIBRARY, nav: Decimal = NAV, **kw: object) -> PaperLoop:
    return PaperLoop(kernel(), [spec(i) for i in ids], nav=nav, workdir=tmp, **kw)  # type: ignore[arg-type]


def run(tmp: Path, scenario: str, **kw: object) -> tuple[PaperLoop, PaperRun]:
    lp = loop(tmp, **kw)  # type: ignore[arg-type]
    return lp, lp.run(SCENARIOS[scenario][1])


def assert_sound(lp: PaperLoop, r: PaperRun) -> None:
    """Flat at the end, journal intact, never short, entries in the window, every filled entry protected."""
    assert r.journal_verified
    held: dict[str, int] = defaultdict(int)
    for f in sorted(lp.broker.trades(), key=lambda x: x.ts):
        held[f.instrument_key] += f.qty if f.side is OrderSide.BUY else -f.qty
        assert held[f.instrument_key] >= 0
        t = f.ts.astimezone(IST).time()
        assert t <= time(15, 0)
        if f.side is OrderSide.BUY:
            assert time(9, 20) <= t <= time(14, 3)
    orders = lp.broker.orders()
    assert all(o.request.order_type is OrderType.LIMIT for o in orders if o.request.side is OrderSide.BUY)
    for key in {f.instrument_key for f in lp.broker.trades() if f.side is OrderSide.BUY}:
        assert any(o.request.instrument_key == key and o.request.order_type is OrderType.SL for o in orders), key
    for d in r.days:
        assert d.flat_at_end and not d.kills and not d.halts


def test_a_gap_day_trades_gap_and_go_and_refuses_a_disallowed_regime(tmp_path: Path) -> None:
    lp, r = run(tmp_path, "gap-go")
    assert_sound(lp, r)
    (d,) = r.days
    approved = [x for x in d.decisions if x.outcome == "APPROVED"]
    assert [x.strategy_id for x in approved] == ["S-GAPGO-001"]
    (t,) = d.trades
    assert t.strategy_id == "S-GAPGO-001" and t.exit_reason == "TARGET" and t.filled
    refused = [x for x in d.decisions if x.strategy_id == "S-VWAPMR-001"]
    assert refused and all(x.outcome == "ALLOCATOR_REFUSED" for x in refused if x.outcome != "SKIPPED")
    assert any("REGIME_BLOCKED" in x.reasons[0] and "GAP_REGIME" in x.reasons[0] for x in refused)
    assert d.realised == t.net_pnl  # one trade: the kernel's realised P&L is the trade's
    assert d.eod_nav == NAV + d.realised and d.fills == 2
    assert r.stage is Lifecycle.PAPER and any("PAPER" in s for s in r.labels) and "SIMULATED" in r.labels[0]


def test_the_governor_limits_bind_in_paper(tmp_path: Path) -> None:
    lp, r = run(tmp_path, "vix-straddle")
    assert_sound(lp, r)
    rejected = [x for x in r.days[0].decisions if x.outcome == "GOVERNOR_REJECTED"]
    assert any("MAX_POSITION" in x.reasons for x in rejected)  # the 1-lot cap binds on the IV-RV leg
    approved = [x for x in r.days[0].decisions if x.outcome == "APPROVED"]
    assert len(approved) <= 10  # OD-014: the system-wide cap of 10 entries a day
    caps = {s.id: s.entry.max_entries_per_day for s in lp.specs}
    for sid in {x.strategy_id for x in approved}:  # OD-014: each spec's own daily cap
        assert sum(x.strategy_id == sid for x in approved) <= caps[sid], sid


def test_a_lower_system_cap_binds_in_paper_before_the_spec_caps(tmp_path: Path) -> None:
    """OD-014's system cap is configurable: at 3 the book stops at 3 entries even though the specs allow more."""
    k = kernel()
    low = dataclasses.replace(k, limits=k.limits.model_copy(update={"max_trades_per_day": 3}))
    lp = PaperLoop(low, [spec(i) for i in LIBRARY], nav=NAV, workdir=tmp_path)
    r = lp.run(SCENARIOS["vix-straddle"][1])
    assert_sound(lp, r)
    d = r.days[0]
    assert lp.rt.state.entries_today <= 3
    refused = [x for x in d.decisions if x.outcome in ("ALLOCATOR_REFUSED", "GOVERNOR_REJECTED")]
    assert any("MAX_ENTRIES_BOOK" in " ".join(x.reasons) or "MAX_TRADES_PER_DAY" in x.reasons for x in refused)


def test_the_straddle_holds_both_legs_under_od_013(tmp_path: Path) -> None:
    lp, r = run(tmp_path, "vix-straddle", ids=("S-VIXSTR-001",))
    assert_sound(lp, r)
    (t,) = r.days[0].trades
    assert [lg.state for lg in t.legs] == ["CLOSED", "CLOSED"] and t.exit_reason != "LEG_REJECTED"
    assert {lg.contract.right for lg in t.legs} == {OptionRight.CE, OptionRight.PE}
    assert len({(lg.contract.expiry, lg.contract.strike) for lg in t.legs}) == 1  # a straddle: one expiry, one strike
    (dec,) = [x for x in r.days[0].decisions if x.outcome == "APPROVED"]
    assert dec.reasons == () and dec.risk_at_stop is not None and dec.risk_at_stop <= Decimal("0.02") * NAV
    # two ENTRY orders, one entry: the straddle used the whole spec cap of 1 and one of the book's 10
    assert lp.rt.state.entries_by_strategy == {"S-VIXSTR-001": 1} and lp.rt.state.entries_today == 1
    assert sum(1 for o in lp.broker.orders() if o.request.side is OrderSide.BUY) == 2


def test_at_the_canary_nav_nothing_fits_the_allocation(tmp_path: Path) -> None:
    lp, r = run(tmp_path, "gap-go", nav=Decimal(10_000))
    assert_sound(lp, r)
    d = r.days[0]
    assert d.fills == 0 and d.realised == 0
    gap = next(x for x in d.decisions if x.strategy_id == "S-GAPGO-001")
    assert gap.reasons == ("NO_TRADE_RISK_OVER_ALLOCATION",)
    assert gap.risk_at_stop is not None and gap.budget is not None and gap.risk_at_stop > gap.budget


def test_on_a_verified_event_day_only_certified_entries_could_pass(tmp_path: Path) -> None:
    ev = load_event_book(CONFIGS / "calendar" / "events.yaml").to_calendar(kernel().market_clock.calendar)
    ids = ("S-FBO-001", "S-VWAPC-001", "S-VWAPMR-001", "S-EVTBO-001")
    lp, r = run(tmp_path, "event-breakout", ids=ids, events=ev)  # Wed 7-Oct-2026: RBI MPC (verified)
    assert_sound(lp, r)
    d = r.days[0]
    assert d.event_day and d.fills == 0
    ev_dec = [x for x in d.decisions if x.strategy_id == "S-EVTBO-001" and x.outcome != "SKIPPED"]
    why = ("EVENT_DAY_NOT_CERTIFIED", "EVENT_DAY_NOT_CERTIFIED: S-EVTBO-001 is not event_certified")
    assert ev_dec and all(x.reasons == why for x in ev_dec)  # the spec decides, and nothing is certified yet
    others = [x for x in d.decisions if x.strategy_id != "S-EVTBO-001" and x.outcome != "SKIPPED"]
    assert others and all("EVENT_REGIME" in x.reasons[0] for x in others)


def test_repeated_signals_are_folded(tmp_path: Path) -> None:
    _, r = run(tmp_path, "gap-go")
    d = r.days[0]
    assert any(x.repeats > 1 for x in d.decisions)
    assert sum(x.repeats for x in d.decisions) > len(d.decisions)


def test_two_days_carry_the_nav_and_runs_are_deterministic(tmp_path: Path) -> None:
    plans = [
        DayPlan(date(2026, 10, 7), 11, gap_pct=1.0, segments=(Segment(30, 0.02, 12), Segment(345, 0, 12))),
        DayPlan(date(2026, 10, 8), 12, segments=(Segment(375, 0, 10, revert=0.3),)),
    ]
    a = loop(tmp_path / "a").run(plans)
    b = loop(tmp_path / "b").run(plans)
    assert a.days[1].sod_nav == a.days[0].eod_nav
    assert [t.as_json() for t in a.trades] == [t.as_json() for t in b.trades]
    assert [(x.at, x.strategy_id, x.outcome, x.reasons) for d in a.days for x in d.decisions] == [
        (x.at, x.strategy_id, x.outcome, x.reasons) for d in b.days for x in d.decisions
    ]
    assert a.config_versions["limits"].startswith("RL-") and a.config_versions["allocator"].startswith("AL-")


def test_bad_inputs_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nav"):
        loop(tmp_path, nav=Decimal(0))
    with pytest.raises(ValueError, match="duplicate"):
        loop(tmp_path, ids=("S-GAPGO-001", "S-GAPGO-001"))
    with pytest.raises(ValueError, match="plug-in"):
        loop(tmp_path, ids=("S-ORB-001",))
    with pytest.raises(ValueError, match="not a trading day"):
        loop(tmp_path).run([DayPlan(date(2026, 10, 10), 1)])  # a Saturday


def test_strike_fit_walks_otm_until_the_risk_fits_the_allocation(tmp_path: Path) -> None:
    """At 2L the gap-and-go strike needs more than its allocation (the day's 4% shared by the eligible strategies);
    with strike_fit_steps the loop walks further OTM (single-leg only) and the Governor approves a smaller risk."""
    nav = Decimal(200_000)
    _, r0 = run(tmp_path / "a", "gap-go", nav=nav)
    assert any("NO_TRADE_RISK_OVER_ALLOCATION" in x.reasons for x in r0.days[0].decisions)
    lp, r = run(tmp_path / "b", "gap-go", nav=nav, strike_fit_steps=12)
    assert_sound(lp, r)
    ok = [x for x in r.days[0].decisions if x.outcome == "APPROVED"]
    (x,) = ok
    assert x.risk_at_stop is not None and x.budget is not None and x.risk_at_stop <= Decimal("0.008") * nav < x.budget
    with pytest.raises(ValueError):
        loop(tmp_path / "c", strike_fit_steps=41)

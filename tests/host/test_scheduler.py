"""The host's IST market-day scheduler: config-driven times, once per day, catch-up in order, holidays silent."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest

from project100c.ops.scheduler import DayEvent, MarketScheduler, ScheduleTimes
from project100c.paper.loop import PaperKernel
from project100c.sessions import IST

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
MON = date(2026, 10, 5)


def at(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(d, time(h, m, s), tzinfo=IST)


@pytest.fixture(scope="module")
def sched_clock() -> PaperKernel:
    return PaperKernel.load(CONFIGS)


def test_day_plan_uses_the_configured_window(sched_clock: PaperKernel) -> None:
    s = MarketScheduler(sched_clock.market_clock)
    plan = {e: t.time() for e, t in s.plan(MON)}
    assert plan == {
        DayEvent.TOKEN_REQUEST: time(8, 45),
        DayEvent.GATE_DEADLINE: time(9, 5),
        DayEvent.MARKET_OPEN: time(9, 15),
        DayEvent.ENTRIES_OPEN: time(9, 20),
        DayEvent.ENTRY_CUTOFF: time(14, 0),
        DayEvent.FLATTEN: time(14, 50),
        DayEvent.HARD_FLAT: time(15, 0),
        DayEvent.END_OF_DAY: time(15, 35),
    }
    assert [e for e, _ in s.plan(MON)] == list(DayEvent)


def test_each_event_fires_once_and_a_late_start_catches_up_in_order(sched_clock: PaperKernel) -> None:
    s = MarketScheduler(sched_clock.market_clock)
    assert s.due(at(MON, 8, 0)) == []
    assert s.due(at(MON, 8, 45)) == [DayEvent.TOKEN_REQUEST]
    assert s.due(at(MON, 8, 50)) == []
    assert s.due(at(MON, 9, 21)) == [DayEvent.GATE_DEADLINE, DayEvent.MARKET_OPEN, DayEvent.ENTRIES_OPEN]
    late = MarketScheduler(sched_clock.market_clock)  # a restart at 14:55 replays the morning in order
    assert late.due(at(MON, 14, 55)) == list(DayEvent)[:6]
    assert late.due(at(MON, 16, 0)) == [DayEvent.HARD_FLAT, DayEvent.END_OF_DAY]
    assert late.due(at(MON, 16, 1)) == []
    nxt = MON.replace(day=6)
    assert late.due(at(nxt, 8, 46)) == [DayEvent.TOKEN_REQUEST] and MON not in late.fired


def test_nothing_on_a_weekend_or_holiday(sched_clock: PaperKernel) -> None:
    s = MarketScheduler(sched_clock.market_clock)
    sat = date(2026, 10, 3)
    assert s.plan(sat) == [] and s.due(at(sat, 10, 0)) == [] and s.phase(at(sat, 10, 0)) == "NON_TRADING_DAY"
    assert s.next_event(at(sat, 10, 0)) is None


def test_phase_labels_and_next_event(sched_clock: PaperKernel) -> None:
    s = MarketScheduler(sched_clock.market_clock)
    assert s.phase(at(MON, 8, 0)) == "PRE_MARKET"
    assert s.phase(at(MON, 9, 0)) == "TOKEN_GATE"
    assert s.phase(at(MON, 9, 10)) == "BEFORE_WINDOW"
    assert s.phase(at(MON, 10, 0)) == "ENTRY_ALLOWED"
    assert s.phase(at(MON, 14, 30)) == "EXIT_ONLY"
    assert s.phase(at(MON, 14, 55)) == "FLATTENING"
    assert s.phase(at(MON, 15, 10)) == "CLOSED"
    assert s.phase(at(MON, 16, 0)) == "AFTER_HOURS"
    assert s.next_event(at(MON, 9, 6)) == (DayEvent.MARKET_OPEN, at(MON, 9, 15))
    assert s.next_event(at(MON, 15, 40)) is None


def test_inconsistent_times_are_refused(sched_clock: PaperKernel) -> None:
    with pytest.raises(ValueError, match="before the gate deadline"):
        ScheduleTimes(token_request=time(9, 10), gate_deadline=time(9, 5))
    with pytest.raises(ValueError, match="start of order activity"):
        MarketScheduler(sched_clock.market_clock, ScheduleTimes(time(9, 0), time(9, 30)))
    with pytest.raises(ValueError, match="end of order activity"):
        MarketScheduler(sched_clock.market_clock, ScheduleTimes(end_of_day=time(14, 0)))

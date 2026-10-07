"""The host's market-day scheduler (IST): which day event is due now. Venue-agnostic, no clock of its own.

The times come from the configs, not from this module: the trading window (``configs/sessions/trading_window.toml``:
order activity from 09:15, entries 09:20-14:00, forced flatten from 14:50, flat by 15:00) and the token gate's
deadline (09:05 by default, ``ops/daily_gate.py``). Only the token request time and the end-of-day housekeeping
time are the scheduler's own settings.

Events, once per trading day, in this order:

    TOKEN_REQUEST (08:45)  ask the broker for today's token (the gate starts)
    GATE_DEADLINE (09:05)  no approved token by now: no trading today (the gate lapses itself; this is the alert)
    MARKET_OPEN   (09:15)  order activity may start (exits and cancels only)
    ENTRIES_OPEN  (09:20)  new entries allowed
    ENTRY_CUTOFF  (14:00)  no new entries
    FLATTEN       (14:50)  the kernel's forced flatten begins
    HARD_FLAT     (15:00)  must be flat; no order activity at or after this
    END_OF_DAY    (15:35)  backup of the journal, daily report

``due(now)`` returns every event whose time has passed and that has not fired yet today, so a host that starts
late (or restarts) catches up in order; the caller acts on each. On a holiday or weekend nothing is due. The
kernel does not depend on this scheduler for safety: the Governor refuses entries outside the window and the
runtime flattens on its own clock. The scheduler drives the host's housekeeping around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum

from project100c.calendar import MarketClock
from project100c.sessions import IST


class DayEvent(StrEnum):
    TOKEN_REQUEST = "TOKEN_REQUEST"
    GATE_DEADLINE = "GATE_DEADLINE"
    MARKET_OPEN = "MARKET_OPEN"
    ENTRIES_OPEN = "ENTRIES_OPEN"
    ENTRY_CUTOFF = "ENTRY_CUTOFF"
    FLATTEN = "FLATTEN"
    HARD_FLAT = "HARD_FLAT"
    END_OF_DAY = "END_OF_DAY"


@dataclass(frozen=True, slots=True)
class ScheduleTimes:
    token_request: time = time(8, 45)
    gate_deadline: time = time(9, 5)
    end_of_day: time = time(15, 35)

    def __post_init__(self) -> None:
        if not self.token_request < self.gate_deadline:
            raise ValueError("the token request must come before the gate deadline")


@dataclass
class MarketScheduler:
    clock: MarketClock
    times: ScheduleTimes = field(default_factory=ScheduleTimes)
    fired: dict[date, list[DayEvent]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        w = self.clock.sessions.window
        if not self.times.gate_deadline <= w.order_activity_start:
            raise ValueError("the token gate deadline must be at or before the start of order activity")
        if not self.times.end_of_day >= w.order_activity_end:
            raise ValueError("end of day must be at or after the end of order activity")

    def plan(self, day: date) -> list[tuple[DayEvent, datetime]]:
        """The day's events with their IST times (empty on a non-trading day)."""
        noon = datetime.combine(day, time(12), tzinfo=IST)
        if not self.clock.is_trading_day(noon):
            return []
        w = self.clock.sessions.window
        at = {
            DayEvent.TOKEN_REQUEST: self.times.token_request,
            DayEvent.GATE_DEADLINE: self.times.gate_deadline,
            DayEvent.MARKET_OPEN: w.order_activity_start,
            DayEvent.ENTRIES_OPEN: w.entry_start,
            DayEvent.ENTRY_CUTOFF: w.entry_cutoff,
            DayEvent.FLATTEN: w.flatten_start,
            DayEvent.HARD_FLAT: w.hard_flat,
            DayEvent.END_OF_DAY: self.times.end_of_day,
        }
        return [(e, datetime.combine(day, at[e], tzinfo=IST)) for e in DayEvent]

    def due(self, now: datetime) -> list[DayEvent]:
        """Events now due (in order); each is returned once per trading day."""
        day = now.astimezone(IST).date()
        done = self.fired.setdefault(day, [])
        for old in [d for d in self.fired if d < day]:
            del self.fired[old]
        out = [e for e, t in self.plan(day) if t <= now and e not in done]
        done.extend(out)
        return out

    def next_event(self, now: datetime) -> tuple[DayEvent, datetime] | None:
        """The next event today, if any (for the status page)."""
        for e, t in self.plan(now.astimezone(IST).date()):
            if t > now:
                return e, t
        return None

    def phase(self, now: datetime) -> str:
        """A short label of where the day stands (for the status page)."""
        if not self.clock.is_trading_day(now):
            return "NON_TRADING_DAY"
        t = now.astimezone(IST).time()
        if t < self.times.token_request:
            return "PRE_MARKET"
        if t < self.times.gate_deadline:
            return "TOKEN_GATE"
        if t >= self.times.end_of_day:
            return "AFTER_HOURS"
        return str(self.clock.phase(now).value)

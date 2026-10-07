"""MarketClock: combines the trading calendar with the session model so callers never assert
'trading_day_confirmed' by hand."""

from __future__ import annotations

from datetime import datetime

from project100c.calendar.model import TradingCalendar
from project100c.errors import SessionError
from project100c.sessions.model import IST, SessionCalendar, WindowPhase


class MarketClock:
    def __init__(self, calendar: TradingCalendar, sessions: SessionCalendar) -> None:
        self._cal = calendar
        self._sessions = sessions

    @property
    def sessions(self) -> SessionCalendar:
        return self._sessions

    @property
    def calendar(self) -> TradingCalendar:
        return self._cal

    def is_trading_day(self, ts: datetime) -> bool:
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise SessionError("naive datetime: timestamps must be timezone-aware")
        return self._cal.is_trading_day(ts.astimezone(IST).date())

    def phase(self, ts: datetime) -> WindowPhase:
        """Window phase; non-trading days are CLOSED (no order activity at all)."""
        if not self.is_trading_day(ts):
            return WindowPhase.CLOSED
        return self._sessions.phase(ts, trading_day_confirmed=True)

    def order_activity_allowed(self, ts: datetime) -> bool:
        return self.phase(ts) not in (WindowPhase.BEFORE_WINDOW, WindowPhase.CLOSED)

    def entry_allowed(self, ts: datetime) -> bool:
        return self.phase(ts) is WindowPhase.ENTRY_ALLOWED

    def must_be_flat(self, ts: datetime) -> bool:
        if not self.is_trading_day(ts):
            return True
        return self._sessions.must_be_flat(ts, trading_day_confirmed=True)

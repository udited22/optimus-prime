"""Request pacing for vendor APIs: a sliding one-second window plus a persisted daily budget.

Time is injected (``clock`` = monotonic seconds, ``sleep``), so tests run on a simulated clock.
The daily budget is counted per IST calendar day in a ``QuotaStore`` (the job database), so a restart
cannot reset it. Every request attempt counts, including failed ones.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from project100c.errors import ConfigError, QuotaExhaustedError
from project100c.sessions.model import IST


class QuotaStore(Protocol):
    def requests_on(self, day: str) -> int: ...

    def add_request(self, day: str) -> int: ...


class MemoryQuotaStore:
    def __init__(self) -> None:
        self._n: dict[str, int] = {}

    def requests_on(self, day: str) -> int:
        return self._n.get(day, 0)

    def add_request(self, day: str) -> int:
        self._n[day] = self._n.get(day, 0) + 1
        return self._n[day]


class RateLimiter:
    def __init__(
        self,
        *,
        per_second: int,
        per_day: int,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        wall_clock: Callable[[], datetime],
        quota: QuotaStore,
    ) -> None:
        if per_second <= 0 or per_day <= 0:
            raise ConfigError("rate limits must be > 0")
        self._per_second = per_second
        self._per_day = per_day
        self._clock = clock
        self._sleep = sleep
        self._wall = wall_clock
        self._quota = quota
        self._recent: deque[float] = deque()
        self.slept_s = 0.0

    def _day(self) -> str:
        now = self._wall()
        if now.tzinfo is None:
            raise ConfigError("wall_clock must return timezone-aware datetimes")
        return now.astimezone(IST).date().isoformat()

    def acquire(self) -> None:
        """Block until a request may be sent, then count it. Raises QuotaExhaustedError at the daily budget."""
        day = self._day()
        if self._quota.requests_on(day) >= self._per_day:
            raise QuotaExhaustedError(
                f"daily request budget {self._per_day} used up for {day} (IST); the job is resumable tomorrow"
            )
        while True:
            now = self._clock()
            while self._recent and now - self._recent[0] >= 1.0:
                self._recent.popleft()
            if len(self._recent) < self._per_second:
                break
            wait = 1.0 - (now - self._recent[0])
            self.slept_s += wait
            self._sleep(wait)
        self._recent.append(self._clock())
        self._quota.add_request(day)

    def backoff(self, seconds: float) -> None:
        if seconds < 0:
            raise ConfigError("negative backoff")
        self.slept_s += seconds
        self._sleep(seconds)


class SimClock:
    """Deterministic monotonic clock for tests: ``sleep`` advances time."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

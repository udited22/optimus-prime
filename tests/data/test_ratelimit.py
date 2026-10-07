"""Rate limiter on a simulated clock: never more than N requests in any 1 s window; daily budget persists."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from project100c.data.dhan import JobStore
from project100c.data.ratelimit import MemoryQuotaStore, RateLimiter, SimClock
from project100c.errors import ConfigError, QuotaExhaustedError
from project100c.sessions import IST

WALL = datetime(2026, 10, 1, 10, 0, tzinfo=IST)


def _limiter(clock: SimClock, per_second: int = 4, per_day: int = 1000, quota: object | None = None) -> RateLimiter:
    return RateLimiter(
        per_second=per_second,
        per_day=per_day,
        clock=clock,
        sleep=clock.sleep,
        wall_clock=lambda: WALL,
        quota=quota or MemoryQuotaStore(),  # type: ignore[arg-type]
    )


def test_burst_of_50_is_paced() -> None:
    clock = SimClock()
    lim = _limiter(clock)
    sent: list[float] = []
    for _ in range(50):
        lim.acquire()
        sent.append(clock())
    for i in range(len(sent)):
        assert sum(1 for t in sent if sent[i] <= t < sent[i] + 1.0) <= 4
    assert sent[-1] >= 12.0 - 1e-9  # 50 requests at 4/s need at least 12 s


def test_daily_budget_persists_across_restart(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "j.sqlite", wall_clock=lambda: WALL)
    clock = SimClock()
    lim = _limiter(clock, per_day=5, quota=store)
    for _ in range(5):
        lim.acquire()
    with pytest.raises(QuotaExhaustedError, match="resumable"):
        lim.acquire()
    store.close()
    store2 = JobStore(tmp_path / "j.sqlite", wall_clock=lambda: WALL)  # "restart"
    with pytest.raises(QuotaExhaustedError):
        _limiter(SimClock(), per_day=5, quota=store2).acquire()
    assert store2.requests_on("2026-10-01") == 5


def test_bad_config_and_naive_clock() -> None:
    clock = SimClock()
    with pytest.raises(ConfigError):
        _limiter(clock, per_second=0)
    lim = RateLimiter(
        per_second=1, per_day=1, clock=clock, sleep=clock.sleep, wall_clock=datetime.now, quota=MemoryQuotaStore()
    )
    with pytest.raises(ConfigError):
        lim.acquire()
    with pytest.raises(ConfigError):
        _limiter(clock).backoff(-1)

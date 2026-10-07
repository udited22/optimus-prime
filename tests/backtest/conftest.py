from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.backtest import BacktestEngine, BarFillModel, LatencyModel, MarketView, StrategyContext
from project100c.backtest.engine import BacktestConfig
from project100c.backtest.types import Intent
from project100c.calendar import MarketClock, TradingCalendar, load_holiday_book
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book
from project100c.market_types import Bar
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows

DAY = date(2026, 9, 15)  # a Tuesday, regular trading day
KEY = "NSE_FO|40001"
ONE_MIN = timedelta(minutes=1)


def at(h: int, m: int, d: date = DAY) -> datetime:
    return datetime.combine(d, time(h, m), IST)


def day_bars(
    price: Callable[[int], Decimal] = lambda i: Decimal("100"),
    volume: Callable[[int], int] = lambda i: 6500,
    d: date = DAY,
    key: str = KEY,
) -> list[Bar]:
    out = []
    t = at(9, 15, d)
    i = 0
    while t < at(15, 40, d):
        p = price(i)
        out.append(Bar(key, t, p, p + Decimal("1"), p - Decimal("1"), p, volume(i)))
        t += ONE_MIN
        i += 1
    return out


@pytest.fixture(scope="session")
def mclock(configs_dir: Path) -> MarketClock:
    return MarketClock(
        TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml")),
        SessionCalendar(
            load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml"),
            load_trading_windows(configs_dir / "sessions" / "trading_window.toml"),
        ),
    )


@pytest.fixture(scope="session")
def costs(configs_dir: Path) -> CostModel:
    return CostModel(
        load_charge_book(configs_dir / "costs" / "nse_fo_index_options.toml"),
        load_brokerage_plans(configs_dir / "costs" / "brokerage_plans.toml"),
    )


@pytest.fixture
def engine_factory(mclock: MarketClock, costs: CostModel) -> Callable[..., BacktestEngine]:
    def make(latency: LatencyModel | None = None, **cfg: object) -> BacktestEngine:
        return BacktestEngine(
            clock=mclock,
            costs=costs,
            bar_model=BarFillModel(interval=ONE_MIN),
            latency=latency or LatencyModel(),
            config=BacktestConfig(**cfg),  # type: ignore[arg-type]
        )

    return make


class Scripted:
    """Strategy that emits scripted intents at given decision times and records what it saw."""

    name = "scripted"

    def __init__(self, script: Mapping[datetime, Sequence[Intent]]) -> None:
        self.script = dict(script)
        self.seen_fills_at: list[tuple[datetime, int]] = []

    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
        assert ctx.now is not None
        self.seen_fills_at.append((ctx.now, len(ctx.fills)))
        return self.script.pop(ctx.now, ())

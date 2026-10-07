from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from project100c.calendar import MarketClock, TradingCalendar, load_holiday_book
from project100c.core_types import OptionRight, OrderSide
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book
from project100c.costs.config import default_live_plan_id
from project100c.instruments import Contract, InstrumentKind
from project100c.journal import JournalRecord
from project100c.kernel.governor import MarketSnapshot, RiskGovernor, TradeIntent
from project100c.kernel.limits import RiskLimits, load_risk_limits
from project100c.kernel.state import KernelState, apply
from project100c.market_types import Quote
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows
from project100c.spec.models import Lifecycle

DAY = date(2026, 10, 5)  # Monday, NSE trading day
KEY = "NSE_FO|NIFTY06OCT2625000CE"


def ist(hh: int, mm: int, ss: int = 0, d: date = DAY) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)


@pytest.fixture(scope="session")
def market_clock(configs_dir: Path) -> MarketClock:
    cal = TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml"))
    s = SessionCalendar(
        load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(configs_dir / "sessions" / "trading_window.toml"),
    )
    return MarketClock(cal, s)


@pytest.fixture(scope="session")
def costs(configs_dir: Path) -> CostModel:
    c = configs_dir / "costs"
    return CostModel(
        load_charge_book(c / "nse_fo_index_options.toml"), load_brokerage_plans(c / "brokerage_plans.toml")
    )


@pytest.fixture(scope="session")
def plan_id(configs_dir: Path) -> str:
    return default_live_plan_id(configs_dir / "costs" / "brokerage_plans.toml")


@pytest.fixture(scope="session")
def limits(configs_dir: Path) -> RiskLimits:
    return load_risk_limits(configs_dir / "risk" / "limits.toml")


@pytest.fixture(scope="session")
def gov(limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str) -> RiskGovernor:
    return RiskGovernor(limits, market_clock, costs, plan_id)


def contract(key: str = KEY, lot: int = 65) -> Contract:
    return Contract(
        "test",
        key,
        "1",
        "NIFTY",
        InstrumentKind.OPTION,
        OptionRight.CE,
        date(2026, 10, 6),
        D(25000),
        lot,
        D("0.05"),
        1800,
        "NIFTY 25000 CE",
    )


def intent(**kw: Any) -> TradeIntent:
    base = TradeIntent(
        intent_id="I1",
        strategy_id="S-ORB-001",
        strategy_status=Lifecycle.CANARY,
        contract=contract(),
        side=OrderSide.BUY,
        qty=65,
        limit_price=D("3.00"),
        decided_at=ist(10, 0),
        stop_trigger=D("2.30"),
        stop_limit=D("2.25"),
        spec_stop_limit=D("2.25"),
    )
    return replace(base, **kw)


def quote(
    bid: str = "2.95",
    ask: str = "3.00",
    *,
    ts: datetime | None = None,
    key: str = KEY,
    qty: int = 1000,
    oi: int | None = 100000,
) -> Quote:
    t = ts or ist(10, 0)
    return Quote(key, t, t, D(bid), D(ask), qty, qty, D(ask), oi)


def market(now: datetime | None = None, *, cash: str = "10000", q: Quote | None = None, **kw: Any) -> MarketSnapshot:
    n = now or ist(10, 0)
    return MarketSnapshot(now=n, quote=q if q is not None else quote(ts=n), available_cash=D(cash), **kw)


class Events:
    """Build KernelState through the real reducer from synthetic journal records."""

    def __init__(self) -> None:
        self.seq = 0
        self.state = KernelState()

    def add(self, et: str, ts: datetime | None = None, **payload: Any) -> Events:
        self.seq += 1
        self.state = apply(self.state, JournalRecord(self.seq, ts or ist(9, 16), et, "", payload, "h"))
        return self


def day(nav: str = "10000", sow: str | None = None) -> Events:
    return Events().add("DAY_START", trading_date=DAY, sod_nav=D(nav), sow_nav=D(sow or nav), week_start=True)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def adv(self, s: float) -> None:
        self.t += timedelta(seconds=s)


ClockT = Callable[[], datetime]

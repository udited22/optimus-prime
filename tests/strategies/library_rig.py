"""Shared wiring for the strategy-library mechanics tests: seeded SYNTHETIC days -> synthetic chain -> engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from decimal import Decimal
from functools import cache
from typing import Any

from project100c.backtest import BacktestEngine, BarFillModel, LatencyModel, RunResult
from project100c.backtest.engine import BacktestConfig
from project100c.backtest.feed import ReplayFeed
from project100c.calendar import ExpiryCalendar, MarketClock, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book
from project100c.kernel.limits import load_risk_limits
from project100c.regime import EventCalendar, RegimeConfig, load_regime_config
from project100c.sessions import SessionCalendar, load_exchange_sessions, load_trading_windows
from project100c.spec.io import load_spec_file
from project100c.spec.models import StrategySpec
from project100c.strategies.library import PLUGINS, LongOptionStrategy, library_metadata
from project100c.synthetic import (
    FUT_KEY,
    INDEX_KEY,
    VIX_KEY,
    DayPlan,
    SyntheticDay,
    generate_chain,
    generate_days,
    merge_chains,
)
from tests.data.dhan_fakes import CONFIGS, REPO

ONE_MIN = timedelta(minutes=1)
SPECS = REPO / "specs"


@dataclass(frozen=True)
class Rig:
    clock: MarketClock
    costs: CostModel
    expiries: ExpiryCalendar
    regime: RegimeConfig


@cache
def rig() -> Rig:
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    clock = MarketClock(
        cal,
        SessionCalendar(
            load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
            load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
        ),
    )
    costs = CostModel(
        load_charge_book(CONFIGS / "costs" / "nse_fo_index_options.toml"),
        load_brokerage_plans(CONFIGS / "costs" / "brokerage_plans.toml"),
    )
    exp = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    return Rig(clock, costs, exp, load_regime_config(CONFIGS / "regime" / "classifier.toml"))


def spec(spec_id: str) -> StrategySpec:
    return load_spec_file(SPECS / f"{spec_id}.yaml")


@dataclass
class LibRun:
    result: RunResult
    strategy: LongOptionStrategy
    days: list[SyntheticDay]

    @property
    def trades(self) -> list[dict[str, Any]]:
        return self.strategy.trades


def run(
    spec_id: str,
    plans: Sequence[DayPlan],
    *,
    nav: Decimal = Decimal(1_000_000),
    gate: bool = True,
    max_lots: int | None = None,  # None: the shipped per-strategy cap (limits.toml strategy_max_lots)
    events: EventCalendar | None = None,
    chain_last_only: bool = False,
    hands_off_from: time = time(14, 50),
    index_key: str = INDEX_KEY,  # relabel the index bars (feed-ordering regression tests)
    decision_keys: Sequence[str] = (),
    daily_features: Mapping[date, Mapping[str, Decimal]] | None = None,
    drop_option_bars: Callable[[Any], bool] | None = None,  # drop chain bars (e.g. no prints into the flatten window)
) -> LibRun:
    r = rig()
    sp = spec(spec_id)
    if max_lots is None:
        max_lots = load_risk_limits(CONFIGS / "risk" / "limits.toml").lot_cap(spec_id)
    days = generate_days(plans)
    first, last = days[0].plan.day, days[-1].plan.day
    exps = [e.date for e in r.expiries.expiries_between(first, last + timedelta(days=21))]
    chain_days = days[-1:] if chain_last_only else days
    chain = merge_chains(generate_chain(d, exps) for d in chain_days)
    idx = [b if index_key == INDEX_KEY else replace(b, instrument_key=index_key) for d in days for b in d.index]
    opt_bars = [b for b in chain.bars if drop_option_bars is None or not drop_option_bars(b)]
    bars = [b for d in days for b in (*d.vix, *d.fut)] + idx + opt_bars
    strat = LongOptionStrategy(
        sp,
        PLUGINS[spec_id](),
        index_key=index_key,
        fut_key=FUT_KEY,
        vix_key=VIX_KEY,
        contracts=chain.contracts,
        costs=r.costs,
        plan_id=sp.cost_assumptions.brokerage_plan,
        risk_budget=Decimal("0.02") * nav,
        regime=r.regime,
        expiries=r.expiries,
        events=events,
        gate_regime=gate,
        prev_close=Decimal(repr(plans[0].prev_close)),
        hands_off_from=hands_off_from,
        daily_features=daily_features,
    )
    engine = BacktestEngine(
        clock=r.clock,
        costs=r.costs,
        bar_model=BarFillModel(interval=ONE_MIN),
        latency=LatencyModel(),
        config=BacktestConfig(plan_id=sp.cost_assumptions.brokerage_plan, starting_cash=nav, max_lots=max_lots),
    )
    meta = {"strategy": library_metadata(strat), "data": "SYNTHETIC: seeded DayPlans, Black-Scholes chain"}
    res = engine.run(ReplayFeed.from_bars(bars, interval=ONE_MIN, decision_keys=decision_keys), strat, metadata=meta)
    return LibRun(res, strat, days)

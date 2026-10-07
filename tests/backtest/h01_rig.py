"""Shared H01 end-to-end wiring for tests: fixture lake -> DhanLakeReader -> spread model -> engine -> S-ORB-001."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from project100c.backtest import BacktestEngine, BarFillModel, LatencyModel, RunResult
from project100c.backtest.engine import BacktestConfig
from project100c.backtest.lake_source import DhanLakeReader, LakeData
from project100c.backtest.spreads import SyntheticSpreadModel, SyntheticSpreadProvider, load_spread_params
from project100c.calendar import MarketClock, TradingCalendar, load_holiday_book
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book
from project100c.kernel.limits import load_risk_limits
from project100c.sessions import SessionCalendar, load_exchange_sessions, load_trading_windows
from project100c.spec.checks import check_cost_reference
from project100c.spec.io import load_spec_file
from project100c.strategies.orb_h01 import OrbH01Strategy, OrbParams, h01_metadata
from tests.backtest.lake_fixture import (
    FUT_LABEL,
    INDEX_LABEL,
    VIX_LABEL,
    FixtureLake,
    build,
    candle_specs,
    expiry_calendar,
    lot_history,
    rolling_spec,
)
from tests.data.dhan_fakes import CONFIGS, REPO

SPEC = REPO / "tests" / "fixtures" / "spec" / "S-ORB-001.yaml"
WARMUP_FROM = date(2026, 7, 1)  # futures/VIX history for the 20-day volume median
OPT_FROM, TO_EXCL = date(2026, 8, 3), date(2026, 8, 22)
ONE_MIN = timedelta(minutes=1)


def build_lake(tmp: Path) -> FixtureLake:
    rolling = rolling_spec(from_date=OPT_FROM, to_date=TO_EXCL, strike_offsets=(-2, -1, 0, 1, 2))
    return build(tmp, [rolling], candle_specs(WARMUP_FROM, TO_EXCL))


@dataclass
class H01Run:
    result: RunResult
    strategy: OrbH01Strategy
    data: LakeData
    spread: SyntheticSpreadProvider


def run_h01(fx: FixtureLake, *, nav: Decimal) -> H01Run:
    spec = load_spec_file(SPEC)
    book = load_charge_book(CONFIGS / "costs" / "nse_fo_index_options.toml")
    plans = load_brokerage_plans(CONFIGS / "costs" / "brokerage_plans.toml")
    check_cost_reference(spec, book, plans)
    costs = CostModel(book, plans)
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    clock = MarketClock(
        cal,
        SessionCalendar(
            load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
            load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
        ),
    )
    reader = DhanLakeReader(fx.rig.lake, config=fx.config, expiries=expiry_calendar(), lots=lot_history())
    data = reader.load(
        option_jobs=fx.rolling, candle_jobs=fx.candles, date_from=WARMUP_FROM, date_to=TO_EXCL - timedelta(days=1)
    )
    spread = SyntheticSpreadProvider(
        SyntheticSpreadModel(load_spread_params(CONFIGS / "backtest" / "synthetic_spreads.toml")),
        data.contracts,
        data.spot_at,
        cal,
        fx.config.nifty_strike_step,
    )
    limits = load_risk_limits(CONFIGS / "risk" / "limits.toml")
    params = OrbParams.from_spec(spec)
    strat = OrbH01Strategy(
        params,
        fut_key=FUT_LABEL,
        index_key=INDEX_LABEL,
        vix_key=VIX_LABEL,
        contracts=data.contracts,
        costs=costs,
        plan_id=spec.cost_assumptions.brokerage_plan,
        risk_budget=limits.per_trade_max_loss_frac * nav,
    )
    engine = BacktestEngine(
        clock=clock,
        costs=costs,
        bar_model=BarFillModel(interval=ONE_MIN, spread=spread),
        latency=LatencyModel(),
        config=BacktestConfig(plan_id=spec.cost_assumptions.brokerage_plan, starting_cash=nav),
    )
    meta = {
        "strategy": h01_metadata(params),
        "data": data.metadata(),
        "risk": {"limits": limits.version, "per_trade_max_loss_frac": limits.per_trade_max_loss_frac, "nav": nav},
        "cost_model": spec.cost_assumptions.cost_model_version,
        "calendar": cal.version,
        "synthetic_prices": "FIXTURE: fake Dhan server, generated values, not market data",
    }
    result = engine.run(data.feed(interval=ONE_MIN), strat, metadata=meta)
    return H01Run(result, strat, data, spread)

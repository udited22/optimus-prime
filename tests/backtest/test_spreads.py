"""B-02 synthetic spread model (ASSUMED): parameter validation, the estimate formula, conservative monotonicity,
the fill-model hook, and that runs using it carry the ASSUMED label in the ledger."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from project100c.backtest import BacktestEngine, BarFillModel, PlaceOrder, ReplayFeed, SimOrder, SimOrderType
from project100c.backtest.engine import BacktestConfig
from project100c.backtest.spreads import (
    SPREAD_STATUS_ASSUMED,
    SyntheticSpreadModel,
    SyntheticSpreadParams,
    SyntheticSpreadProvider,
    load_spread_params,
)
from project100c.backtest.types import OptionContract
from project100c.calendar import MarketClock, TradingCalendar, load_holiday_book
from project100c.core_types import OptionRight, OrderSide
from project100c.costs import CostModel
from project100c.errors import ConfigError, SpreadModelError
from project100c.market_types import Bar
from project100c.sessions import IST
from tests.backtest.conftest import DAY, KEY, ONE_MIN, Scripted, at, day_bars

D = Decimal


@pytest.fixture(scope="module")
def params(configs_dir: Path) -> SyntheticSpreadParams:
    return load_spread_params(configs_dir / "backtest" / "synthetic_spreads.toml")


@pytest.fixture(scope="module")
def model(params: SyntheticSpreadParams) -> SyntheticSpreadModel:
    return SyntheticSpreadModel(params)


def _raw(params: SyntheticSpreadParams) -> dict[str, Any]:
    return params.model_dump(mode="json")


def test_config_is_labelled_assumed(params: SyntheticSpreadParams) -> None:
    assert params.status == SPREAD_STATUS_ASSUMED
    assert params.version.startswith("SPREAD-ASSUMED-")
    assert "No measurement" in params.basis


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(status="MEASURED"),  # no calibration path yet
        lambda r: r["dte"][0].update(multiplier="0.9"),  # a band may never narrow the base
        lambda r: r["time_of_day"][1].update(start="09:31"),  # gap
        lambda r: r["time_of_day"][-1].update(end="15:29"),  # does not reach the cash close
        lambda r: r["moneyness"][-1].update(max_steps=20),  # last band must be open-ended
        lambda r: r["moneyness"][1].update(max_steps=1),  # not ascending
        lambda r: r.update(premium_pct="0.5", max_spread_pct="0.25"),
        lambda r: r.update(tick_size="0"),
    ],
)
def test_invalid_params_refused(params: SyntheticSpreadParams, mutate: Callable[[dict[str, Any]], None]) -> None:
    raw = _raw(params)
    mutate(raw)
    with pytest.raises(ValueError):
        SyntheticSpreadParams.model_validate(raw)


def test_loader_refuses_non_assumed_file(tmp_path: Path, configs_dir: Path) -> None:
    text = (configs_dir / "backtest" / "synthetic_spreads.toml").read_text()
    p = tmp_path / "s.toml"
    p.write_text(text.replace('status = "ASSUMED"', 'status = "CALIBRATED"'))
    with pytest.raises(ConfigError):
        load_spread_params(p)


def _t(h: int, m: int) -> datetime:
    return datetime.combine(DAY, time(h, m), IST)


@pytest.mark.parametrize(
    ("premium", "hm", "dte", "steps", "spread", "half", "capped"),
    [
        ("100", (11, 0), 3, 0, "1.00", "0.50", False),  # 1% of premium, all multipliers 1
        ("5", (11, 0), 3, 0, "0.10", "0.05", False),  # floor: 2 ticks
        ("100", (9, 20), 0, 7, "5.25", "2.65", False),  # 1 x 2.0 x 1.5 x 1.75; half rounded UP to a tick
        ("2", (9, 15), 0, 20, "0.50", "0.25", True),  # 0.10 x 2 x 1.5 x 2.5 = 0.75 capped at 25% of 2
        ("63.35", (15, 10), 10, 4, "1.50", "0.75", False),  # 0.6335 x 1.5 x 1.25 x 1.25 = 1.4848 -> 30 ticks
    ],
)
def test_estimate_formula(
    model: SyntheticSpreadModel,
    premium: str,
    hm: tuple[int, int],
    dte: int,
    steps: int,
    spread: str,
    half: str,
    capped: bool,
) -> None:
    e = model.estimate(D(premium), _t(*hm), dte, steps)
    p = model.params
    base = max(p.min_spread_ticks * p.tick_size, p.premium_pct * D(premium))
    raw = base * e.m_time * e.m_dte * e.m_moneyness
    expect_ticks = math.ceil(
        min(raw, max(p.min_spread_ticks * p.tick_size, p.max_spread_pct * D(premium))) / p.tick_size
    )
    assert e.spread == expect_ticks * p.tick_size  # formula restated independently
    assert (e.spread, e.half_spread, e.capped) == (D(spread), D(half), capped)
    assert e.status == SPREAD_STATUS_ASSUMED and e.version == p.version
    assert e.half_spread * 2 >= e.spread and e.half_spread % p.tick_size == 0


def test_widening_any_dimension_never_narrows(model: SyntheticSpreadModel) -> None:
    times = [_t(9, 15), _t(9, 29), _t(9, 30), _t(12, 0), _t(15, 0), _t(15, 29), _t(15, 39)]
    for prem, t, dte, steps in itertools.product([D("0.5"), D(20), D(150)], times, [0, 1, 6, 7, 30], [0, 3, 6, 11]):
        e = model.estimate(prem, t, dte, steps)
        assert e.spread >= 2 * model.params.tick_size
        assert model.estimate(prem, t, dte, steps + 5).spread >= e.spread
        if dte > 0:
            assert model.estimate(prem, t, 0, steps).spread >= e.spread  # expiry day is never tighter
        assert model.estimate(prem * 2, t, dte, steps).spread >= e.spread
        assert e.spread >= model.estimate(prem, _t(12, 0), dte, steps).spread  # no band tighter than midday


def test_estimate_refuses_bad_inputs(model: SyntheticSpreadModel) -> None:
    with pytest.raises(SpreadModelError):
        model.estimate(D(10), _t(15, 40), 1, 0)  # outside every band: no silent default
    with pytest.raises(SpreadModelError):
        model.estimate(D(10), _t(9, 14), 1, 0)
    with pytest.raises(SpreadModelError):
        model.estimate(D(0), _t(11, 0), 1, 0)
    with pytest.raises(SpreadModelError):
        model.estimate(D(10), _t(11, 0), -1, 0)


class Fixed:
    def __init__(self, h: str) -> None:
        self.h = D(h)

    def half_spread(self, instrument_key: str, bar: Bar) -> Decimal:
        return self.h

    def describe(self) -> dict[str, object]:
        return {"spread_model": "FIXED-TEST", "spread_status": SPREAD_STATUS_ASSUMED}


def _order(side: OrderSide, lim: str, typ: SimOrderType = SimOrderType.LIMIT, trig: str | None = None) -> SimOrder:
    spec = PlaceOrder(KEY, "NIFTY", OptionRight.CE, side, 65, 65, typ, D(lim), None if trig is None else D(trig))
    return SimOrder("O1", spec, at(10, 0), at(10, 0))


def _bar(o: str, h: str, lo: str, c: str) -> Bar:
    return Bar(KEY, at(10, 1), D(o), D(h), D(lo), D(c), 6500)


PLAIN = BarFillModel(interval=ONE_MIN)
WIDE = BarFillModel(interval=ONE_MIN, spread=Fixed("0.50"))


def test_buy_must_clear_limit_by_half_spread() -> None:
    o = _order(OrderSide.BUY, "100")
    shallow = _bar("101", "102", "99.60", "101")
    assert PLAIN.match(o, shallow).qty == 65  # type: ignore[union-attr]
    m = WIDE.match(o, shallow)
    assert m is not None and m.qty == 0 and m.half_spread == D("0.50")
    assert WIDE.match(o, _bar("101", "102", "99.50", "101")).qty == 0  # type: ignore[union-attr]  # equal: no
    deep = WIDE.match(o, _bar("101", "102", "99.45", "101"))
    assert deep is not None and deep.qty == 65 and deep.price == D("100")  # still no price improvement


def test_sell_must_clear_limit_by_half_spread() -> None:
    o = _order(OrderSide.SELL, "100")
    assert PLAIN.match(o, _bar("99", "100.40", "98", "99")).qty == 65  # type: ignore[union-attr]
    assert WIDE.match(o, _bar("99", "100.40", "98", "99")).qty == 0  # type: ignore[union-attr]
    assert WIDE.match(o, _bar("99", "100.55", "98", "99")).qty == 65  # type: ignore[union-attr]


def test_stop_sell_gap_includes_half_spread() -> None:
    o = _order(OrderSide.SELL, "69.80", SimOrderType.SL_LIMIT, "70")
    bar = _bar("70.10", "70.20", "69", "69.5")  # opens inside the band: plain model fills at the limit
    assert PLAIN.match(o, bar).qty == 65  # type: ignore[union-attr]
    m = WIDE.match(o, bar)  # 70.10 - 0.50 < 69.80: treated as gapped through the band
    assert m is not None and m.triggered and m.qty == 0


def test_negative_half_spread_is_an_error() -> None:
    with pytest.raises(Exception, match="negative half-spread"):
        BarFillModel(interval=ONE_MIN, spread=Fixed("-0.05")).match(
            _order(OrderSide.BUY, "100"), _bar("1", "2", "1", "1")
        )


@pytest.fixture(scope="module")
def cal(configs_dir: Path) -> TradingCalendar:
    return TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml"))


def _provider(model: SyntheticSpreadModel, cal: TradingCalendar, spot: Decimal | None) -> SyntheticSpreadProvider:
    c = OptionContract(KEY, "NIFTY", date(2026, 9, 22), D(25000), OptionRight.CE, 65)
    return SyntheticSpreadProvider(model, {KEY: c}, lambda k, t: spot, cal, D(50))


def test_provider_uses_contract_spot_and_calendar(model: SyntheticSpreadModel, cal: TradingCalendar) -> None:
    p = _provider(model, cal, D(24790))  # 210 points away -> 4 steps (rounded) -> 1.25
    bar = Bar(KEY, at(11, 0), D(100), D(100), D(99), D(99), 100)
    e = p.estimate(KEY, bar)
    assert p.dte(DAY, date(2026, 9, 22)) == 5  # 16,17,18,21,22 Sep 2026 (no holidays that week)
    assert (e.m_time, e.m_dte, e.m_moneyness) == (D(1), D(1), D("1.25"))
    assert e.half_spread == D("0.65")  # 1.00 x 1.25 = 1.25 -> 25 ticks -> half 13 ticks
    assert p.estimates == 1


def test_provider_refuses_missing_context(model: SyntheticSpreadModel, cal: TradingCalendar) -> None:
    bar = Bar(KEY, at(11, 0), D(100), D(100), D(99), D(99), 100)
    with pytest.raises(SpreadModelError, match="no underlying spot"):
        _provider(model, cal, None).half_spread(KEY, bar)
    with pytest.raises(SpreadModelError, match="no contract terms"):
        _provider(model, cal, D(25000)).half_spread("OTHER", bar)
    late = Bar(KEY, at(11, 0, date(2026, 9, 23)), D(100), D(100), D(99), D(99), 100)
    with pytest.raises(SpreadModelError, match="after the contract expiry"):
        _provider(model, cal, D(25000)).half_spread(KEY, late)


def test_run_with_spread_model_is_labelled_assumed(
    mclock: MarketClock, costs: CostModel, model: SyntheticSpreadModel, cal: TradingCalendar
) -> None:
    eng = BacktestEngine(
        clock=mclock,
        costs=costs,
        bar_model=BarFillModel(interval=ONE_MIN, spread=_provider(model, cal, D(25000))),
        config=BacktestConfig(),
    )
    buy = PlaceOrder(KEY, "NIFTY", OptionRight.CE, OrderSide.BUY, 65, 65, SimOrderType.LIMIT, D("100"))
    bars = day_bars()  # high 101 -> spread 1.01 -> 21 ticks -> half 11 ticks = 0.55; low 99 clears 100 by 1.00
    r = eng.run(ReplayFeed.from_bars(bars, interval=ONE_MIN), Scripted({at(10, 1): [buy]}))
    start = r.ledger.entries[0]
    assert start["fill_model"]["spread_model"] == model.version
    assert start["fill_model"]["spread_status"] == "ASSUMED"
    fill = next(e for e in r.ledger.entries if e["kind"] == "FILL")
    assert fill["half_spread"] == D("0.55") and r.fills[0].half_spread == D("0.55")
    # the same run without the spread model is a different (hash-distinguishable) experiment
    plain = BacktestEngine(clock=mclock, costs=costs, bar_model=BarFillModel(interval=ONE_MIN))
    r2 = plain.run(ReplayFeed.from_bars(bars, interval=ONE_MIN), Scripted({at(10, 1): [buy]}))
    assert r2.ledger_hash != r.ledger_hash and r2.ledger.entries[0]["fill_model"]["spread_model"] is None
    assert timedelta(0) <= r.fills[0].ts - at(10, 1)

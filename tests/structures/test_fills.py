"""The offline structure simulator's bid-ask spread (gate V4): the engine's ASSUMED synthetic
spread model applied to next-open fills. Numbers come from configs/backtest/synthetic_spreads.toml."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest

from project100c.backtest.spreads import SyntheticSpreadModel, load_spread_params
from project100c.calendar import TradingCalendar, load_holiday_book
from project100c.sessions.model import IST
from project100c.structures import OptionHalfSpread, fill_price

REPO = Path(__file__).resolve().parents[2]
TUE = date(2026, 10, 6)  # a NIFTY weekly expiry (Tuesday)


@pytest.fixture(scope="module")
def hs() -> OptionHalfSpread:
    model = SyntheticSpreadModel(load_spread_params(REPO / "configs" / "backtest" / "synthetic_spreads.toml"))
    cal = TradingCalendar(load_holiday_book(REPO / "configs" / "calendar" / "nse_fo_holidays.toml"))
    return OptionHalfSpread(model, cal, 50)


def at(d: date, t: str) -> datetime:
    return datetime.combine(d, time.fromisoformat(t), IST)


def test_it_is_the_engine_model_and_says_so(hs: OptionHalfSpread) -> None:
    assert hs.status == "ASSUMED" and hs.describe()["spread_model"].startswith("SPREAD-ASSUMED")


def test_half_spread_matches_the_config_formula(hs: OptionHalfSpread) -> None:
    # 100-rupee ATM premium, mid-session, 4 trading days out: full = 1% x 100 = 1.00 -> 20 ticks, half = 10 ticks
    assert hs(100.0, at(date(2026, 10, 1), "11:00"), TUE, 25000, 25010) == pytest.approx(0.50)
    # the opening band doubles it; expiry day x1.5; far from the money widens it
    assert hs(100.0, at(date(2026, 10, 1), "09:20"), TUE, 25000, 25010) == pytest.approx(1.00)
    assert hs(100.0, at(TUE, "11:00"), TUE, 25000, 25010) == pytest.approx(0.75)
    assert hs(100.0, at(date(2026, 10, 1), "11:00"), TUE, 25400, 25000) > hs(
        100.0, at(date(2026, 10, 1), "11:00"), TUE, 25000, 25000
    )


def test_a_cheap_option_still_pays_the_two_tick_floor(hs: OptionHalfSpread) -> None:
    assert hs(0.5, at(date(2026, 10, 1), "11:00"), TUE, 25500, 25000) >= 0.05


def test_fills_move_against_the_trader() -> None:
    assert fill_price(100.0, True, 2, 0.05, 0.5) == pytest.approx(100.6)
    assert fill_price(100.0, False, 2, 0.05, 0.5) == pytest.approx(99.4)
    assert fill_price(0.3, False, 2, 0.05, 0.5) == pytest.approx(0.05)  # never below one tick
    assert fill_price(100.0, True, 2, 0.05) == pytest.approx(100.1)  # no spread: the old SR-2026-10-03.1 fill
    with pytest.raises(ValueError):
        fill_price(100.0, True, -1, 0.05)


def test_after_expiry_is_refused(hs: OptionHalfSpread) -> None:
    with pytest.raises(ValueError):
        hs(10.0, at(date(2026, 10, 7), "11:00"), TUE, 25000, 25000)

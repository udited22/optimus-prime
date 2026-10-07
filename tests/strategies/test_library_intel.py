"""Mechanics tests for the intelligence-layer plug-ins H19-H22 (strategies/library/signals_intel.py).

SYNTHETIC multi-day tapes (seeded DayPlans; the Black-Scholes chain only on the last day) so the plug-ins have the
history they need. What is asserted is mechanics: the setup fires for the stated reason only with enough history and
the stated filter, exits are the plug-in's or the base's, the book is flat and never short. No edge is claimed.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from functools import cache
from itertools import pairwise

import pytest

from project100c.strategies.library.optmath import bs_price, gamma_concentration, implied_vol
from project100c.strategies.library.signals_intel import rv5
from project100c.synthetic import DayPlan, Segment
from tests.strategies.library_rig import LibRun, rig, run
from tests.strategies.test_library import assert_sound

WED = date(2026, 10, 7)


def days_before(n: int) -> list[date]:
    cal = rig().clock.calendar
    out = [WED]
    while len(out) < n:
        out.insert(0, cal.previous_trading_day(out[0]))
    return out


def quiet(days: list[date], seed: int, *, vol: float = 8, revert: float = 0.3, vix: float = 13) -> list[DayPlan]:
    return [DayPlan(d, seed * 100 + i, segments=(Segment(375, 0, vol, revert=revert),), vix_open=vix)
            for i, d in enumerate(days)]  # fmt: skip


def noise_plans(history: int = 15) -> list[DayPlan]:
    ds = days_before(history + 1)
    last = DayPlan(ds[-1], 1, segments=(Segment(60, 0.012, 10), Segment(120, 0.004, 10), Segment(195, -0.01, 10)))
    return [*quiet(ds[:-1], 1), last]


def imom_plans(drift: float = 0.003) -> list[DayPlan]:
    ds = days_before(22)
    segs = (Segment(255, drift, 14), Segment(30, 0.01, 14), Segment(90, 0.006, 14))
    return [*quiet(ds[:-1], 1, revert=0.2), DayPlan(ds[-1], 1, segments=segs)]


def dayvol_plans(vix: float = 10) -> list[DayPlan]:
    ds = days_before(24)
    segs = (Segment(90, 0, 30), Segment(60, 0.03, 30), Segment(225, 0.0, 30))
    return [*quiet(ds[:-1], 2, vol=30, revert=0.0, vix=vix), DayPlan(ds[-1], 2, segments=segs, vix_open=vix)]


def gex_plans() -> list[DayPlan]:
    return [DayPlan(WED, 2, segments=(Segment(60, 0, 8, revert=0.4), Segment(30, 0.02, 12), Segment(285, 0.004, 12)))]


def rank(x: int) -> dict[date, dict[str, Decimal]]:
    return {WED: {"gex_rank": Decimal(x)}}


@cache
def scenario(name: str) -> LibRun:
    if name == "noise":
        return run("S-NOISE-001", noise_plans(), chain_last_only=True)
    if name == "imom":
        return run("S-IMOM-001", imom_plans(), chain_last_only=True)
    if name == "dayvol":
        return run("S-DAYVOL-001", dayvol_plans(), chain_last_only=True)
    return run("S-GEXMO-001", gex_plans(), daily_features=rank(80))


EXPECTED = {
    "noise": ("S-NOISE-001", "NOISE_BREAK_UP", "INVALIDATED_BACK_IN_NOISE"),
    "imom": ("S-IMOM-001", "ROD_MOMENTUM_UP", "TARGET"),
    "dayvol": ("S-DAYVOL-001", "INTRADAY_VOL_CHEAP", "TARGET"),
    "gex": ("S-GEXMO-001", "GAMMA_BREAK_UP", "TARGET"),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_plugin_trades_its_setup(name: str) -> None:
    lr = scenario(name)
    assert_sound(lr)
    _, reason, exit_reason = EXPECTED[name]
    closed = [t for t in lr.trades if t["outcome"] == "CLOSED"]
    assert len(closed) == 1, lr.trades
    t = closed[0]
    assert t["reason"] == reason and t["exit_reason"] == exit_reason
    assert t["day"] == WED  # never before the history it needs exists (and the chain is only on the last day)
    assert t["risk_at_stop"] <= Decimal("0.02") * lr.result.starting_cash


def test_noise_needs_its_lookback_and_checks_on_the_half_hour() -> None:
    t = scenario("noise").trades[0]
    assert t["at"].minute in (0, 30) and t["facts"]["upper"] > t["facts"]["lower"]
    short = run("S-NOISE-001", noise_plans(history=8), chain_last_only=True)  # 8 < lookback_sessions 14
    assert not short.strategy.signals


def test_imom_signals_at_1330_and_needs_the_move() -> None:
    t = scenario("imom").trades[0]
    assert t["at"].hour == 13 and t["at"].minute == 30
    assert t["facts"]["r_rod_pct"] >= Decimal("0.4") and t["facts"]["rv_pct"] >= 50
    flat = run("S-IMOM-001", imom_plans(drift=-0.0005), chain_last_only=True)
    assert not [s for s in flat.strategy.signals if s["reason"].startswith("ROD_MOMENTUM_UP")]


def test_dayvol_is_a_two_leg_straddle_that_buys_only_cheap_intraday_variance() -> None:
    t = next(t for t in scenario("dayvol").trades if t["outcome"] == "CLOSED")
    assert sorted(t["rights"]) == ["CE", "PE"] and len(t["legs_detail"]) == 2
    assert len({leg["key"].rsplit("CE", 1)[0].rsplit("PE", 1)[0] for leg in t["legs_detail"]}) == 1  # same strike
    assert t["facts"]["ratio"] >= Decimal("1.25") and t["facts"]["term"] <= Decimal("0.5")
    assert scenario("dayvol").result.rejects == 0  # OD-013: the shipped limits give it 2 lots (RL-2026-10-03.2)
    dear = run("S-DAYVOL-001", dayvol_plans(vix=60), chain_last_only=True)  # IV far above the realised variance
    assert not dear.strategy.signals
    capped = run("S-DAYVOL-001", dayvol_plans(), chain_last_only=True, max_lots=1)
    assert_sound(capped)
    assert capped.trades[0]["exit_reason"] == "LEG_REJECTED"


def test_gexmo_trades_only_on_high_gamma_days_and_only_the_first_break() -> None:
    t = scenario("gex").trades[0]
    assert t["facts"]["gex_rank"] == 80 and t["at"].minute % 15 == 0
    low = run("S-GEXMO-001", gex_plans(), daily_features=rank(50))  # below g_pct_min 67
    assert not low.strategy.signals
    missing = run("S-GEXMO-001", gex_plans())  # no feature (G not computable): no trade
    assert not missing.strategy.signals


def test_option_math() -> None:
    p = bs_price(25_000, 25_000, 3 / 365, 0.14, True)
    iv = implied_vol(p, 25_000, 25_000, 3 / 365, True)
    assert iv is not None and iv == pytest.approx(0.14, abs=1e-6)
    assert implied_vol(0.0001, 25_000, 25_000, 3 / 365, True) is None
    g1 = gamma_concentration([(25_000, 0.14, 1000, 65)], spot=25_000, t=3 / 365)
    g2 = gamma_concentration([(25_000, 0.14, 2000, 65), (26_000, 0.14, 0, 65)], spot=25_000, t=3 / 365)
    assert g1 > 0 and g2 == pytest.approx(2 * g1)


def test_rv5_uses_five_minute_closes() -> None:
    lr = scenario("gex")
    bars = list(lr.days[0].index)
    v = rv5(bars)
    px = [float(bars[0].open)] + [float(b.close) for b in bars if (b.start.minute + 1) % 5 == 0]
    assert v == pytest.approx(sum(math.log(b / a) ** 2 for a, b in pairwise(px)))
    assert rv5([]) == 0.0

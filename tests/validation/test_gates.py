"""Validation toolkit (S-02, docs/research/validation.md V1-V18). The backlog AT: a known-overfit synthetic strategy is
rejected and a
known-edge synthetic strategy is accepted. All trade data here is SYNTHETIC; nothing claims an edge."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

from project100c.errors import ConfigError
from project100c.sessions.model import IST
from project100c.spec.models import StrategySpec
from project100c.validation import (
    Era,
    GateConfig,
    GateStatus,
    Trade,
    ValidationInputs,
    Verdict,
    era_of,
    load_gate_config,
    validate,
)
from project100c.validation.stats import (
    day_block_bootstrap_ci,
    deflated_sharpe,
    expected_max_sharpe,
    max_drawdown_frac,
    mc_drawdown,
    moments,
    percentile,
)
from tests.data.dhan_fakes import CONFIGS
from tests.strategies.library_rig import spec

NAV = Decimal(1_000_000)
START = date(2025, 9, 1)


@cache
def cfg() -> GateConfig:
    return load_gate_config(CONFIGS / "validation" / "gates.toml")


def make_trades(n: int, mean: float, sd: float, *, seed: int, per_day: int = 2, start: date = START,
                tags: tuple[str, ...] = ("TRENDING_UP", "VOLATILITY_NORMAL"), **kw: Any) -> list[Trade]:  # fmt: skip
    rng = random.Random(seed)
    out, d = [], start
    for i in range(n):
        if i and i % per_day == 0:
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
        p = Decimal(repr(round(rng.gauss(mean, sd), 2)))
        ts = datetime.combine(d, time(10, 0), IST) + timedelta(minutes=30 * (i % per_day))
        t = Trade(d, ts, ts + timedelta(minutes=20), p, Decimal(60), Decimal(30), Decimal(2_000), 65,
                  frozenset(tags), exit_reason="STOP" if p < 0 else "TARGET")  # fmt: skip
        out.append(replace(t, **kw) if kw else t)
    return out


def good_inputs(trades: list[Trade], sp: StrategySpec, **kw: Any) -> ValidationInputs:
    base = ValidationInputs(
        spec=sp,
        trades=trades,
        nav=NAV,
        data_label="SYNTHETIC",
        n_trials=20,
        trial_sr_variance=0.002,
        lookahead_stamps=[(t.entry_ts, t.entry_ts - timedelta(minutes=1)) for t in trades],
        canary_ci_lower=-10.0,
        fills_dated_costs=True,
        fill_model={"model": "bar", "spread_model": "synthetic_spreads.toml SS-1"},
        param_grid={(i, j): 200.0 + 10 * (i + j) for i in range(3) for j in range(3)},
        delayed_trades=[replace(t, net_pnl=t.net_pnl - Decimal(50)) for t in trades],
        rerun_hashes=("ab" * 32, "ab" * 32),
    )
    return replace(base, **kw)


@cache
def edge() -> list[Trade]:
    return make_trades(300, 300.0, 1000.0, seed=1)


SP = spec("S-VWAPC-001")  # eligible TRENDING_UP / TRENDING_DOWN


# ---------------------------------------------------------------------------------------------- stats
def test_percentile_and_bootstrap_are_deterministic() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    days = [t.day for t in edge()]
    pnl = [float(t.net_pnl) for t in edge()]
    a = day_block_bootstrap_ci(days, pnl, level=0.9, iterations=500, seed=7)
    assert a == day_block_bootstrap_ci(days, pnl, level=0.9, iterations=500, seed=7)
    assert a[1] < a[0] < a[2]
    with pytest.raises(ValueError):
        day_block_bootstrap_ci([], [], level=0.9, iterations=10, seed=1)


def test_expected_max_sharpe_grows_with_trials_and_the_dsr_deflates() -> None:
    assert expected_max_sharpe(1, 0.01) == 0
    assert expected_max_sharpe(10, 0.01) < expected_max_sharpe(1000, 0.01)
    m = moments([float(t.net_pnl) for t in edge()])
    few, _ = deflated_sharpe(m, n_trials=2, sr_variance=0.002)
    many, _ = deflated_sharpe(m, n_trials=100_000, sr_variance=0.02)
    assert few > many


def test_drawdown_and_monte_carlo() -> None:
    dd, low = max_drawdown_frac([100, -300, 50], 1000)
    assert dd == pytest.approx(300 / 1100) and low == pytest.approx(0.8)
    a = mc_drawdown([-50.0, 40.0], nav=1000, iterations=500, seed=3, dd_frac=0.15, ruin_frac=0.5)
    assert a == mc_drawdown([-50.0, 40.0], nav=1000, iterations=500, seed=3, dd_frac=0.15, ruin_frac=0.5)


def test_rule_eras_follow_design_13() -> None:
    assert era_of(date(2024, 11, 19)) is Era.MULTI_WEEKLY
    assert era_of(date(2024, 11, 20)) is Era.SINGLE_WEEKLY_THU
    assert era_of(date(2025, 9, 1)) is Era.TUESDAY


def test_the_gate_config_is_versioned(tmp_path: Any) -> None:
    c = cfg()
    assert c.version.startswith("VG-") and c.min_oos_trades == 100 and c.dsr_min == Decimal("0.95")
    p = tmp_path / "g.toml"
    p.write_text("[[gates]]\nversion = 'x'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_gate_config(p)


# ---------------------------------------------------------------------------------------------- the AT
def test_a_known_edge_synthetic_strategy_passes_every_gate_it_can() -> None:
    r = validate(good_inputs(edge(), SP), cfg())
    failed = [(g.code, g.note, g.metrics) for g in r.gates if g.status is GateStatus.FAIL]
    assert not failed, failed
    assert {g.code for g in r.gates if g.status is GateStatus.NOT_EVALUATED} == {"V2", "V16"}
    assert r.verdict is Verdict.PASSES_ON_SYNTHETIC  # synthetic data can never VALIDATE
    assert len(r.gates) == 18 and [g.code for g in r.gates] == [f"V{i}" for i in range(1, 19)]


def test_on_real_data_with_a_consistent_holdout_the_same_strategy_validates() -> None:
    hold = make_trades(80, 415.0, 100.0, seed=99, start=date(2026, 4, 1))
    r = validate(good_inputs(edge(), SP, data_label="REAL", universe_point_in_time=True, holdout_trades=hold), cfg())
    assert r.verdict is Verdict.VALIDATED, [(g.code, g.status, g.note) for g in r.gates if g.status != "PASS"]


def test_a_known_overfit_synthetic_strategy_is_rejected() -> None:
    # 200 zero-edge trials; keep the luckiest one, as a careless search would
    trials = [make_trades(120, 0.0, 1000.0, seed=1000 + k) for k in range(200)]
    srs = [moments([float(t.net_pnl) for t in tr]).sharpe for tr in trials]
    best = max(range(200), key=lambda k: srs[k])
    var = sum((s - sum(srs) / len(srs)) ** 2 for s in srs) / (len(srs) - 1)
    fresh = make_trades(60, 0.0, 1000.0, seed=4242, start=date(2026, 5, 1))  # its holdout: still no edge
    inp = good_inputs(trials[best], SP, n_trials=200, trial_sr_variance=var, data_label="REAL",
                      universe_point_in_time=True, holdout_trades=fresh)  # fmt: skip
    r = validate(inp, cfg())
    assert r.verdict is Verdict.REJECTED
    assert r.gate("V9").status is GateStatus.FAIL  # deflated by the 200 trials
    assert r.gate("V16").status is GateStatus.FAIL  # the holdout does not confirm it


# ---------------------------------------------------------------------------------------------- single gates
def test_v1_catches_an_input_from_the_future() -> None:
    t = edge()
    stamps = [(x.entry_ts, x.entry_ts - timedelta(minutes=1)) for x in t]
    stamps[5] = (t[5].entry_ts, t[5].entry_ts + timedelta(seconds=1))
    r = validate(good_inputs(t, SP, lookahead_stamps=stamps), cfg())
    assert r.gate("V1").status is GateStatus.FAIL and r.gate("V1").metrics["violations"] == "1"
    lat = validate(good_inputs(t, SP, latency=timedelta(minutes=2)), cfg())  # inputs must also clear the latency
    assert lat.gate("V1").status is GateStatus.FAIL


def test_v4_needs_a_spread_model_and_v3_dated_costs() -> None:
    r = validate(good_inputs(edge(), SP, fill_model={"model": "bar", "spread_model": None}, fills_dated_costs=False),
                 cfg())  # fmt: skip
    assert r.gate("V4").status is GateStatus.FAIL and "optimistic" in r.gate("V4").note
    assert r.gate("V3").status is GateStatus.FAIL


def test_v6_sample_size_and_the_recent_era() -> None:
    old = make_trades(300, 300.0, 1000.0, seed=1, start=date(2024, 1, 1), per_day=4)  # all before 1-Sep-2025
    r = validate(good_inputs(old, SP), cfg())
    assert r.gate("V6").status is GateStatus.FAIL and r.gate("V6").metrics["recent_era_trades"] == "0"
    small = validate(good_inputs(edge()[:50], SP), cfg())
    assert small.gate("V6").status is GateStatus.FAIL


def test_v7_one_lucky_trade_is_concentration() -> None:
    t = make_trades(200, 0.0, 300.0, seed=5) + make_trades(1, 200_000.0, 1.0, seed=6, start=date(2025, 12, 1))
    r = validate(good_inputs(t, SP), cfg())
    assert r.gate("V7").status is GateStatus.FAIL


def test_v8_a_sharp_peak_fails() -> None:
    grid = {(i, j): -50.0 for i in range(3) for j in range(3)} | {(1, 1): 900.0}
    r = validate(good_inputs(edge(), SP, param_grid=grid), cfg())
    assert r.gate("V8").status is GateStatus.FAIL


def test_v10_a_strategy_living_on_thin_slippage_fails_the_stress() -> None:
    t = [replace(x, slippage=Decimal(600)) for x in edge()]  # 2x slippage costs another 600 a trade
    r = validate(good_inputs(t, SP), cfg())
    assert r.gate("V10").status is GateStatus.FAIL and r.gate("V5").status is GateStatus.PASS


def test_v11_and_v17() -> None:
    r = validate(good_inputs(edge(), SP, delayed_trades=[replace(x, net_pnl=x.net_pnl - 600) for x in edge()],
                             rerun_hashes=("a" * 64, "b" * 64)), cfg())  # fmt: skip
    assert r.gate("V11").status is GateStatus.FAIL and r.gate("V17").status is GateStatus.FAIL


def test_v13_ruinous_sizing_fails_the_monte_carlo() -> None:
    r = validate(good_inputs(edge(), SP, nav=Decimal(20_000)), cfg())  # the same trades on a tiny account
    assert r.gate("V13").status is GateStatus.FAIL


def test_v14_regime_dependence() -> None:
    t = edge() + make_trades(40, -800.0, 300.0, seed=8, tags=("TRENDING_DOWN",), start=date(2026, 3, 2))
    r = validate(good_inputs(t, SP), cfg())
    assert r.gate("V14").status is GateStatus.FAIL and "TRENDING_DOWN" in r.gate("V14").note
    p = edge() + make_trades(40, 800.0, 300.0, seed=9, tags=("ABNORMAL_MARKET",), start=date(2026, 3, 2))
    r2 = validate(good_inputs(p, SP), cfg())
    assert r2.gate("V14").status is GateStatus.FAIL and "prohibited" in r2.gate("V14").note


def test_v15_event_dependence_needs_certification() -> None:
    t = make_trades(200, -100.0, 300.0, seed=10) + make_trades(
        120, 2000.0, 300.0, seed=11, event_day=True, start=date(2026, 1, 5)
    )
    r = validate(good_inputs(t, SP), cfg())
    assert r.gate("V15").status is GateStatus.FAIL
    assert validate(good_inputs(t, SP, event_certified=True), cfg()).gate("V15").status is GateStatus.PASS


def test_v16_allows_one_look_only() -> None:
    hold = make_trades(80, 300.0, 1000.0, seed=99, start=date(2026, 4, 1))
    r = validate(good_inputs(edge(), SP, data_label="REAL", holdout_trades=hold, holdout_prior_looks=1), cfg())
    assert r.gate("V16").status is GateStatus.FAIL and "second look" in r.gate("V16").note


def test_v18_reports_min_capital_against_the_nav() -> None:
    r = validate(good_inputs(edge(), SP, nav=Decimal(50_000)), cfg())
    g = r.gate("V18")
    assert g.status is GateStatus.PASS
    assert g.metrics["min_capital_inr"] == "100000" and g.metrics["eligible_at_nav"] == "False"


def test_missing_inputs_are_not_evaluated_and_block_validation() -> None:
    bare = ValidationInputs(spec=SP, trades=edge(), nav=NAV, data_label="REAL", n_trials=20)
    r = validate(bare, cfg())
    ne = {g.code for g in r.gates if g.status is GateStatus.NOT_EVALUATED}
    assert {"V1", "V2", "V3", "V4", "V8", "V11", "V16", "V17"} <= ne
    assert r.verdict is not Verdict.VALIDATED
    assert r.to_dict()["verdict"] == r.verdict.value


def test_the_report_is_reproducible() -> None:
    a = validate(good_inputs(edge(), SP), cfg()).to_dict()
    assert a == validate(good_inputs(edge(), SP), cfg()).to_dict()

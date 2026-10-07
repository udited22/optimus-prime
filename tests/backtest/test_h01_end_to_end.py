"""End to end on FIXTURE data (the fake Dhan server run through the real downloader; synthetic prices).

The chain is: lake manifests (with lineage) -> DhanLakeReader -> synthetic spread model (ASSUMED) -> bar
backtester -> the H01 spec S-ORB-001 -> a hash-chained ledger plus a per-component cost breakdown.

The P&L numbers here mean nothing, because the prices are generated. The tests check mechanics, determinism,
provenance and the kernel rules, not strategy quality."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import time, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

from project100c.backtest.engine import COST_COMPONENTS
from project100c.backtest.lake_source import EXPIRY_CODE_ASSUMPTION
from project100c.backtest.report import verify_ledger_file, write_run_artifacts
from project100c.core_types import OrderSide
from project100c.costs import CostModel, Side, load_brokerage_plans, load_charge_book
from project100c.errors import BacktestError
from project100c.sessions import IST
from project100c.spec.io import load_spec_file
from project100c.strategies.orb_h01 import H01_DEVIATIONS, OrbParams
from tests.backtest.h01_rig import SPEC, H01Run, build_lake, run_h01
from tests.backtest.lake_fixture import FixtureLake
from tests.data.dhan_fakes import CONFIGS

D = Decimal
LAKH = D(100000)


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FixtureLake]:
    fx = build_lake(tmp_path_factory.mktemp("h01lake"))
    yield fx
    fx.rig.store.close()


@pytest.fixture(scope="module")
def run(lake: FixtureLake) -> H01Run:
    return run_h01(lake, nav=LAKH)


def test_produces_ledger_and_cost_breakdown(run: H01Run, tmp_path: Path) -> None:
    r = run.result
    closed = [t for t in run.strategy.trades if t.get("outcome") == "CLOSED"]
    assert closed, "fixture should produce at least one round trip"
    assert r.flat_at_end and len(r.fills) % 2 == 0 and r.rejects == 0
    a = write_run_artifacts(r, tmp_path / "a", strategy_report={"counters": run.strategy.counters})
    b = write_run_artifacts(r, tmp_path / "b", strategy_report={"counters": run.strategy.counters})
    for x, y in ((a.ledger, b.ledger), (a.costs, b.costs), (a.summary, b.summary)):
        assert x.read_bytes() == y.read_bytes()  # deterministic artifacts
    assert verify_ledger_file(a.ledger) == r.ledger_hash  # the file alone reproduces the hash chain
    totals = r.cost_breakdown()
    assert sum(totals[k] for k in COST_COMPONENTS[:-1]) == totals["total"] == r.total_charges > 0
    assert r.net_pnl == r.gross_pnl - r.total_charges
    flows = sum((f.price * f.qty) * (1 if f.side is OrderSide.SELL else -1) for f in r.fills)
    assert r.starting_cash + flows - r.total_charges == r.cash
    assert '"brokerage"' in a.costs.read_text() and '"stt"' in a.costs.read_text()


def test_charges_recomputed_independently(run: H01Run) -> None:
    costs = CostModel(
        load_charge_book(CONFIGS / "costs" / "nse_fo_index_options.toml"),
        load_brokerage_plans(CONFIGS / "costs" / "brokerage_plans.toml"),
    )
    for f in run.result.fills:
        side = Side.BUY if f.side is OrderSide.BUY else Side.SELL
        again = costs.order_charges(side, f.price, f.qty, f.ts.astimezone(IST).date(), "upstox-options")
        assert f.breakdown == again and f.charges == again.total
        assert f.half_spread > 0  # every fill had to clear the ASSUMED spread


def test_run_metadata_carries_provenance_and_caveats(run: H01Run) -> None:
    start = run.result.ledger.entries[0]
    assert start["kind"] == "START" and start["strategy"] == "S-ORB-001@0.1.0"
    meta = start["metadata"]
    assert meta["strategy"]["deviations"] == list(H01_DEVIATIONS)
    inputs = {i["dataset"]: i for i in meta["data"]["inputs"]}
    assert set(inputs) == {"dhan_rolling_option", "dhan_intraday"}
    assert all(len(i["fingerprint"]) == 64 and i["parts_used"] > 0 for i in inputs.values())
    assert inputs["dhan_rolling_option"]["fingerprint"] == run.data.reports[0].fingerprint()
    assert EXPIRY_CODE_ASSUMPTION in meta["data"]["assumptions"]
    assert start["fill_model"]["spread_status"] == "ASSUMED"
    assert "FIXTURE" in meta["synthetic_prices"]
    fills = [e for e in run.result.ledger.entries if e["kind"] == "FILL"]
    assert fills and all(set(COST_COMPONENTS[:-1]) <= set(e["charge_parts"]) for e in fills)


def test_rerun_same_ledger_hash(lake: FixtureLake, run: H01Run) -> None:
    again = run_h01(lake, nav=LAKH)
    assert again.result.ledger_hash == run.result.ledger_hash


def _floor(p: Decimal) -> Decimal:
    t = D("0.05")
    return (p / t).to_integral_value(rounding=ROUND_FLOOR) * t


def test_spec_rules_hold_in_the_ledger(run: H01Run) -> None:
    led = run.result.ledger.entries
    accepted = {e["order_id"]: e for e in led if e["kind"] == "ORDER_ACCEPTED"}
    params = OrbParams.from_spec(load_spec_file(SPEC))
    for e in accepted.values():
        t = e["ts"].astimezone(IST)
        c = run.data.contracts[e["key"]]
        assert c.expiry > t.date()  # nearest weekly with DTE >= 1
        assert e["qty"] == c.lot_size == 65  # one lot (NSE/FAOP/70616 lot size)
        if "ENTRY" in e["tag"]:
            assert e["side"] is OrderSide.BUY and params.entry_start <= t.time() <= params.entry_end
    for tr in (t for t in run.strategy.trades if t.get("entry_fill") is not None):
        fill = tr["entry_fill"]
        assert tr["stop_trigger"] == _floor(fill * D("0.70"))  # 30% premium stop
        assert tr["stop_limit"] == tr["stop_trigger"] - 4 * D("0.05")  # 4-tick SL-LIMIT offset
        assert tr["target"] == fill + D("1.5") * (fill - tr["stop_trigger"])
        if tr["exit_reason"] == "MAX_HOLD":  # decided on the first bar at/after 75 min (+ report latency)
            late = tr["exit_decided_at"] - (tr["entry_ts"] + timedelta(minutes=75))
            assert timedelta(0) <= late <= timedelta(minutes=2)
        if tr["exit_reason"] != "STOP":  # cancel-stop then sell-limit chase: ~3 bars per attempt
            assert tr["exit_ts"] - tr["exit_decided_at"] <= timedelta(minutes=3 * tr["exit_attempts"] + 2)
        assert tr["exit_ts"].astimezone(IST).time() <= time(14, 50)
        assert tr["risk_at_stop"] <= D("0.02") * LAKH


def test_ten_thousand_nav_cannot_afford_the_spec(lake: FixtureLake) -> None:
    """With a NAV of 10,000 and the 2% per-trade cap (OD-005), a 30% stop on a 65-lot ATM option breaks the
    budget: the strategy must decline every signal rather than trade oversized."""
    r = run_h01(lake, nav=D(10000))
    assert not r.result.fills
    signals = [t for t in r.strategy.trades if "risk_at_stop" in t]
    assert signals and all(t["outcome"] == "NO_TRADE_RISK" and t["risk_at_stop"] > D(200) for t in signals)


def test_params_come_from_the_spec() -> None:
    spec = load_spec_file(SPEC)
    p = OrbParams.from_spec(spec)
    assert (p.or_minutes, p.theta_v, p.stop_pct, p.target_r) == (15, D("1.5"), D(30), D("1.5"))
    assert (p.time_exit, p.max_hold, p.max_chase_ticks) == (time(14, 30), timedelta(minutes=75), 2)
    bad = spec.model_copy(update={"exit": spec.exit.model_copy(update={"profit_taking": "trail only"})})
    with pytest.raises(BacktestError, match="N R"):
        OrbParams.from_spec(bad)

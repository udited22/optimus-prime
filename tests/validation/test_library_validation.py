"""The toolkit on a real library run (SYNTHETIC days): mechanics only, and it can never validate."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from project100c.validation import GateStatus, ValidationInputs, Verdict, load_gate_config, trades_from_library_run
from project100c.validation import validate as run_gates
from tests.data.dhan_fakes import CONFIGS
from tests.strategies.test_library import scenario


def test_a_library_run_goes_through_the_gates_and_is_not_validated() -> None:
    lr = scenario("gap-go")
    trades = trades_from_library_run(lr.trades, lr.result)
    assert len(trades) == 1
    t = trades[0]
    assert t.net_pnl == lr.result.net_pnl  # one trade: its P&L is the run's
    assert t.charges == lr.result.total_charges and t.qty == 65
    stamps = [(s["decided_at"], s["inputs_end"]) for s in lr.strategy.signals]
    meta = lr.result.ledger.entries[0]["fill_model"]
    inp = ValidationInputs(
        spec=lr.strategy.spec, trades=trades, nav=Decimal(1_000_000), data_label="SYNTHETIC", n_trials=1,
        lookahead_stamps=stamps, latency=timedelta(0), fills_dated_costs=all(f.breakdown for f in lr.result.fills),
        fill_model=meta, rerun_hashes=(lr.result.ledger_hash, lr.result.ledger_hash),
    )  # fmt: skip
    r = run_gates(inp, load_gate_config(CONFIGS / "validation" / "gates.toml"))
    assert r.gate("V1").status is GateStatus.PASS  # decisions never use a bar before it closes
    assert r.gate("V3").status is GateStatus.PASS
    assert r.gate("V4").status is GateStatus.FAIL  # the mechanics rig runs without a spread model
    assert r.gate("V6").status is GateStatus.FAIL  # one trade
    assert r.verdict is Verdict.REJECTED


def test_paired_trades_keep_their_own_record() -> None:
    """Every closed trade is paired with the record it came from (two trades on a day never share one)."""
    from project100c.validation import paired_trades_from_library_run
    from tests.strategies.test_library import SCENARIOS

    n = 0
    for name in SCENARIOS:
        lr = scenario(name)
        pairs = paired_trades_from_library_run(lr.trades, lr.result)
        assert [t for _, t in pairs] == trades_from_library_run(lr.trades, lr.result)
        assert len({id(r) for r, _ in pairs}) == len(pairs)
        for rec, t in pairs:
            assert rec["outcome"] == "CLOSED" and rec["day"] == t.day and t.entry_ts >= rec["at"]
        n += len(pairs)
    assert n >= 5

"""economics/mvc.py: minimum viable capital and per-lot trade statistics."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from project100c.economics.mvc import (
    CapitalInputs,
    CapitalPolicy,
    max_drawdown,
    minimum_viable_capital,
    suggest_decision,
    trade_stats,
)


def test_mvc_is_the_binding_constraint() -> None:
    x = CapitalInputs(cash_per_lot=50_000, worst_trade_loss_per_lot=16_000, stress_loss_per_lot=17_000,
                      max_drawdown_per_lot=7_500)  # fmt: skip
    m = minimum_viable_capital(x)
    assert m.binding == "per_trade" and m.capital == pytest.approx(800_000)
    assert m.components["cash"] == 75_000 and m.components["stress"] == 170_000
    assert m.components["drawdown"] == pytest.approx(56_250)
    loose = minimum_viable_capital(x, CapitalPolicy(risk_frac=0.10))
    assert loose.binding == "stress" and loose.capital == 170_000


def test_no_stress_input_means_no_stress_component() -> None:
    m = minimum_viable_capital(CapitalInputs(10_000, 5_000, None, 20_000))
    assert "stress" not in m.components and m.binding == "per_trade"


def test_trade_stats_and_drawdown() -> None:
    assert max_drawdown([100, -50, -70, 30, 200, -10]) == 120
    d0 = date(2024, 1, 1)
    trades = [(d0 + timedelta(days=7 * i), 300.0 if i % 3 else -400.0) for i in range(60)]
    s = trade_stats(trades)
    assert s.n == 60 and s.worst_trade == -400 and 0.6 < s.hit_rate < 0.7
    assert s.ci is not None and s.ci[0] < s.mean < s.ci[1]
    assert s.trades_per_year == pytest.approx(60 / s.years, rel=0.01)
    assert suggest_decision(s).startswith(("ITERATE", "FORWARD", "PROMOTE"))
    losing = trade_stats([(d0 + timedelta(days=i), -100.0 - i % 5) for i in range(40)])
    assert suggest_decision(losing).startswith("KILL")
    with pytest.raises(ValueError):
        trade_stats([])


def test_scoreboard_prints_one_table_per_candidate(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("scoreboard", repo / "scripts" / "scoreboard.py")
    assert spec is not None and spec.loader is not None
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)
    rows = ["day,net_1lot,lot,entry_fill"] + [
        f"2024-0{1 + i % 9}-1{i % 10},{(-300 if i % 3 else 500)},75,100" for i in range(30)
    ]
    (tmp_path / "t.csv").write_text("\n".join(rows) + "\n")
    (tmp_path / "c.toml").write_text(
        '[[candidate]]\nid = "T"\nworst_loss = "premium_p95"\ncash_per_lot = "premium_p95"\n'
        'source = { kind = "csv", path = "t.csv", day_col = "day", pnl_col = "net_1lot", lot_col = "lot", '
        'current_lot = 65, premium_col = "entry_fill" }\n'
    )
    assert sb.main(["--candidates", str(tmp_path / "c.toml"), "--lake", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "### T" in out and "MVC, 2% per-trade rule | ₹325,000 (binding: per_trade" in out

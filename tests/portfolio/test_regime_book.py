from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from project100c.errors import ConfigError
from project100c.portfolio.regime_book import (
    BookTrade,
    LabelIndex,
    RegimeMapConfig,
    book_metrics,
    book_trades_from_run,
    cell_table,
    exclusion,
    load_regime_map_config,
    mean_diff_ci,
    regime_cell,
    simulate_book,
    walk_forward_select,
    walk_forward_windows,
)
from project100c.sessions.model import IST

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def _cfg() -> RegimeMapConfig:
    return load_regime_map_config(CONFIGS / "portfolio" / "regime_map.toml")


def _t(strategy: str, d: date, hh: int, mm: int, pnl: float, *, hold: int = 30, risk: float = 150.0,
       cell: str = "VOLATILITY_NORMAL", why: str = "TIME_EXIT") -> BookTrade:  # fmt: skip
    e = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)
    return BookTrade(strategy, cell, d, e, e + timedelta(minutes=1), e + timedelta(minutes=hold), pnl, 40.0, 5.0,
                     risk, why)  # fmt: skip


def test_config_loads_and_is_pre_registered() -> None:
    c = _cfg()
    assert c.version == "RM-2026-10-03.2"
    assert ("S-ORB-002", "S-ORBML-001") in {tuple(g) for g in c.exclusive_groups}
    assert load_regime_map_config(
        CONFIGS / "portfolio" / "regime_map.toml", version="RM-2026-10-03.1"
    ).version.endswith(".1")
    assert c.cell_dimensions == ("vol",)
    assert c.holdout == "HD-2026-10-03.1"
    assert c.max_trades_per_day == 10
    w = walk_forward_windows(c)
    assert w[0] == (date(2023, 9, 1), date(2023, 11, 30))
    assert w[-1][1] == c.research_to
    assert all(b[0] == a[1] + timedelta(days=1) for a, b in pairwise(w))


def test_unknown_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_regime_map_config(CONFIGS / "portfolio" / "regime_map.toml", version="RM-1999-01-01.1")
    p = tmp_path / "x.toml"
    p.write_text("[other]\n")
    with pytest.raises(ConfigError):
        load_regime_map_config(p)


def test_walk_forward_uses_only_earlier_trades() -> None:
    c = _cfg()
    good = [_t("A", date(2023, 1, 2) + timedelta(days=i), 10, 0, 100.0 + i % 3) for i in range(40)]
    late = [_t("B", date(2023, 9, 4) + timedelta(days=i), 10, 0, 500.0) for i in range(40)]  # inside window 1
    sel = walk_forward_select(good + late, c)
    first = sel[0]
    assert ("A", "VOLATILITY_NORMAL") in first.selected
    assert all(k[0] != "B" for k in first.stats)  # B's trades are in the test window: invisible to its selection
    assert ("B", "VOLATILITY_NORMAL") in sel[1].selected


def test_losing_or_thin_pairs_are_not_selected() -> None:
    c = _cfg()
    losers = [_t("L", date(2023, 1, 2) + timedelta(days=i), 10, 0, -50.0 + (i % 2)) for i in range(40)]
    thin = [_t("T", date(2023, 1, 2) + timedelta(days=i), 11, 0, 300.0) for i in range(5)]
    sel = walk_forward_select(losers + thin, c)[0]
    assert sel.selected == {}
    assert sel.stats[("T", "VOLATILITY_NORMAL")].n == 5


def test_book_limits() -> None:
    c = _cfg()
    d = date(2024, 1, 2)
    cands = [
        (_t("S-ORB-001", d, 9, 40, 100.0), 1.0),
        (_t("S-VWAPMR-001", d, 9, 50, 50.0), 0.5),  # overlaps the first (one position at a time)
        (_t("S-FBO-001", d, 10, 30, 50.0), 0.5),  # exclusive group with ORB-001 today
        (_t("S-ORB-001", d, 11, 0, 50.0), 0.5),  # its own cap (1 a day)
        (_t("S-VWAPMR-001", d, 12, 0, 20.0, risk=300.0), 0.5),  # > 2% of NAV 10k
        (_t("S-GAPGO-001", d, 13, 0, 30.0), 0.5),
    ]
    caps = {"S-ORB-001": 1, "S-VWAPMR-001": 2, "S-FBO-001": 1, "S-GAPGO-001": 1}
    r = simulate_book(cands, nav0=10_000.0, days=[d, d + timedelta(days=1)], caps=caps, cfg=c)
    assert [t.strategy for t in r.taken] == ["S-ORB-001", "S-GAPGO-001"]
    assert r.skipped == {"EXCLUSIVE_GROUP": 1, "ONE_POSITION": 1, "RISK_OVER_2PCT_OF_NAV": 1,
                         "STRATEGY_MAX_ENTRIES": 1}  # fmt: skip
    assert r.daily == {d: 130.0, d + timedelta(days=1): 0.0}


def test_daily_stop_cooldown_and_dd_suspension() -> None:
    c = _cfg()
    d = date(2024, 1, 2)
    caps = {"X": 10}
    cands = [
        (_t("X", d, 9, 40, -250.0, hold=10, why="STOP"), 1.0),
        (_t("X", d, 9, 55, 10.0), 1.0),  # 5 min after the stop: cooldown
        (_t("X", d, 10, 30, -200.0, hold=10), 1.0),  # loss: realised day loss now 450 >= 4% of 10k
        (_t("X", d, 11, 0, 10.0), 1.0),  # daily stop
    ]
    r = simulate_book(cands, nav0=10_000.0, days=[d], caps=caps, cfg=c)
    assert r.skipped == {"COOLDOWN_AFTER_STOP": 1, "DAILY_STOP": 1}
    days = [d + timedelta(days=i) for i in range(10)]
    big = [(_t("X", days[i], 10, 0, -390.0), 1.0) for i in range(10)]
    r2 = simulate_book(big, nav0=10_000.0, days=days, caps=caps, cfg=c)
    assert r2.suspended_on is not None
    assert r2.skipped.get("DD_SUSPENDED", 0) + r2.skipped.get("WEEKLY_FREEZE", 0) > 0
    m = book_metrics(r2)
    assert m["max_drawdown"] >= 0.125
    assert m["net_pnl"] < 0


def test_metrics_on_a_flat_and_a_steady_book() -> None:
    c = _cfg()
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(252)]
    r = simulate_book([], nav0=10_000.0, days=days, caps={}, cfg=c)
    m = book_metrics(r)
    assert m["trades"] == 0 and m["net_cagr"] == 0.0 and m["sharpe"] is None and m["hit_rate"] is None
    cands = [(_t("X", days[i], 10, 0, 20.0 if i % 4 else -10.0), 1.0) for i in range(252)]
    m2 = book_metrics(simulate_book(cands, nav0=10_000.0, days=days, caps={"X": 1}, cfg=c))
    assert m2["trades"] == 252
    assert m2["hit_rate"] == pytest.approx(0.75, abs=0.01)
    assert m2["net_cagr"] > 0 and m2["sharpe"] > 0 and m2["sortino"] > m2["sharpe"]
    assert m2["calmar"] is not None and m2["expectancy_r"] == pytest.approx(m2["expectancy_inr"] / 150.0, rel=1e-3)


def test_cells_and_exclusions_from_signal_tags() -> None:
    assert regime_cell(["MEAN_REVERTING", "VOLATILITY_EXPANSION"], ["vol"]) == "VOLATILITY_EXPANSION"
    assert regime_cell(["TRENDING_UP", "VOLATILITY_NORMAL"], ["vol", "trend"]) == "VOLATILITY_NORMAL+TREND_UP"
    assert regime_cell(["TRENDING_UP"], ["vol"]) is None
    assert exclusion(["VOLATILITY_NORMAL", "EVENT_REGIME"], []) == "EVENT_DAY"
    assert exclusion(["NO_EDGE"], []) == "NO_EDGE_OR_WARMUP"
    assert exclusion(["ABNORMAL_MARKET"], []) == "ABNORMAL"
    assert exclusion(["VOLATILITY_EXPANSION", "TRENDING_UP"], ["TRENDING_UP"]) == "SPEC_PROHIBITED"
    assert exclusion(["VOLATILITY_EXPANSION"], ["TRENDING_UP"]) is None


def test_run_records_become_book_trades_and_a_cell_table() -> None:
    def rec(day: str, pnl: str, tags: list[str]) -> dict[str, object]:
        return {"day": day, "signal_at": f"{day}T09:50:00+05:30", "entry_ts": f"{day}T09:52:00+05:30",
                "exit_ts": f"{day}T10:30:00+05:30", "net_pnl": pnl, "charges": "50", "slippage": "10",
                "risk_at_stop": "200", "exit_reason": "TARGET", "lib_regime_tags": tags}  # fmt: skip

    doc = {"strategy": "S-X-001", "trades": [
        rec("2024-03-06", "100", ["VOLATILITY_NORMAL"]), rec("2024-03-07", "-50", ["VOLATILITY_NORMAL"]),
        rec("2024-03-08", "300", ["VOLATILITY_EXPANSION"]), rec("2024-03-11", "999", ["EVENT_REGIME"]),
        rec("2024-03-12", "999", ["VOLATILITY_LOW", "TRENDING_UP"])]}  # fmt: skip
    trades, skipped = book_trades_from_run(doc, dims=["vol"], prohibited=["TRENDING_UP"])
    assert len(trades) == 3 and skipped == {"EVENT_DAY": 1, "SPEC_PROHIBITED": 1}
    assert trades[0].day == date(2024, 3, 6) and trades[0].signal_at.utcoffset() == timedelta(hours=5, minutes=30)
    rows = {(r["strategy"], r["cell"]): r for r in cell_table(trades)}
    assert rows[("S-X-001", "ALL")]["trades"] == 3 and rows[("S-X-001", "ALL")]["net"] == 350
    n = rows[("S-X-001", "VOLATILITY_NORMAL")]
    assert n["hit_rate"] == 0.5 and n["expectancy_inr"] == 25 and n["expectancy_r"] == 0.125


def test_label_stream_tags_override_plugin_tags() -> None:
    base = {"warmup": "0", "trend": "UP", "vol": "EXPANSION", "gap": "GAP_UP", "opening": "UNDETERMINED",
            "expiry": "1", "event": "0", "abnormal": "0"}  # fmt: skip
    rows = [{**base, "ts": "2024-03-06T09:49:00+05:30"},
            {**base, "ts": "2024-03-06T09:50:00+05:30", "vol": "NORMAL"},
            {**base, "ts": "2024-03-07T09:50:00+05:30", "warmup": "1"}]  # fmt: skip
    li = LabelIndex(rows)
    t1 = li.at(datetime(2024, 3, 6, 9, 52, tzinfo=IST))
    assert t1 == frozenset({"TRENDING_UP", "VOLATILITY_NORMAL", "GAP_REGIME", "EXPIRY_REGIME"})
    assert li.at(datetime(2024, 3, 6, 9, 48, tzinfo=IST)) is None
    assert li.at(datetime(2024, 3, 7, 10, 0, tzinfo=IST)) == frozenset({"NO_EDGE", "GAP_REGIME", "EXPIRY_REGIME"})
    rec = {"day": "2024-03-06", "signal_at": "2024-03-06T09:50:00+05:30", "entry_ts": "2024-03-06T09:52:00+05:30",
           "exit_ts": "2024-03-06T10:30:00+05:30", "net_pnl": "1", "charges": "1", "slippage": "1",
           "risk_at_stop": "10", "exit_reason": "TIME", "lib_regime_tags": []}  # fmt: skip
    tr, sk = book_trades_from_run({"strategy": "S-ORB-002", "trades": [rec]}, dims=["vol"], prohibited=[], labels=li)
    assert tr[0].cell == "VOLATILITY_NORMAL" and not sk
    tr, sk = book_trades_from_run({"strategy": "S-ORB-002", "trades": [rec]}, dims=["vol"], prohibited=[])
    assert not tr and sk == {"NO_CELL": 1}


def test_mean_diff_ci() -> None:
    d0 = date(2024, 1, 1)
    a = [(d0 + timedelta(days=i), 1.0 + (i % 3) * 0.1) for i in range(60)]
    b = [(d0 + timedelta(days=i), (i % 3) * 0.1) for i in range(60)]
    got = mean_diff_ci(a, b, level=0.9, iterations=500, seed=1)
    assert got is not None and abs(got[0] - 1.0) < 1e-9 and got[1] > 0.9 and got[2] < 1.1
    assert mean_diff_ci([], b, level=0.9, iterations=10, seed=1) is None

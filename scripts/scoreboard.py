#!/usr/bin/env python3
"""Economic scoreboard: one table per candidate (research plane; reads trade histories, places nothing).

  scripts/scoreboard.py --candidates <file.toml> [--out report.md]

The candidate file is private (alpha parameters and capital assumptions stay out of the public repository); by
default it is read from $AXIOM_PRIVATE/scoreboard/candidates.toml (AXIOM_PRIVATE defaults to <repo>/private_alpha).
Each [[candidate]] names its trade history (per-lot net P&L after costs), the capital inputs and its forward-test
record directory. Per candidate the table shows expectancy after costs with a 90% CI, hit rate, drawdown, trades a
year, the minimum viable capital (economics/mvc.py) under the owner's 2% per-trade rule and under a 5% research
budget, the annual net return on that capital and a mechanical first decision (the owner decides).

Candidate keys: id, rationale, selection (how many variants it was chosen from), source = {kind, path, ...},
cash_per_lot, worst_loss = "max_defined" | "premium_p95" | a number, stress_loss_per_lot (optional), forward_dir.
Source kinds: "sell_trades_json" (key, from, to; rows [day, entry, exit, net, R, reason, max_loss]) and "csv"
(day_col, pnl_col, optional lot_col + current_lot to rescale per-lot P&L, premium_col for premium_p95).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import statistics
import sys
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.economics.mvc import (  # noqa: E402
    CapitalInputs,
    CapitalPolicy,
    fmt_inr,
    minimum_viable_capital,
    suggest_decision,
    trade_stats,
)

PRIVATE = Path(os.environ.get("AXIOM_PRIVATE", str(REPO / "private_alpha")))


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def load(src: dict[str, Any], lake_root: Path) -> tuple[list[tuple[date, float]], dict[str, float]]:
    """(trades [(day, per-lot net)], facts) from a source block."""
    path = Path(src["path"]) if Path(src["path"]).is_absolute() else lake_root / src["path"]
    lo = date.fromisoformat(str(src.get("from", "1900-01-01")))
    hi = date.fromisoformat(str(src.get("to", "2999-12-31")))
    facts: dict[str, float] = {}
    out: list[tuple[date, float]] = []
    if src["kind"] == "sell_trades_json":
        rows = json.loads(path.read_text())[src["key"]]
        sel = [r for r in rows if lo <= date.fromisoformat(r[0]) <= hi]
        out = [(date.fromisoformat(r[0]), float(r[3])) for r in sel]
        facts["max_defined"] = max(float(r[6]) for r in sel)
        facts["median_defined"] = statistics.median(float(r[6]) for r in sel)
    elif src["kind"] == "csv":
        raw = gzip.open(path, "rt") if path.suffix == ".gz" else path.open()
        with raw as fh:
            rows = list(csv.DictReader(io.StringIO(fh.read())))
        cur = float(src.get("current_lot", 0)) or None
        prem: list[float] = []
        for r in rows:
            d = date.fromisoformat(r[src["day_col"]])
            if not lo <= d <= hi:
                continue
            pnl = float(r[src["pnl_col"]])
            if cur and src.get("lot_col"):
                pnl *= cur / float(r[src["lot_col"]])
            out.append((d, pnl))
            if src.get("premium_col") and cur:
                prem.append(float(r[src["premium_col"]]) * cur)
        if prem:
            facts["premium_p95"] = _pct(prem, 0.95)
            facts["premium_median"] = statistics.median(prem)
    else:
        raise SystemExit(f"unknown source kind {src['kind']}")
    return out, facts


def forward_count(d: str | None) -> str:
    if not d:
        return "not tracked"
    p = Path(d)
    n = len(list(p.glob("*.json"))) if p.exists() else 0
    return f"{n} sessions recorded (blind until the single evaluation)"


def table(c: dict[str, Any], lake_root: Path) -> str:
    trades, facts = load(c["source"], lake_root)
    s = trade_stats(trades)
    wl = c["worst_loss"]
    worst = float(facts[wl]) if isinstance(wl, str) else float(wl)
    cash = float(facts[c["cash_per_lot"]]) if isinstance(c["cash_per_lot"], str) else float(c["cash_per_lot"])
    x = CapitalInputs(cash, worst, c.get("stress_loss_per_lot"), s.max_drawdown)
    m2 = minimum_viable_capital(x, CapitalPolicy(risk_frac=0.02))
    m5 = minimum_viable_capital(x, CapitalPolicy(risk_frac=0.05))
    ci = "-" if s.ci is None else f"{fmt_inr(s.ci[0])} .. {fmt_inr(s.ci[1])}"
    rows = [
        ("Economic rationale", c.get("rationale", "")),
        ("Evidence window", f"{s.first} .. {s.last} ({s.years} yrs, {c['source'].get('label', '')})"),
        ("Selection", c.get("selection", "")),
        ("Trades (per year)", f"{s.n} ({s.trades_per_year})"),
        ("Net per lot per trade, after costs", f"{fmt_inr(s.mean)} (90% CI {ci})"),
        (
            "Hit rate / profit factor",
            f"{s.hit_rate:.0%} / {s.profit_factor:.2f}" if s.profit_factor else f"{s.hit_rate:.0%}",
        ),
        ("Positive months", f"{s.positive_month_share:.0%}"),
        ("Worst trade / max drawdown (per lot)", f"{fmt_inr(s.worst_trade)} / {fmt_inr(-s.max_drawdown)}"),
        ("Annual net per lot", fmt_inr(s.annual_net)),
        ("Cash or margin per lot", f"{fmt_inr(cash)} ({c.get('cash_note', '')})"),
        ("Worst-case loss per trade (per lot)", f"{fmt_inr(worst)} ({wl})"),
        ("Stress loss per lot", fmt_inr(c.get("stress_loss_per_lot"))),
        ("MVC, 2% per-trade rule", f"{fmt_inr(m2.capital)} (binding: {m2.binding}; {m2.components})"),
        ("MVC, 5% per-trade research budget", f"{fmt_inr(m5.capital)} (binding: {m5.binding})"),
        ("Annual net / MVC (2% / 5%)", f"{s.annual_net / m2.capital:.1%} / {s.annual_net / m5.capital:.1%}"),
        ("Forward test", forward_count(c.get("forward_dir"))),
        ("Mechanical first read", suggest_decision(s)),
    ]
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return f"### {c['id']}\n\n| | |\n|---|---|\n{body}\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", type=Path, default=PRIVATE / "scoreboard" / "candidates.toml")
    ap.add_argument("--lake", type=Path, default=REPO / "lake")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    cfg = tomllib.loads(a.candidates.read_text())
    md = "\n".join(table(c, a.lake) for c in cfg["candidate"])
    md = f"## Economic scoreboard (per lot, net of costs; SIMULATED/backtest unless stated)\n\n{md}"
    print(md)
    if a.out:
        a.out.write_text(md + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

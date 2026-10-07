#!/usr/bin/env python3
"""H01 (S-ORB-001) end to end on REAL Dhan data from the local lake (research only; no orders, no network).

Segments of ~3 months are run separately (memory), each with a 35-day warm-up in which the futures proxy and VIX
history accumulate but no option bars are fed, so no trade can happen before the segment starts. Results are summed.

Inputs (selected explicitly, through manifests, by ``DhanLakeReader``): NIFTY index and India VIX 1-minute, nearest-
and next-weekly options ATM±2 CE+PE (a subset of the downloaded ATM±10 / ATM±3 chunks), and the ASSUMED futures proxy
(``backtest/proxies.py``). DQ WARN parts are accepted (``accept_warn``): the index carries a 15:30 print on some days
and VALUE_ROUNDED float noise; BLOCKED parts are never used.

  scripts/h01_real_data.py --from 2025-10-02 --to 2026-10-01 --nav 10000
  scripts/h01_real_data.py --from 2025-10-02 --to 2026-10-01 --nav 100000 --label HYPOTHETICAL

``--hypothesis H01b`` runs S-ORB-002 instead (specs/S-ORB-002.yaml): the same mechanics on the H01b signal series
(index price, near-the-money option volume; ``strategies/orb_h01b.py``), which is its definition, not a proxy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.backtest import BacktestEngine, BarFillModel, LatencyModel  # noqa: E402
from project100c.backtest.engine import BacktestConfig  # noqa: E402
from project100c.backtest.feed import ReplayFeed  # noqa: E402
from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.backtest.proxies import FUTURES_PROXY_ASSUMPTION, futures_volume_proxy  # noqa: E402
from project100c.backtest.report import write_run_artifacts  # noqa: E402
from project100c.backtest.spreads import SyntheticSpreadModel, SyntheticSpreadProvider, load_spread_params  # noqa: E402
from project100c.calendar import (  # noqa: E402
    ExpiryCalendar,
    MarketClock,
    TradingCalendar,
    load_expiry_rules,
    load_holiday_book,
)
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import ACTIVE_FUTURES, INDEX_LABEL, VIX_LABEL, plan_backfill  # noqa: E402
from project100c.data.dhan.jobs import CandleJobSpec, RollingOptionJobSpec  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.kernel.limits import load_risk_limits  # noqa: E402
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows  # noqa: E402
from project100c.spec.checks import check_cost_reference  # noqa: E402
from project100c.spec.io import load_spec_file  # noqa: E402
from project100c.strategies.orb_h01 import OrbH01Strategy, OrbParams, h01_metadata  # noqa: E402
from project100c.strategies.orb_h01b import (  # noqa: E402
    H01B_SIGNAL_DEFINITION,
    OrbH01bStrategy,
    h01b_metadata,
    h01b_signal_bars,
)

CONFIGS = REPO / "configs"
SPEC = REPO / "tests" / "fixtures" / "spec" / "S-ORB-001.yaml"
SPEC_H01B = REPO / "specs" / "S-ORB-002.yaml"
H01B_SIGNAL = "NIFTY-H01B-SIGNAL"
FUT_PROXY = "NIFTY-FUTPROXY"
ONE_MIN = timedelta(minutes=1)
WARMUP = timedelta(days=35)
OFFSETS = (-2, -1, 0, 1, 2)


def _overlaps(s: RollingOptionJobSpec | CandleJobSpec, d0: date, d1: date) -> bool:
    return s.from_date <= d1 and s.to_date > d0


def _jobs(
    anchor: date, d0: date, d1: date, fut_label: str | None = None
) -> tuple[list[RollingOptionJobSpec], list[CandleJobSpec]]:
    opts: list[RollingOptionJobSpec] = []
    cands: list[CandleJobSpec] = []
    for it in plan_backfill(anchor=anchor, futures=ACTIVE_FUTURES):
        s = it.spec
        if not _overlaps(s, d0, d1):
            continue
        if isinstance(s, RollingOptionJobSpec):
            opts.append(s.model_copy(update={"strike_offsets": OFFSETS}))  # a subset of the downloaded chunks
        elif s.label in (INDEX_LABEL, VIX_LABEL) or s.label == fut_label:
            cands.append(s)
    return opts, cands


def run_segment(a: argparse.Namespace, seg0: date, seg1: date, out: Path) -> dict[str, Any]:
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    h01b = a.hypothesis == "H01b"
    spec = load_spec_file(SPEC_H01B if h01b else SPEC)
    book = load_charge_book(CONFIGS / "costs" / "nse_fo_index_options.toml")
    plans = load_brokerage_plans(CONFIGS / "costs" / "brokerage_plans.toml")
    check_cost_reference(spec, book, plans)
    costs = CostModel(book, plans)
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    sessions = SessionCalendar(
        load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
    )
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    lots = load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")
    w0 = seg0 - WARMUP
    opts, cands = _jobs(date.fromisoformat(a.anchor), w0, seg1, a.futures_label)
    dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    reader = DhanLakeReader(
        Lake(a.lake),
        config=cfg,
        expiries=expiries,
        lots=lots,
        accept_warn=True,
        max_missing_fraction=dq.max_missing_candle_fraction,  # OD-016: conflicts count toward it
    )
    data = reader.load(option_jobs=opts, candle_jobs=cands, date_from=w0, date_to=seg1)
    idx = data.candles[INDEX_LABEL]
    fut_key = H01B_SIGNAL if h01b else (a.futures_label or FUT_PROXY)
    proxy = (
        h01b_signal_bars(
            label=H01B_SIGNAL,
            index_bars=idx,
            option_bars=data.option_bars,
            contracts=data.contracts,
            strike_step=cfg.nifty_strike_step,
        )
        if h01b
        else data.candles[a.futures_label]
        if a.futures_label
        else futures_volume_proxy(
            label=FUT_PROXY,
            index_bars=idx,
            option_bars=data.option_bars,
            contracts=data.contracts,
            strike_step=cfg.nifty_strike_step,
        )
    )
    seg_opts = [b for b in data.option_bars if b.start.astimezone(IST).date() >= seg0]
    bars = [*seg_opts, *idx, *data.candles[VIX_LABEL], *proxy]
    spread = SyntheticSpreadProvider(
        SyntheticSpreadModel(load_spread_params(CONFIGS / "backtest" / "synthetic_spreads.toml")),
        data.contracts,
        data.spot_at,
        cal,
        cfg.nifty_strike_step,
    )
    limits = load_risk_limits(CONFIGS / "risk" / "limits.toml")
    nav = Decimal(a.nav)
    params = OrbParams.from_spec(spec)
    strat = (OrbH01bStrategy if h01b else OrbH01Strategy)(
        params,
        fut_key=fut_key,
        index_key=INDEX_LABEL,
        vix_key=VIX_LABEL,
        contracts=data.contracts,
        costs=costs,
        plan_id=spec.cost_assumptions.brokerage_plan,
        risk_budget=limits.per_trade_max_loss_frac * nav,
    )
    engine = BacktestEngine(
        clock=MarketClock(cal, sessions),
        costs=costs,
        bar_model=BarFillModel(interval=ONE_MIN, spread=spread),
        latency=LatencyModel(),
        config=BacktestConfig(plan_id=spec.cost_assumptions.brokerage_plan, starting_cash=nav),
    )
    meta = {
        "label": a.label,
        "segment": {"from": seg0.isoformat(), "to": seg1.isoformat(), "warmup_from": w0.isoformat()},
        "strategy": h01b_metadata(params) if h01b else h01_metadata(params),
        "data": data.metadata(),
        "risk": {"limits": limits.version, "per_trade_max_loss_frac": limits.per_trade_max_loss_frac, "nav": nav},
        "cost_model": spec.cost_assumptions.cost_model_version,
        "calendar": cal.version,
        "assumptions": [
            H01B_SIGNAL_DEFINITION
            if h01b
            else FUTURES_PROXY_ASSUMPTION
            if not a.futures_label
            else f"REAL futures input: Dhan 1-minute candles of the active contract {a.futures_label}",
            "ASSUMED synthetic spreads (configs/backtest/synthetic_spreads.toml); Dhan history has no bid/ask",
            "DQ WARN parts accepted; BLOCKED (quarantined) parts excluded",
            "OD-016: conflicting duplicate rows across Dhan series are dropped (missing minutes, WARN)",
            "REAL market data: Dhan Data API 1-minute bars (no quotes)",
        ],
    }
    result = engine.run(ReplayFeed.from_bars(bars, interval=ONE_MIN), strat, metadata=meta)
    seg_trades = [t for t in strat.trades if t["day"] >= seg0]
    write_run_artifacts(result, out, strategy_report={"counters": strat.counters, "trades": seg_trades})
    return {
        "segment": f"{seg0}..{seg1}",
        "net_pnl": result.net_pnl,
        "gross_pnl": result.gross_pnl,
        "charges": result.total_charges,
        "fills": len(result.fills),
        "flat_at_end": result.flat_at_end,
        "rejects": result.rejects,
        "outcomes": Counter(str(t.get("outcome")) for t in seg_trades),
        "exit_reasons": Counter(str(t.get("exit_reason")) for t in seg_trades if t.get("outcome") == "CLOSED"),
        "counters": dict(strat.counters),
        "closed": [
            {
                k: str(t.get(k))
                for k in ("day", "right", "contract", "entry_fill", "exit_fill", "exit_reason", "risk_at_stop")
            }
            for t in seg_trades
            if t.get("outcome") == "CLOSED"
        ],
        "risk_at_stop": [str(t["risk_at_stop"]) for t in seg_trades if "risk_at_stop" in t],
        "trading_days_with_options": len({b.start.astimezone(IST).date() for b in seg_opts}),
        "ledger_hash": result.ledger_hash,
        "data_fingerprints": [r.fingerprint() for r in data.reports],
        "skipped_parts": [r.skipped_by_reason() for r in data.reports],
        "conflicts": [
            {
                "minutes": r.conflict_minutes,
                "rows_dropped": r.conflict_rows_dropped,
                "series_days_excluded": len(r.conflict_series_days_excluded),
            }
            for r in data.reports
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="d0", required=True)
    p.add_argument("--to", dest="d1", required=True, help="inclusive")
    p.add_argument("--nav", required=True)
    p.add_argument("--label", default="REAL-DATA BACKTEST")
    p.add_argument("--segment-days", type=int, default=92)
    p.add_argument("--anchor", default="2026-10-02")
    p.add_argument(
        "--futures-label",
        choices=[f.label for f in ACTIVE_FUTURES],
        help="use a REAL active futures contract instead of the ASSUMED proxy (cross-check; its history is short)",
    )
    p.add_argument(
        "--hypothesis",
        choices=["H01", "H01b"],
        default="H01",
        help="H01b = S-ORB-002 (index price, near-the-money option volume); H01 behaviour is unchanged",
    )
    p.add_argument("--lake", type=Path, default=REPO / "lake")
    p.add_argument("--out", type=Path, default=REPO / "lake" / "runs")
    a = p.parse_args()
    if a.hypothesis == "H01b" and a.futures_label:
        p.error("--futures-label applies to H01 only (H01b's signal series is defined on option volume)")
    d0, d1 = date.fromisoformat(a.d0), date.fromisoformat(a.d1)
    segs: list[tuple[date, date]] = []
    s = d0
    while s <= d1:
        e = min(d1, s + timedelta(days=a.segment_days - 1))
        segs.append((s, e))
        s = e + timedelta(days=1)
    tag = f"{a.hypothesis.lower()}-{a.d0}-{a.d1}-nav{a.nav}" + ("-realfut" if a.futures_label else "")
    results = []
    for s0, s1 in segs:
        r = run_segment(a, s0, s1, a.out / tag / f"{s0}")
        results.append(r)
        print(json.dumps({k: v for k, v in r.items() if k not in ("closed", "risk_at_stop")}, default=str), flush=True)
    (a.out / tag).mkdir(parents=True, exist_ok=True)
    (a.out / tag / "segments.json").write_text(json.dumps(results, default=str, indent=1))
    tot = {k: sum((r[k] for r in results), Decimal(0)) for k in ("net_pnl", "gross_pnl", "charges")}
    print(json.dumps({"label": a.label, "nav": a.nav, "total": tot, "segments": len(results)}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

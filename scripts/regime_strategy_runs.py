#!/usr/bin/env python3
"""Every strategy in the library (11 plug-ins, H01 and H01b) on REAL Dhan data, UNGATED, per segment (research only).

Each segment's data is loaded once and every strategy runs on it at each NAV. The regime gate is OFF, so every
signal the strategy would take in any regime is traded. The trades are then sliced by the regime labels of the
standalone classifier stream (``scripts/regime_research.py``), and the regime -> strategy map is chosen
walk-forward from those slices. Per-trade risk cap: 2% of NAV (limits.toml); one lot (OD-014); ASSUMED synthetic
spreads; dated, VERIFIED costs; DQ WARN parts accepted, BLOCKED parts excluded; OD-016 conflicts dropped.

Segments never span a NIFTY lot revision (the reader holds one lot per contract). Index and VIX load from 35 days
before the segment (classifier and gap history). Options load from the later of that date and the last lot
revision on or before the segment start, so the H01 proxy / H01b signal series have their 20-day history wherever
the lot allows it. Only option bars inside the segment are fed, so no trade can happen in the warm-up.

  scripts/regime_strategy_runs.py --from 2022-08-29 --to 2026-10-01 --navs 10000 100000 --jobs 3
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time as _time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from h01_real_data import _jobs  # noqa: E402

from project100c.backtest import BacktestEngine, BarFillModel, LatencyModel  # noqa: E402
from project100c.backtest.engine import BacktestConfig  # noqa: E402
from project100c.backtest.feed import ReplayFeed  # noqa: E402
from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.backtest.proxies import futures_volume_proxy  # noqa: E402
from project100c.backtest.spreads import SyntheticSpreadModel, SyntheticSpreadProvider, load_spread_params  # noqa: E402
from project100c.calendar import (  # noqa: E402
    ExpiryCalendar,
    MarketClock,
    TradingCalendar,
    load_expiry_rules,
    load_holiday_book,
)
from project100c.calendar.events import load_event_book  # noqa: E402
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import INDEX_LABEL, VIX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.kernel.limits import load_risk_limits  # noqa: E402
from project100c.regime import load_regime_config  # noqa: E402
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows  # noqa: E402
from project100c.spec.io import load_spec_file  # noqa: E402
from project100c.strategies.library import PLUGINS, LongOptionStrategy  # noqa: E402
from project100c.strategies.orb_h01 import OrbH01Strategy, OrbParams  # noqa: E402
from project100c.strategies.orb_h01b import OrbH01bStrategy, h01b_signal_bars  # noqa: E402
from project100c.validation.trades import paired_trades_from_library_run  # noqa: E402

CONFIGS = REPO / "configs"
ONE_MIN = timedelta(minutes=1)
FUT_PROXY = "NIFTY-FUTPROXY"
H01B_SIGNAL = "NIFTY-H01B-SIGNAL"
# The library base decides on the index bar using the SAME minute's option bar, so within a minute the index must come
# AFTER the option keys ("NIFTY|..."). The Dhan label "NIFTY-INDEX" sorts before them ('-' < '|'), which made every
# library entry NO_OPTION_BAR. Library runs feed with ReplayFeed(decision_keys=(index,)) ("decision-keys"); the first
# runs relabelled the index "NIFTY~INDEX" instead ("relabel": the same event order, kept to re-run them). H01/H01b
# retry until the option bar arrives, so they keep the plain key order (their earlier runs stay reproducible).
LIB_INDEX = "NIFTY~INDEX"
LOT_REVISIONS = (date(2024, 4, 26), date(2024, 11, 21), date(2025, 10, 29))
H01_SPEC = REPO / "tests" / "fixtures" / "spec" / "S-ORB-001.yaml"  # H01 as pre-registered (unchanged)
STRATEGIES = (*sorted(PLUGINS), "S-ORB-001", "S-ORB-002")
# the volatility round: daily H25/H26 features, and the
# H36 no-cap diagnostic "<spec>~NOCAP" (entries up to the system cap of 10, no stand-down after losses)
VOL_FEATURE_SPECS = ("S-VOLCHEAP-001", "S-VOLHOLD-001", "S-EXPVOL-001")
NOCAP = "~NOCAP"
# the top-traders candidates H37-H40: daily
# index features from scripts/daily_index_features.py (completed sessions only)
TT_FEATURE_SPECS = ("S-TRDAY-001", "S-NR7-001", "S-OOPS-001", "S-PBTREND-001")


def vol_features(path: Path | None) -> dict[date, dict[str, Decimal]]:
    """scripts/vol_features.py output: h25 / h25_from / h26 per day (point in time)."""
    if path is None or not path.exists():
        raise SystemExit("the volatility specs need --vol-features (run scripts/vol_features.py first)")
    out: dict[date, dict[str, Decimal]] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            out[date.fromisoformat(r["day"])] = {k: Decimal(v) for k, v in r.items() if k != "day" and v}
    return out


def load_variant(name: str) -> Any:
    """The spec of a strategy name; '<id>~NOCAP' lifts the spec's daily cap to 10 and drops the loss stand-down."""
    sid = name.removesuffix(NOCAP)
    spec = load_spec_file(REPO / "specs" / f"{sid}.yaml")
    if name == sid:
        return spec
    d = spec.model_dump(mode="json")
    d["entry"]["max_entries_per_day"] = 10
    d["signal"]["params"].pop("max_losses_per_day", None)
    return type(spec).model_validate(d)


def segments(d0: date, d1: date, days: int) -> list[tuple[date, date]]:
    cuts = sorted({d0, *[r for r in LOT_REVISIONS if d0 < r <= d1]})
    out: list[tuple[date, date]] = []
    for i, c in enumerate(cuts):
        end = cuts[i + 1] - timedelta(days=1) if i + 1 < len(cuts) else d1
        s = c
        while s <= end:
            e = min(end, s + timedelta(days=days - 1))
            out.append((s, e))
            s = e + timedelta(days=1)
    return out


def gex_features(path: Path | None) -> dict[date, dict[str, Decimal]]:
    """H22's point-in-time daily feature (scripts/feature_gex.py): gex_rank of the 10:15 gamma concentration."""
    if path is None or not path.exists():
        raise SystemExit("S-GEXMO-001 needs --gex-features (run scripts/feature_gex.py first)")
    out: dict[date, dict[str, Decimal]] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            if r.get("gex_rank"):
                out[date.fromisoformat(r["day"])] = {"gex_rank": Decimal(r["gex_rank"])}
    return out


def _plain(x: Any) -> Any:
    if isinstance(x, Decimal | date):
        return str(x)
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set | frozenset):
        return [_plain(v) for v in x]
    return x


def run_segment(
    seg0: date,
    seg1: date,
    navs: list[str],
    strategies: list[str],
    out: Path,
    lake: Path,
    clf_version: str,
    index_order: str = "decision-keys",
    warmup_days: int = 35,
    gex_csv: Path | None = None,
    vol_csv: Path | None = None,
) -> dict[str, Any]:
    t_start = _time.time()
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    book = load_charge_book(CONFIGS / "costs" / "nse_fo_index_options.toml")
    plans = load_brokerage_plans(CONFIGS / "costs" / "brokerage_plans.toml")
    costs = CostModel(book, plans)
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    sessions = SessionCalendar(
        load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
    )
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    events = load_event_book(CONFIGS / "calendar" / "events.yaml").to_calendar(cal)
    # pinned: the strategies' own regime exits must not change when a newer classifier block is adopted
    regime = load_regime_config(CONFIGS / "regime" / "classifier.toml", version=clf_version)
    lots = load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")
    limits = load_risk_limits(CONFIGS / "risk" / "limits.toml")
    dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    w0 = seg0 - timedelta(days=warmup_days)
    o0 = max([w0, *[r for r in LOT_REVISIONS if r <= seg0]])
    opts, cands = _jobs(date(2026, 10, 2), w0, seg1)
    reader = DhanLakeReader(Lake(lake), config=cfg, expiries=expiries, lots=lots, accept_warn=True,
                            max_missing_fraction=dq.max_missing_candle_fraction)  # fmt: skip
    od = reader.load(option_jobs=opts, candle_jobs=[], date_from=o0, date_to=seg1)
    cd = reader.load(option_jobs=[], candle_jobs=cands, date_from=w0, date_to=seg1)
    idx = cd.candles[INDEX_LABEL]
    vix = cd.candles[VIX_LABEL]
    proxy = futures_volume_proxy(label=FUT_PROXY, index_bars=idx, option_bars=od.option_bars,
                                 contracts=od.contracts, strike_step=cfg.nifty_strike_step)  # fmt: skip
    h01b_sig = h01b_signal_bars(label=H01B_SIGNAL, index_bars=idx, option_bars=od.option_bars,
                                contracts=od.contracts, strike_step=cfg.nifty_strike_step)  # fmt: skip
    relabel = index_order == "relabel"
    lib_key = LIB_INDEX if relabel else INDEX_LABEL
    lib_idx = [replace(b, instrument_key=LIB_INDEX) for b in idx] if relabel else idx
    assert not relabel or all(LIB_INDEX > k for k in od.contracts)
    seg_opts = [b for b in od.option_bars if b.start.astimezone(IST).date() >= seg0]
    spread = SyntheticSpreadProvider(
        SyntheticSpreadModel(load_spread_params(CONFIGS / "backtest" / "synthetic_spreads.toml")),
        od.contracts, od.spot_at, cal, cfg.nifty_strike_step,
    )  # fmt: skip
    load_s = _time.time() - t_start
    data_meta = {
        "options": od.metadata(), "candles": cd.metadata(),
        "conflicts": [{"minutes": r.conflict_minutes, "rows_dropped": r.conflict_rows_dropped,
                       "series_days_excluded": len(r.conflict_series_days_excluded)} for r in od.reports],
        "skipped_parts": [r.skipped_by_reason() for r in od.reports],
        "fingerprints": [r.fingerprint() for r in (*od.reports, *cd.reports)],
        "option_days": len({b.start.astimezone(IST).date() for b in seg_opts}),
    }  # fmt: skip
    summary: dict[str, Any] = {"segment": f"{seg0}..{seg1}", "load_s": round(load_s, 1), "runs": []}
    for sid in strategies:
        for nav_s in navs:
            t0 = _time.time()
            nav = Decimal(nav_s)
            budget = limits.per_trade_max_loss_frac * nav
            if sid in ("S-ORB-001", "S-ORB-002"):
                h01b = sid == "S-ORB-002"
                spec = load_spec_file(REPO / "specs" / "S-ORB-002.yaml" if h01b else H01_SPEC)
                sig = h01b_sig if h01b else proxy
                strat: Any = (OrbH01bStrategy if h01b else OrbH01Strategy)(
                    OrbParams.from_spec(spec), fut_key=sig[0].instrument_key if sig else H01B_SIGNAL,
                    index_key=INDEX_LABEL, vix_key=VIX_LABEL, contracts=od.contracts, costs=costs,
                    plan_id=spec.cost_assumptions.brokerage_plan, risk_budget=budget,
                )  # fmt: skip
                bars = [*seg_opts, *idx, *vix, *sig]
            else:
                spec = load_variant(sid)
                base_id = sid.removesuffix(NOCAP)
                feats = (gex_features(gex_csv) if base_id == "S-GEXMO-001"
                         else vol_features(vol_csv) if base_id in VOL_FEATURE_SPECS
                         else vol_features(vol_csv.parent / "daily-index.csv") if base_id in TT_FEATURE_SPECS
                         else None)  # fmt: skip
                strat = LongOptionStrategy(
                    spec, PLUGINS[base_id](), index_key=lib_key, fut_key=FUT_PROXY, vix_key=VIX_LABEL,
                    contracts=od.contracts, costs=costs, plan_id=spec.cost_assumptions.brokerage_plan,
                    risk_budget=budget, regime=regime, expiries=expiries, events=events, gate_regime=False,
                    daily_features=feats,
                )  # fmt: skip
                bars = [*seg_opts, *lib_idx, *vix, *proxy]
            decision = () if relabel or sid in ("S-ORB-001", "S-ORB-002") else (INDEX_LABEL,)
            engine = BacktestEngine(
                clock=MarketClock(cal, sessions), costs=costs,
                bar_model=BarFillModel(interval=ONE_MIN, spread=spread), latency=LatencyModel(),
                config=BacktestConfig(plan_id=spec.cost_assumptions.brokerage_plan, starting_cash=nav,
                                      max_lots=limits.lot_cap(sid.removesuffix(NOCAP))),
            )  # fmt: skip
            meta = {"segment": [str(seg0), str(seg1)], "strategy": sid, "nav": nav_s, "gate_regime": False,
                    "data": data_meta["fingerprints"]}  # fmt: skip
            feed = ReplayFeed.from_bars(bars, interval=ONE_MIN, decision_keys=decision)
            result = engine.run(feed, strat, metadata=meta)
            recs = [t for t in strat.trades if t["day"] >= seg0]
            if sid in ("S-ORB-001", "S-ORB-002"):
                lib_recs = [{**t, "at": t["signal_at"], "legs_detail": [{"key": t["contract"]}]}
                            for t in recs if t.get("outcome") == "CLOSED"]  # fmt: skip
                sigs = [{"at": t["signal_at"], "decided_at": t["signal_at"], "inputs_end": t["signal_at"],
                         "outcome": t.get("outcome", "ENTRY_SENT")} for t in recs]  # fmt: skip
            else:
                lib_recs = [t for t in recs if t.get("outcome") == "CLOSED"]
                sigs = [s for s in strat.signals if s["day"] >= seg0]
            tl = []
            for rec, tr in paired_trades_from_library_run(lib_recs, result):
                tl.append({
                    "day": str(tr.day), "signal_at": rec["at"].isoformat(), "reason": str(rec.get("reason", "")),
                    "entry_ts": tr.entry_ts.isoformat(), "exit_ts": tr.exit_ts.isoformat(),
                    "net_pnl": str(tr.net_pnl), "charges": str(tr.charges), "slippage": str(tr.slippage),
                    "risk_at_stop": str(tr.risk_at_stop), "qty": tr.qty, "exit_reason": tr.exit_reason,
                    "lib_regime_tags": sorted(tr.regimes),
                    # per leg [entry fill, exit fill] (library runs; H36's lot-scaled sensitivity recomputes charges)
                    "fills": [[str(lg.get("entry_fill")), str(lg.get("exit_fill"))]
                              for lg in rec.get("legs_detail", []) if "entry_fill" in lg],
                    "facts": {k: str(v) for k, v in dict(rec.get("facts", {})).items()},
                    "stop_pct": str(rec.get("stop_pct", "")),
                })  # fmt: skip
            outcomes = Counter(str(t.get("outcome")) for t in recs)
            doc = {
                "strategy": sid, "spec_version": spec.version, "nav": nav_s, "segment": [str(seg0), str(seg1)],
                "net_pnl": str(result.net_pnl), "gross_pnl": str(result.gross_pnl),
                "charges": str(result.total_charges), "ledger_hash": result.ledger_hash,
                "flat_at_end": result.flat_at_end, "rejects": result.rejects, "fills": len(result.fills),
                "outcomes": dict(outcomes), "counters": dict(strat.counters), "trades": tl,
                "signal_outcomes": dict(Counter(str(x.get("outcome")) for x in sigs)),
                "signal_stamps": [[str(s.get("decided_at")), str(s.get("inputs_end"))] for s in sigs],
                "risk_at_stop_all": [str(t["risk_at_stop"]) for t in recs if "risk_at_stop" in t],
                "classifier": regime.version, "fills_dated_costs": all(f.breakdown for f in result.fills),
                "fill_model": result.ledger.entries[0]["fill_model"] if result.ledger.entries else None,
                "data": data_meta, "run_s": round(_time.time() - t0, 1), "warmup_days": warmup_days,
            }  # fmt: skip
            f = out / f"{sid}-nav{nav_s}-{seg0}.json"
            f.write_text(json.dumps(_plain(doc), indent=1, default=str))
            summary["runs"].append({"strategy": sid, "nav": nav_s, "trades": len(tl), "net": str(result.net_pnl),
                                    "run_s": doc["run_s"]})  # fmt: skip
    summary["total_s"] = round(_time.time() - t_start, 1)
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="d0", required=True)
    p.add_argument("--to", dest="d1", required=True)
    p.add_argument("--navs", nargs="+", default=["10000", "100000"])
    p.add_argument("--strategies", nargs="+", default=list(STRATEGIES))
    p.add_argument("--segment-days", type=int, default=61)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--lake", type=Path, default=Path("lake"))
    p.add_argument("--out", type=Path, default=Path("lake/runs/regime/strategies"))
    p.add_argument("--only-missing", action="store_true")
    p.add_argument("--classifier-version", default="RC-2026-10-02.1")
    p.add_argument("--index-order", choices=("decision-keys", "relabel"), default="decision-keys")
    p.add_argument("--warmup-days", type=int, default=35, help="calendar days of index history before a segment")
    p.add_argument("--vol-features", type=Path, default=Path("lake/runs/vol/features/vol-labels.csv"))
    p.add_argument("--gex-features", type=Path, default=Path("lake/runs/regime/features/gex.csv"))
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    segs = segments(date.fromisoformat(a.d0), date.fromisoformat(a.d1), a.segment_days)
    if a.only_missing:
        segs = [s for s in segs if not all((a.out / f"{sid}-nav{n}-{s[0]}.json").exists()
                                           for sid in a.strategies for n in a.navs)]  # fmt: skip
    print(json.dumps({"segments": [f"{s}..{e}" for s, e in segs]}), flush=True)
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(run_segment, s, e, a.navs, a.strategies, a.out, a.lake, a.classifier_version,
                         a.index_order, a.warmup_days, a.gex_features, a.vol_features): (s, e)
                for s, e in segs}  # fmt: skip
        for fu in as_completed(futs):
            try:
                print(json.dumps(fu.result(), default=str), flush=True)
            except Exception:  # report and continue with the other segments
                print(json.dumps({"segment": str(futs[fu]), "error": traceback.format_exc()}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

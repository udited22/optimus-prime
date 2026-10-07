#!/usr/bin/env python3
"""Tune the regime classifier's TREND settings on the IN-SAMPLE window only (docs/research/validation.md §13.5).

A small grid, fixed in this file before it was run: confirm_bars {5, 10, 15} x slope_bars {30, 60} x ADX
(enter, exit) {(25, 18), (30, 20)} = 12 configs, the first being v0 (RC-2026-10-02.1). Each config streams the index
from 2022-06-01 to the in-sample end (configs/regime/validation.toml) and is scored on the in-sample, NON-holdout
days only; nothing after the in-sample end is read. Every config is registered as an IN_SAMPLE trial.

Selection rule (also fixed before the run): among configs that pass the in-sample stability criteria, the highest
in-sample TREND_DIRECTION CI lower bound; ties -> fewer median trend flips. The winner becomes a new classifier block
and is then evaluated ONCE out of sample by scripts/regime_research.py.

  scripts/regime_tune.py --jobs 3
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from h01_real_data import _jobs  # noqa: E402
from regime_research import build_sessions  # noqa: E402

from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book  # noqa: E402
from project100c.calendar.events import load_event_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import INDEX_LABEL, VIX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.regime import RegimeClassifier, RegimeConfig, load_regime_config  # noqa: E402
from project100c.regime.validation import evaluate, load_validation_criteria, run_stream  # noqa: E402
from project100c.registry import RunPurpose  # noqa: E402
from project100c.registry.trials import DEFAULT_REGISTRY, code_sha, digest, open_registry, record_trial  # noqa: E402
from project100c.validation.holdout import load_holdout  # noqa: E402

CONFIGS = REPO / "configs"
BASE = "RC-2026-10-02.1"
GRID = [(c, s, a) for c, s, a in product((5, 10, 15), (30, 60), ((25, 18), (30, 20)))]
LAKE = Path("lake")


def variant(base: RegimeConfig, confirm: int, slope: int, adx: tuple[int, int]) -> RegimeConfig:
    t = base.trend.model_copy(update={"confirm_bars": confirm, "slope_bars": slope,
                                      "adx_enter": adx[0], "adx_exit": adx[1]})  # fmt: skip
    return RegimeConfig.model_validate({**base.model_dump(), "version": base.version, "trend": t.model_dump()})


def score(i: int) -> dict[str, Any]:
    confirm, slope, adx = GRID[i]
    base = load_regime_config(CONFIGS / "regime" / "classifier.toml", version=BASE)
    cfg = variant(base, confirm, slope, adx)
    crit = load_validation_criteria(CONFIGS / "regime" / "validation.toml")
    hold = load_holdout(CONFIGS / "validation" / "holdout.toml", version=crit.holdout)
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    events = load_event_book(CONFIGS / "calendar" / "events.yaml").to_calendar(cal)
    d0, d1 = date(2022, 6, 1), crit.in_sample[1]
    _, cands = _jobs(date(2026, 10, 2), d0, d1)
    reader = DhanLakeReader(Lake(LAKE), config=load_dhan_config(CONFIGS / "data" / "dhan.toml"), expiries=expiries,
                            lots=load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml"), accept_warn=True,
                            max_missing_fraction=load_thresholds(CONFIGS / "dq" / "thresholds.toml").max_missing_candle_fraction)  # fmt: skip  # noqa: E501
    cd = reader.load(option_jobs=[], candle_jobs=cands, date_from=d0, date_to=d1)
    sessions, _ = build_sessions(cd.candles[INDEX_LABEL], cal, 300)
    rows = run_stream(RegimeClassifier(cfg, expiries=expiries, events=events), sessions,
                      {b.start: b.close for b in cd.candles[VIX_LABEL]})  # fmt: skip
    rep = evaluate(rows, crit, classifier=f"{BASE}+tune{i}",
                   exclude=lambda d: hold.contains(d) or d > crit.in_sample[1])  # fmt: skip
    st = rep["stability"]["IS"]
    td = rep["tests"]["TREND_DIRECTION"]["IS"]
    stable = (st["trend_run_median"] >= crit.min_median_trend_run_bars
              and st["trend_flips_median"] <= crit.max_median_trend_flips
              and st["vol_run_median"] >= crit.min_median_vol_run_bars
              and st["vol_flips_median"] <= crit.max_median_vol_flips)  # fmt: skip
    return {"i": i, "confirm_bars": confirm, "slope_bars": slope, "adx_enter": adx[0], "adx_exit": adx[1],
            "is_stable": stable, "trend_flips_median": st["trend_flips_median"],
            "trend_run_median": st["trend_run_median"], "td_is": td["estimate"], "td_is_lo": td["ci_lower"],
            "tm_is": rep["tests"]["TREND_MAGNITUDE"]["IS"]["estimate"],
            "kappa_is": rep["confusion"]["trend_IS"].get("kappa"),
            "data": [r.fingerprint() for r in cd.reports], "cfg": cfg.model_dump(mode="json")}  # fmt: skip


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jobs", type=int, default=3)
    a = p.parse_args()
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        res = sorted(ex.map(score, range(len(GRID))), key=lambda r: r["i"])
    with open_registry(DEFAULT_REGISTRY) as reg:
        for r in res:
            record_trial(
                reg, strategy_id="REGIME-CLASSIFIER", strategy_version=f"{BASE}+tune{r['i']}",
                spec_hash=digest(r["cfg"]), code=code_sha(REPO), data_hash=digest(r["data"]), cost_version="n/a",
                purpose=RunPurpose.IN_SAMPLE, params={k: r[k] for k in ("confirm_bars", "slope_bars", "adx_enter",
                                                                       "adx_exit")},
                ledger_hash=digest(r), n_trades=0,
                metrics={k: r[k] for k in ("is_stable", "td_is", "td_is_lo", "trend_flips_median", "kappa_is")},
                registered_by="scripts/regime_tune.py",
            )  # fmt: skip
    ok = [r for r in res if r["is_stable"]]
    best = max(ok, key=lambda r: (r["td_is_lo"], -r["trend_flips_median"])) if ok else None
    out = LAKE / "runs" / "regime" / "classifier" / f"tune-{BASE}.json"
    out.write_text(json.dumps({"grid": [{k: v for k, v in r.items() if k not in ("cfg", "data")} for r in res],
                               "selected": None if best is None else best["i"]}, indent=1))  # fmt: skip
    for r in res:
        print(json.dumps({k: v for k, v in r.items() if k not in ("cfg", "data")}))
    keys = ("i", "confirm_bars", "slope_bars", "adx_enter", "adx_exit")
    print(json.dumps({"selected": None if best is None else {k: best[k] for k in keys}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

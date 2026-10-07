#!/usr/bin/env python3
"""Labels of the alternative regime models H25 F-TERM-001 and H26 F-HMMREG-001 (configs/regime/alt_models.toml).

Inputs (all point in time): the classifier-stream CSV of RC-2026-10-02.1 (1-min index closes;
scripts/regime_research.py), the F-GEX-001 daily file (ATM IV snapshots, DTE, G rank; scripts/feature_gex.py) and
India VIX 1-min closes from the lake.
Holdout days (configs/validation/holdout.toml) are labelled but never used to fit anything.

Output: lake/runs/regime/labels/alt-{version}.csv (day, label, from_time; a trade signalled before from_time uses the
previous session's label) and lake/runs/regime/labels/alt-daily.csv (the daily inputs plus the session's 1-min
open-to-close realised volatility, used for the information test). Research only.

  scripts/regime_alt_models.py
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
import tomllib
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from h01_real_data import _jobs  # noqa: E402

from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import VIX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.regime.alt_models import hmm_walk_forward, tercile_labels, term_slope  # noqa: E402
from project100c.sessions import IST  # noqa: E402
from project100c.validation.holdout import load_holdout  # noqa: E402

CONFIGS = REPO / "configs"


def daily_from_stream(path: Path) -> dict[date, dict[str, float]]:
    closes: dict[date, list[float]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            closes.setdefault(date.fromisoformat(r["day"]), []).append(float(r["close"]))
    out: dict[date, dict[str, float]] = {}
    prev: float | None = None
    for d in sorted(closes):
        c = closes[d]
        if len(c) < 300:
            prev = c[-1]
            continue
        rets = [math.log(b / a) for a, b in pairwise(c)]
        row = {"rv_oc": math.sqrt(sum(x * x for x in rets) * 252), "close": c[-1]}
        if prev:
            row["abs_gap_pct"] = abs(c[0] / prev - 1) * 100
        out[d] = row
        prev = c[-1]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lake", type=Path, default=Path("lake"))
    a = ap.parse_args()
    cfgs = {m["id"]: m for m in tomllib.loads((CONFIGS / "regime" / "alt_models.toml").read_text())["alt_model"]}
    hd = load_holdout(CONFIGS / "validation" / "holdout.toml")
    lab_dir = a.lake / "runs" / "regime" / "labels"
    daily = daily_from_stream(lab_dir / "RC-2026-10-02.1.csv.gz")
    gex = {date.fromisoformat(r["day"]): r for r in csv.DictReader((a.lake / "runs/regime/features/gex.csv").open())}

    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    d0, d1 = min(daily) - timedelta(days=10), max(daily)
    dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    reader = DhanLakeReader(Lake(a.lake), config=load_dhan_config(CONFIGS / "data" / "dhan.toml"), expiries=expiries,
                            lots=load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml"), accept_warn=True,
                            max_missing_fraction=dq.max_missing_candle_fraction)  # fmt: skip
    _, cands = _jobs(date(2026, 10, 2), d0, d1)
    cd = reader.load(option_jobs=[], candle_jobs=[c for c in cands if c.label == VIX_LABEL],
                     date_from=d0, date_to=d1)  # fmt: skip
    vix_close: dict[date, float] = {}
    for b in sorted(cd.candles[VIX_LABEL], key=lambda b: b.start):
        vix_close[b.start.astimezone(IST).date()] = float(b.close)
    vdays = sorted(vix_close)
    for i, d in enumerate(vdays[1:], 1):
        if d in daily:
            daily[d]["vix_change"] = vix_close[d] - vix_close[vdays[i - 1]]

    def slope(g: dict[str, str], snap: str, rest_h: float) -> float | None:
        try:
            iv1, iv2, dte = float(g[f"iv1_{snap}"]), float(g[f"iv2_{snap}"]), float(g["code1_dte"])
        except (KeyError, ValueError):
            return None
        return term_slope(iv1, iv2, dte + rest_h / 24)

    for d, row in daily.items():
        g = gex.get(d)
        if g is None:
            continue
        s0930, s1300 = slope(g, "0930", 6.0), slope(g, "1300", 2.5)
        if s0930 is not None:
            row["term_slope_0930"] = s0930
        if s1300 is not None:
            row["term_slope_1300"] = s1300
        if g.get("gex_rank"):
            row["gex_rank"] = float(g["gex_rank"])
    trainable = {d for d in daily if not hd.contains(d)}
    summary: dict[str, Any] = {}

    ts = cfgs["F-TERM-001"]
    vals = {d: r["term_slope_0930"] for d, r in daily.items() if "term_slope_0930" in r}
    tl = tercile_labels(vals, trainable, lookback=int(ts["lookback"]), min_history=int(ts["min_history"]),
                        labels=ts["labels"])  # fmt: skip
    with (lab_dir / f"alt-{ts['version']}.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "label", "from_time"])
        for d in sorted(tl):
            w.writerow([d.isoformat(), tl[d], "09:30"])
    summary[ts["version"]] = {"days": len(tl), "counts": {k: list(tl.values()).count(k) for k in ts["labels"]}}

    hm = cfgs["F-HMMREG-001"]
    feats = list(hm["features"])
    obs = {}
    for d, r in daily.items():
        if all(k in r for k in ("rv_oc", *feats[1:])):
            obs[d] = [math.log(max(r["rv_oc"], 1e-4)), *[r[k] for k in feats[1:]]]
    hl, fits = hmm_walk_forward(obs, trainable & set(obs), k=int(hm["states"]), labels=hm["labels"],
                                min_train=int(hm["min_train_sessions"]), max_iter=int(hm["max_iter"]),
                                tol=float(hm["tol"]), var_floor=float(hm["var_floor"]))  # fmt: skip
    with (lab_dir / f"alt-{hm['version']}.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "label", "from_time"])
        for d in sorted(hl):
            w.writerow([d.isoformat(), hl[d], "09:15"])
    summary[hm["version"]] = {"days": len(hl), "obs_days": len(obs), "first_fit": fits[0] if fits else None,
                              "fits": len(fits),
                              "counts": {k: list(hl.values()).count(k) for k in hm["labels"]}}  # fmt: skip
    cols = ["rv_oc", "abs_gap_pct", "vix_change", "term_slope_0930", "term_slope_1300", "gex_rank"]
    with (lab_dir / "alt-daily.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["day", *cols])
        for d in sorted(daily):
            w.writerow([d.isoformat(), *[daily[d].get(c, "") for c in cols]])
    (lab_dir / "alt-models-fits.json").write_text(json.dumps({"fits": fits, "summary": summary}, indent=1))
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

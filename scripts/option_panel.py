#!/usr/bin/env python3
"""Compact per-day option panels for the OD-019 offline simulator (H42-H47): nearest-weekly contracts at the 21
rolling strikes (ATM-10..ATM+10) and the NIFTY index, one-minute bars, from the clean lake through DhanLakeReader
(the same DQ rules as every strategy run). Research only; output gitignored.

Per month: lake/runs/sell/panel/<YYYY-MM-DD>.pkl.gz = {day: {"idx": float32[375, 4] (o, h, l, c; NaN = missing),
"opt": {(expiry_iso, strike, right): float32[375, 6] (o, h, l, c, volume, vendor iv)},
"lots": {same key: lot size}}}.

  scripts/option_panel.py --from 2022-08-29 --to 2026-10-01 [--jobs 2] [--only-missing]
"""

from __future__ import annotations

import argparse
import gzip
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from feature_gex import month_chunks  # noqa: E402
from h01_real_data import _jobs  # noqa: E402

from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import INDEX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.sessions import IST  # noqa: E402

CONFIGS = REPO / "configs"
OUT = Path("lake/runs/sell/panel")


def _m(ts: datetime) -> int:
    t = ts.astimezone(IST).time()
    return (t.hour * 60 + t.minute) - (9 * 60 + 15)


def chunk(seg0: date, seg1: date, lake: Path) -> dict[str, Any]:
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    lots = load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")
    dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    opts, cands = _jobs(date(2026, 10, 2), seg0, seg1)
    opts = [o.model_copy(update={"strike_offsets": tuple(range(-10, 11))}) for o in opts if o.expiry_codes == (1,)]
    reader = DhanLakeReader(Lake(lake), config=cfg, expiries=expiries, lots=lots, accept_warn=True,
                            max_missing_fraction=dq.max_missing_candle_fraction)  # fmt: skip
    try:
        od = reader.load(option_jobs=opts, candle_jobs=[], date_from=seg0, date_to=seg1)
        cd = reader.load(option_jobs=[], candle_jobs=[c for c in cands if c.label == INDEX_LABEL], date_from=seg0,
                         date_to=seg1)  # fmt: skip
    except Exception as e:  # a month with no clean data
        return {"chunk": f"{seg0}..{seg1}", "error": str(e)[:300]}
    days: dict[date, dict[str, Any]] = {}
    for b in cd.candles.get(INDEX_LABEL, []):
        m = _m(b.start)
        if not 0 <= m < 375:
            continue
        d = b.start.astimezone(IST).date()
        e = days.setdefault(d, {"idx": np.full((375, 4), np.nan, np.float32), "opt": {}})
        e["idx"][m] = (float(b.open), float(b.high), float(b.low), float(b.close))
    for b in od.option_bars:
        m = _m(b.start)
        d = b.start.astimezone(IST).date()
        if not 0 <= m < 375 or d not in days:
            continue
        c = od.contracts[b.instrument_key]
        k = (c.expiry.isoformat(), float(c.strike), c.right.value)
        arr = days[d]["opt"].get(k)
        if arr is None:
            arr = days[d]["opt"][k] = np.full((375, 6), np.nan, np.float32)
            days[d].setdefault("lots", {})[k] = int(c.lot_size)
        iv = od.iv.get((b.instrument_key, b.start))
        arr[m] = (float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume),
                  float(iv) if iv is not None else np.nan)  # fmt: skip
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / f"{seg0.isoformat()}.pkl.gz", "wb") as fh:
        pickle.dump(days, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return {"chunk": f"{seg0}..{seg1}", "days": len(days), "contracts": len(od.contracts)}


def load_panel(d0: date, d1: date) -> dict[date, dict[str, Any]]:
    out: dict[date, dict[str, Any]] = {}
    for f in sorted(OUT.glob("*.pkl.gz")):
        with gzip.open(f, "rb") as fh:
            part = pickle.load(fh)  # our own research output
        out.update({d: v for d, v in part.items() if d0 <= d <= d1})
    return dict(sorted(out.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="d0", required=True)
    ap.add_argument("--to", dest="d1", required=True)
    ap.add_argument("--lake", type=Path, default=Path("lake"))
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--only-missing", action="store_true")
    a = ap.parse_args()
    chunks = month_chunks(date.fromisoformat(a.d0), date.fromisoformat(a.d1))
    if a.only_missing:
        chunks = [c for c in chunks if not (OUT / f"{c[0].isoformat()}.pkl.gz").exists()]
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        for r in ex.map(chunk, [c[0] for c in chunks], [c[1] for c in chunks], [a.lake] * len(chunks)):
            print(r, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

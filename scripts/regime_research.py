#!/usr/bin/env python3
"""Regime classifier on REAL Dhan index + VIX bars: the standalone label stream and its validation
(docs/research/validation.md §13.5).

1. Streams a classifier version over every NIFTY 1-minute session in the lake (index and VIX parts that passed DQ;
   WARN accepted, BLOCKED excluded), carrying the classifier's cross-session history from one session to the next.
   Sessions are the NSE F&O trading days of the calendar, bars 09:15-15:30 IST; a day with fewer than
   ``--min-bars`` bars is skipped (a partial vendor day). The previous close is used only when the previous trading
   day is in the data.
2. Writes the label stream (gitignored lake: ``runs/regime/labels/<version>.csv.gz``). The strategy slicing and the
   regime -> strategy map (scripts/regime_portfolio.py) read regime labels from this file by timestamp.
3. Evaluates it against the pre-registered criteria (configs/regime/validation.toml), with every HOLDOUT day
   (configs/validation/holdout.toml) dropped before scoring, and writes the JSON report
   (``runs/regime/classifier/<version>__<criteria>.json``). No prices leave the lake; the report holds statistics.

  scripts/regime_research.py --classifier-version RC-2026-10-02.1
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time as _time
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from h01_real_data import _jobs  # noqa: E402

from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book  # noqa: E402
from project100c.calendar.events import load_event_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import INDEX_LABEL, VIX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.market_types import Bar  # noqa: E402
from project100c.regime import RegimeClassifier, load_regime_config  # noqa: E402
from project100c.regime.validation import (  # noqa: E402
    CSV_HEADER,
    LabelRow,
    evaluate,
    load_validation_criteria,
    run_stream,
)
from project100c.registry import RunPurpose  # noqa: E402
from project100c.registry.trials import DEFAULT_REGISTRY, code_sha, digest, open_registry, record_trial  # noqa: E402
from project100c.sessions import IST  # noqa: E402
from project100c.validation.holdout import load_holdout  # noqa: E402

CONFIGS = REPO / "configs"
OPEN, CLOSE = time(9, 15), time(15, 30)


def load_labels(path: Path) -> list[LabelRow]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        next(f)
        return [LabelRow.from_csv(line) for line in f]


def build_sessions(
    idx: tuple[Bar, ...], cal: TradingCalendar, min_bars: int
) -> tuple[list[tuple[date, Decimal | None, list[Bar]]], dict[str, int]]:
    days: dict[date, list[Bar]] = {}
    for b in idx:
        st = b.start.astimezone(IST)
        if OPEN <= st.time() < CLOSE:
            days.setdefault(st.date(), []).append(b)
    stats = {"days_in_data": len(days), "skipped_not_trading_day": 0, "skipped_partial": 0, "no_prev_close": 0}
    out: list[tuple[date, Decimal | None, list[Bar]]] = []
    for d in sorted(days):
        if not cal.covers(d) or not cal.is_trading_day(d):
            stats["skipped_not_trading_day"] += 1
            continue
        bars = sorted(days[d], key=lambda b: b.start)
        if len(bars) < min_bars:
            stats["skipped_partial"] += 1
            continue
        prev = cal.previous_trading_day(d)
        pc = days[prev][-1].close if prev in days and len(days[prev]) >= min_bars else None
        if pc is None:
            stats["no_prev_close"] += 1
        out.append((d, pc, bars))
    stats["sessions"] = len(out)
    return out, stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--classifier-version", required=True)
    p.add_argument(
        "--classifier-file",
        type=Path,
        default=CONFIGS / "regime" / "classifier.toml",
        help="classifier.toml (adopted) or candidates.toml (under evaluation)",
    )
    p.add_argument("--criteria-version", default=None)
    p.add_argument("--from", dest="d0", default="2021-10-01")
    p.add_argument("--to", dest="d1", default="2026-10-01")
    p.add_argument("--min-bars", type=int, default=300)
    p.add_argument("--lake", type=Path, default=Path("lake"))
    p.add_argument("--reuse-labels", action="store_true", help="evaluate an existing label file")
    a = p.parse_args()
    t0 = _time.time()
    clf_cfg = load_regime_config(a.classifier_file, version=a.classifier_version)
    crit = load_validation_criteria(CONFIGS / "regime" / "validation.toml", version=a.criteria_version)
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    events = load_event_book(CONFIGS / "calendar" / "events.yaml").to_calendar(cal)
    out_l = a.lake / "runs" / "regime" / "labels"
    out_r = a.lake / "runs" / "regime" / "classifier"
    out_l.mkdir(parents=True, exist_ok=True)
    out_r.mkdir(parents=True, exist_ok=True)
    lab_path = out_l / f"{clf_cfg.version}.csv.gz"
    meta: dict[str, object] = {"classifier": clf_cfg.version, "classifier_status": clf_cfg.status}
    if a.reuse_labels:
        rows = load_labels(lab_path)
    else:
        d0, d1 = date.fromisoformat(a.d0), date.fromisoformat(a.d1)
        cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
        dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
        lots = load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")
        _, cands = _jobs(date(2026, 10, 2), d0, d1)
        reader = DhanLakeReader(Lake(a.lake), config=cfg, expiries=expiries, lots=lots, accept_warn=True,
                                max_missing_fraction=dq.max_missing_candle_fraction)  # fmt: skip
        cd = reader.load(option_jobs=[], candle_jobs=cands, date_from=d0, date_to=d1)
        idx, vix_bars = cd.candles[INDEX_LABEL], cd.candles[VIX_LABEL]
        vix = {b.start: b.close for b in vix_bars}
        sessions, sstats = build_sessions(idx, cal, a.min_bars)
        meta.update(sessions=sstats, data=[r.fingerprint() for r in cd.reports], load_s=round(_time.time() - t0, 1))
        rows = run_stream(RegimeClassifier(clf_cfg, expiries=expiries, events=events), sessions, vix)
        with gzip.open(lab_path, "wt", encoding="utf-8") as f:
            f.write(CSV_HEADER + "\n")
            for r in rows:
                f.write(r.as_csv() + "\n")
        meta["stream_s"] = round(_time.time() - t0, 1)
    hold = load_holdout(CONFIGS / "validation" / "holdout.toml", version=crit.holdout)
    rep = evaluate(rows, crit, classifier=clf_cfg.version, exclude=hold.contains)
    rep["meta"] = meta
    rep["generated_at"] = datetime.now(IST).isoformat(timespec="seconds")
    rpath = out_r / f"{clf_cfg.version}__{crit.version}.json"
    rpath.write_text(json.dumps(rep, indent=1, default=str))
    # every evaluation on real data is a trial (docs/research/validation.md)
    with open_registry(DEFAULT_REGISTRY) as reg:
        rep["trial_id"] = record_trial(
            reg, strategy_id="REGIME-CLASSIFIER", strategy_version=clf_cfg.version,
            spec_hash=digest(clf_cfg.model_dump(mode="json")), code=code_sha(REPO),
            data_hash=digest(meta.get("data") or lab_path.name), cost_version="n/a",
            purpose=RunPurpose.OUT_OF_SAMPLE, params={"criteria": crit.version, "holdout": crit.holdout},
            ledger_hash=digest([r.as_csv() for r in rows[:: max(1, len(rows) // 5000)]]), n_trades=0,
            metrics={"verdict": rep["verdict"], "failures": len(rep["failures"])},
            registered_by="scripts/regime_research.py",
        )  # fmt: skip
    rpath.write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({"report": str(rpath), "labels": str(lab_path), "verdict": rep["verdict"],
                      "failures": rep["failures"], "stability": rep["stability"],
                      "tests": {k: {"IS": v["IS"]["estimate"], "OOS": v["OOS"]["estimate"],
                                    "OOS_lo": v["OOS"]["ci_lower"], "years+": v["years_positive"]}
                                for k, v in rep["tests"].items()},
                      "elapsed_s": round(_time.time() - t0, 1)}, indent=1, default=str))  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

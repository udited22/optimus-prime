#!/usr/bin/env python3
"""H23 S-ORBML-001: the pre-registered meta-label veto on S-ORB-002 (H01b) trades, walk-forward (research).

The primary is S-ORB-002 unchanged: its trades come from the library run (scripts/regime_strategy_runs.py) at each
NAV. For each trade the ten features of the spec are built at the signal minute (point in time):

  1 OR width (09:15-09:30 high-low) / 20-day ATR (true range of the 20 previous sessions)
  2 |gap| % (today's 09:15 open vs the previous session's last close)
  3 India VIX (last 1-min close ending at or before the signal)
  4 VIX 30-min change
  5 IV term slope: code-2 / code-1 ATM IV - 1 at the latest scripts/feature_gex.py snapshot ending at or before the
    signal
  6 F-GEX-001 G percentile: the PREVIOUS session's gex_rank (today's is taken at 10:15, after many signals)
  7 minutes since 09:15
  8 DTE of the nearest weekly expiry
  9 previous-day return (close to close)
 10 K-11 volatility tag at the signal (classifier stream RC-2026-10-02.1; COMPRESSION -1, NORMAL 0, EXPANSION +1)

Label (the spec's triple barrier, read off the realised trade path): 1 when the trade exits at its 1.5 R TARGET within
60 minutes of entry, else 0. Model: L2 logistic regression, C = 1, retrained monthly on the labelled signals known
before the month and NOT on holdout days (configs/validation/holdout.toml); at least 150 of them; take a trade when
p >= 0.55. Purged 5-fold CV with a 1-day embargo is reported as a diagnostic only. No parameter is tuned.

Output: lake/runs/regime/strategies/S-ORBML-001-nav{nav}-overlay.json in the library-run format (kept trades only),
so scripts/regime_portfolio.py treats it like any strategy. Each NAV's overlay is registered as a trial.

  scripts/orbml_overlay.py
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from h01_real_data import _jobs  # noqa: E402

from project100c.backtest.lake_source import DhanLakeReader  # noqa: E402
from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book  # noqa: E402
from project100c.data.dhan import load_dhan_config  # noqa: E402
from project100c.data.dhan.backfill import INDEX_LABEL, VIX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.portfolio.regime_book import LabelIndex  # noqa: E402
from project100c.registry import RunPurpose  # noqa: E402
from project100c.registry.trials import DEFAULT_REGISTRY, code_sha, digest, open_registry, record_trial  # noqa: E402
from project100c.sessions import IST  # noqa: E402
from project100c.spec.io import load_spec_file  # noqa: E402
from project100c.studies.metalabel import Sample, cv_diagnostic, walk_forward  # noqa: E402
from project100c.validation.holdout import load_holdout  # noqa: E402

CONFIGS = REPO / "configs"
SNAPS = {"0930": time(9, 30), "1015": time(10, 15), "1100": time(11, 0), "1200": time(12, 0), "1300": time(13, 0)}
VOLTAG = {"VOLATILITY_COMPRESSION": -1.0, "VOLATILITY_NORMAL": 0.0, "VOLATILITY_EXPANSION": 1.0}
FEATURES = ["or_width_atr", "abs_gap_pct", "vix", "vix_chg_30m", "iv_term_slope", "gex_rank_prev",
            "minutes_since_open", "dte", "prev_day_ret", "vol_tag"]  # fmt: skip


def _f(x: str | None) -> float | None:
    return float(x) if x not in (None, "") else None


def day_stats(idx: list[Any]) -> dict[date, dict[str, float]]:
    by: dict[date, list[Any]] = {}
    for b in idx:
        st = b.start.astimezone(IST)
        if time(9, 15) <= st.time() < time(15, 30):
            by.setdefault(st.date(), []).append(b)
    out: dict[date, dict[str, float]] = {}
    prev_close: float | None = None
    trs: list[float] = []
    for d in sorted(by):
        bs = sorted(by[d], key=lambda b: b.start)
        hi, lo = max(float(b.high) for b in bs), min(float(b.low) for b in bs)
        orb = [b for b in bs if b.start.astimezone(IST).time() < time(9, 30)]
        row: dict[str, float] = {"open": float(bs[0].open), "close": float(bs[-1].close)}
        if orb:
            row["or_width"] = max(float(b.high) for b in orb) - min(float(b.low) for b in orb)
        if len(trs) >= 20:
            row["atr20"] = sum(trs[-20:]) / 20
        if prev_close is not None:
            row["prev_close"] = prev_close
            row["gap_pct"] = (row["open"] / prev_close - 1) * 100
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        else:
            tr = hi - lo
        out[d] = row
        trs.append(tr)
        prev_close = row["close"]
    days = sorted(out)
    for i, d in enumerate(days):
        if i >= 1 and "prev_close" in out[days[i - 1]]:
            out[d]["prev_ret"] = out[days[i - 1]]["close"] / out[days[i - 1]]["prev_close"] - 1
    return out


Feat = tuple[tuple[float, ...] | None, datetime, dict[str, Any]]


def features(t: dict[str, Any], ds: dict[date, dict[str, float]], gex: dict[date, dict[str, str]], gex_days: list[date],
             vix_t: list[datetime], vix_v: list[float], labels: LabelIndex) -> Feat:  # fmt: skip
    d = date.fromisoformat(t["day"])
    sig = datetime.fromisoformat(t["signal_at"])
    row = ds.get(d, {})
    vals: dict[str, float | None] = {k: None for k in FEATURES}
    latest = datetime(d.year, d.month, d.day, 9, 30, tzinfo=IST)
    if "or_width" in row and row.get("atr20"):
        vals["or_width_atr"] = row["or_width"] / row["atr20"]
    if "gap_pct" in row:
        vals["abs_gap_pct"] = abs(row["gap_pct"])
    vals["prev_day_ret"] = row.get("prev_ret")
    i = bisect_right(vix_t, sig) - 1  # vix_t are bar ENDS
    j = bisect_right(vix_t, sig - timedelta(minutes=30)) - 1
    if i >= 0 and vix_t[i].date() == d:
        vals["vix"] = vix_v[i]
        latest = max(latest, vix_t[i])
        if j >= 0 and vix_t[j].date() == d:
            vals["vix_chg_30m"] = vix_v[i] - vix_v[j]
    g = gex.get(d)
    if g is not None:
        vals["dte"] = _f(g.get("code1_dte"))
        for tag, tm in reversed(list(SNAPS.items())):
            end = datetime.combine(d, tm, IST)
            if end <= sig:
                a, b = _f(g.get(f"iv1_{tag}")), _f(g.get(f"iv2_{tag}"))
                if a and b:
                    vals["iv_term_slope"] = b / a - 1
                    latest = max(latest, end)
                break
    k = bisect_right(gex_days, d - timedelta(days=1)) - 1
    if k >= 0:
        vals["gex_rank_prev"] = _f(gex[gex_days[k]].get("gex_rank"))
    vals["minutes_since_open"] = (sig - datetime.combine(d, time(9, 15), IST)).total_seconds() / 60
    vt = [VOLTAG[x] for x in (labels.at(sig) or ()) if x in VOLTAG]
    vals["vol_tag"] = vt[0] if vt else None
    x = None if any(v is None for v in vals.values()) else tuple(float(vals[k2] or 0.0) for k2 in FEATURES)
    return x, latest, vals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lake", type=Path, default=Path("lake"))
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--no-register", action="store_true")
    a = ap.parse_args()
    runs = a.lake / "runs" / "regime" / "strategies"
    spec_path = REPO / "specs" / "S-ORBML-001.yaml"
    if not spec_path.exists():
        spec_path = REPO / "specs" / "drafts" / "S-ORBML-001.yaml"
    spec = load_spec_file(spec_path)
    p = {k: v.value for k, v in spec.signal.params.items()}
    hd = load_holdout(CONFIGS / "validation" / "holdout.toml")
    gex_rows = list(csv.DictReader((a.lake / "runs" / "regime" / "features" / "gex.csv").open()))
    gex = {date.fromisoformat(r["day"]): r for r in gex_rows}
    gex_days = sorted(gex)
    with gzip.open(a.lake / "runs" / "regime" / "labels" / "RC-2026-10-02.1.csv.gz", "rt", encoding="utf-8") as fh:
        labels = LabelIndex(csv.DictReader(fh))

    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    d0, d1 = date(2022, 6, 1), date(2026, 10, 1)
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    lots = load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")
    _, cands = _jobs(date(2026, 10, 2), d0, d1)
    reader = DhanLakeReader(Lake(a.lake), config=cfg, expiries=expiries, lots=lots, accept_warn=True,
                            max_missing_fraction=dq.max_missing_candle_fraction)  # fmt: skip
    cd = reader.load(option_jobs=[], candle_jobs=cands, date_from=d0, date_to=d1)
    ds = day_stats(list(cd.candles[INDEX_LABEL]))
    vix = sorted(cd.candles[VIX_LABEL], key=lambda b: b.start)
    vix_t = [(b.start + timedelta(minutes=1)).astimezone(IST) for b in vix]
    vix_v = [float(b.close) for b in vix]
    data_fp = [r.fingerprint() for r in cd.reports]
    del cd

    reg = None if a.no_register else open_registry(a.registry)
    summary: dict[str, Any] = {}
    for nav in ("10000", "100000"):
        docs = [json.loads(f.read_text()) for f in sorted(runs.glob(f"S-ORB-002-nav{nav}-*.json"))]
        if not docs:
            continue
        trades = sorted((t for d in docs for t in d["trades"]), key=lambda t: t["signal_at"])
        samples, feats = [], []
        for t in trades:
            x, latest, vals = features(t, ds, gex, gex_days, vix_t, vix_v, labels)
            ent, ex = datetime.fromisoformat(t["entry_ts"]), datetime.fromisoformat(t["exit_ts"])
            y = 1 if t["exit_reason"] == "TARGET" and ex - ent <= timedelta(minutes=60) else 0
            d = date.fromisoformat(t["day"])
            samples.append(Sample(d, datetime.fromisoformat(t["signal_at"]), ex, x, y, trainable=not hd.contains(d)))
            feats.append((latest, vals))
        dec = walk_forward(samples, retrain_months=int(p["retrain_months"]), min_train=int(p["min_train_signals"]),
                           p_min=float(p["p_min"]), c=1.0)  # fmt: skip
        by_sig = {d.sample.signal_at: d for d in dec}
        kept, stamps = [], []
        for t, (latest, _vals) in zip(trades, feats, strict=True):
            dd = by_sig[datetime.fromisoformat(t["signal_at"])]
            if dd.take:
                kept.append({**t, "meta_p": round(dd.p or 0.0, 4)})
                stamps.append([t["signal_at"], latest.isoformat()])
        missing = {k: sum(1 for _, v in feats if v.get(k) is None) for k in FEATURES}
        research = [s for s in samples if s.trainable]
        cv = (
            cv_diagnostic(research, k=int(p["cv_folds"]), embargo_days=int(p["embargo_days"]), c=1.0)
            if research
            else {}
        )
        counts: dict[str, int] = {}
        for d_ in dec:
            counts[d_.reason] = counts.get(d_.reason, 0) + 1
        doc = {"strategy": "S-ORBML-001", "spec_version": spec.version, "nav": nav, "segment": "overlay",
               "primary": "S-ORB-002", "trades": kept, "signal_stamps": stamps,
               "fills_dated_costs": all(d["fills_dated_costs"] for d in docs), "fill_model": docs[0]["fill_model"],
               "classifier": docs[0]["classifier"], "ledger_hash": digest(kept), "params": p,
               "features": FEATURES, "decisions": counts, "cv_diagnostic": cv,
               "n_primary_trades": len(trades), "missing_by_feature": missing, "n_labelled_research": len(research),
               "label_rate_research": cv.get("base_rate"), "data": data_fp,
               "gex_rows": len(gex_rows)}  # fmt: skip
        out = runs / f"S-ORBML-001-nav{nav}-overlay.json"
        out.write_text(json.dumps(doc, indent=1, default=str))
        summary[nav] = {"missing": missing, "primary": len(trades), "kept": len(kept), "decisions": counts, "cv": cv}
        if reg is not None:
            record_trial(
                reg, strategy_id="S-ORBML-001", strategy_version=spec.version,
                spec_hash=digest(spec.model_dump(mode="json")),
                code=code_sha(REPO), data_hash=digest([d["ledger_hash"] for d in docs] + [len(gex_rows)]),
                cost_version="dated", purpose=RunPurpose.WALK_FORWARD, params={**p, "nav": nav},
                ledger_hash=doc["ledger_hash"], n_trades=len(kept), metrics={"decisions": counts, "cv": cv},
                registered_by="scripts/orbml_overlay.py",
            )  # fmt: skip
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

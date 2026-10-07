"""F-GEX-001 / H22 feature on the real lake: the daily partial-chain gamma concentration G at 10:15 and its
point-in-time rank, plus ATM IV snapshots of the nearest and next weekly expiries (H23 term-slope feature).

For each trading day, from the 1-minute option bars that START at 10:14 (closed at 10:15):
  G = sum over code-1 ATM-10..ATM+10 and code-2 ATM-3..ATM+3 strikes of BS gamma x (CE + PE open interest) x spot^2
      x 1%. Dhan reports OI as QUANTITY (contracts x lot), so no lot factor (the spec's 'x lot' is already inside).
      IV: the vendor IV of the OTM side when > 0, else an own BS inversion of the OTM close. Every option is
      counted dealer-short (the pre-registered India-short sign, H24). G is dominated by days to expiry.
  A day with more than 3 code-1 strikes lacking OI or a computable IV gets no G (H22 STAND_DOWN_DAY rule).
gex_rank(d) = 100 x share of the previous ``--lookback`` days with a G whose G <= G(d); no rank with fewer days.
ATM IV snapshots (code 1 and code 2) at 09:30, 10:15, 11:00, 12:00 and 13:00 (bar ends).

Output: lake/runs/regime/features/gex.csv (gitignored). Research only.

    .venv/bin/python scripts/feature_gex.py --from 2022-06-01 --to 2026-10-01 --jobs 4
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ProcessPoolExecutor
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
from project100c.data.dhan.backfill import INDEX_LABEL  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.instruments.lot_history import load_lot_history  # noqa: E402
from project100c.sessions import IST  # noqa: E402
from project100c.strategies.library.optmath import bs_gamma, implied_vol, years_to_expiry  # noqa: E402

CONFIGS = REPO / "configs"
G_AT = time(10, 14)  # bar START (closes 10:15)
SNAPS = {"0930": time(9, 29), "1015": time(10, 14), "1100": time(10, 59), "1200": time(11, 59), "1300": time(12, 59)}


LOT_REVISIONS = (date(2024, 4, 26), date(2024, 11, 21), date(2025, 10, 29))  # as scripts/regime_strategy_runs.py


def month_chunks(d0: date, d1: date) -> list[tuple[date, date]]:
    """Calendar months, also cut at each lot revision (a contract may not change lot size inside one load)."""
    out, s = [], d0
    while s <= d1:
        nxt = date(s.year + (s.month == 12), s.month % 12 + 1, 1)
        cut = min([r for r in LOT_REVISIONS if s < r < nxt] or [nxt])
        out.append((s, min(d1, cut - timedelta(days=1))))
        s = cut
    return out


def chunk(seg0: date, seg1: date, lake: Path) -> list[dict[str, Any]]:
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    expiries = ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))
    lots = load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")
    dq = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    opts, cands = _jobs(date(2026, 10, 2), seg0, seg1)
    wide, near = tuple(range(-10, 11)), (-3, -2, -1, 0, 1, 2, 3)
    opts = [o.model_copy(update={"strike_offsets": wide if o.expiry_codes == (1,) else near}) for o in opts]
    reader = DhanLakeReader(Lake(lake), config=cfg, expiries=expiries, lots=lots, accept_warn=True,
                            max_missing_fraction=dq.max_missing_candle_fraction)  # fmt: skip
    try:
        od = reader.load(option_jobs=opts, candle_jobs=[], date_from=seg0, date_to=seg1)
        cd = reader.load(option_jobs=[], candle_jobs=[c for c in cands if c.label == INDEX_LABEL], date_from=seg0,
                         date_to=seg1)  # fmt: skip
    except Exception as e:  # a month with no clean data: no features
        return [{"chunk": f"{seg0}..{seg1}", "error": str(e)[:300]}]
    spot = {b.start: b.close for b in cd.candles.get(INDEX_LABEL, [])}
    want = {t for t in SNAPS.values()}
    by_min: dict[datetime, list[Any]] = {}
    for b in od.option_bars:
        if b.start.astimezone(IST).time() in want:
            by_min.setdefault(b.start, []).append(b)
    rows: dict[date, dict[str, Any]] = {}
    for ts, bars in sorted(by_min.items()):
        d = ts.astimezone(IST).date()
        s = spot.get(ts)
        if s is None:
            continue
        sp = float(s)
        now = ts + timedelta(minutes=1)
        exps = sorted({od.contracts[b.instrument_key].expiry for b in bars} - {e for e in [] if e})
        exps = [e for e in exps if e >= d]
        code1 = exps[0] if exps else None
        code2 = exps[1] if len(exps) > 1 else None
        # per (expiry, strike): IV = the vendor IV of the OTM side when > 0, else own BS inversion of the OTM close,
        # else the ITM side; OI = CE + PE open interest (Dhan reports quantity, not lots)
        by_k: dict[tuple[date, Any], dict[str, Any]] = {}
        for b in bars:
            c = od.contracts[b.instrument_key]
            x = by_k.setdefault((c.expiry, c.strike), {})
            x[c.right.value] = b
        strikes: dict[tuple[date, Any], tuple[float | None, int | None, float]] = {}
        for (e, k), x in by_k.items():
            t = years_to_expiry(now, e)
            otm, itm = ("CE", "PE") if float(k) >= sp else ("PE", "CE")
            iv = None
            for side in (otm, itm):
                b = x.get(side)
                if b is None:
                    continue
                v = od.iv.get((b.instrument_key, b.start))
                if v is not None and v > 0:
                    iv = float(v) / 100
                    break
                iv = implied_vol(float(b.close), sp, float(k), t, side == "CE")
                if iv is not None:
                    break
            ois = [x[r].oi for r in ("CE", "PE") if r in x and x[r].oi is not None]
            strikes[(e, k)] = (iv, sum(ois) if len(ois) == 2 else None, t)
        r = rows.setdefault(d, {"day": d})
        tag = next(k for k, v in SNAPS.items() if v == ts.astimezone(IST).time())
        for code, e in (("1", code1), ("2", code2)):
            ks = [k for (ee, k) in strikes if ee == e]
            if e is None or not ks:
                continue
            k0 = min(ks, key=lambda k: (abs(float(k) - sp), k))
            iv0 = strikes[(e, k0)][0]
            if iv0 is not None:
                r[f"iv{code}_{tag}"] = round(iv0, 5)
        if ts.astimezone(IST).time() != G_AT or code1 is None:
            continue
        s1 = sorted([k for (ee, k) in strikes if ee == code1], key=lambda k: abs(float(k) - sp))[:21]
        s2 = sorted([k for (ee, k) in strikes if ee == code2], key=lambda k: abs(float(k) - sp))[:7]
        g, n, missing = 0.0, 0, 0
        for e, ks in ((code1, s1), (code2, s2)):
            for k in ks:
                iv, oi, t = strikes[(e, k)]
                if iv is None or oi is None:
                    missing += e == code1
                    continue
                g += bs_gamma(sp, float(k), t, iv) * oi * sp * sp * 0.01
                n += 1
        r.update({"G": round(g, 2) if missing <= 3 and n > 0 else None, "G_legs": n, "G_missing_strikes": missing,
                  "spot_1015": sp, "code1_dte": (code1 - d).days})  # fmt: skip
    return list(rows.values())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="d0", required=True)
    p.add_argument("--to", dest="d1", required=True)
    p.add_argument("--lookback", type=int, default=60)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--lake", type=Path, default=Path("lake"))
    a = p.parse_args()
    chunks = month_chunks(date.fromisoformat(a.d0), date.fromisoformat(a.d1))
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        for res in ex.map(chunk, [c[0] for c in chunks], [c[1] for c in chunks], [a.lake] * len(chunks)):
            for r in res:
                if "error" in r:
                    print(json.dumps(r), flush=True)
                else:
                    rows.append(r)
    rows.sort(key=lambda r: r["day"])
    hist: list[float] = []
    for r in rows:
        g = r.get("G")
        if g is not None and len(hist) >= a.lookback:
            past = hist[-a.lookback :]
            r["gex_rank"] = round(100 * sum(1 for x in past if x <= g) / a.lookback, 2)
        if g is not None:
            hist.append(g)
    cols = ["day", "G", "gex_rank", "G_legs", "G_missing_strikes", "spot_1015", "code1_dte",
            *[f"iv{c}_{t}" for t in SNAPS for c in ("1", "2")]]  # fmt: skip
    out = a.lake / "runs" / "regime" / "features" / "gex.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"out": str(out), "days": len(rows), "with_G": sum(1 for r in rows if r.get("G") is not None),
                      "ranked": sum(1 for r in rows if r.get("gex_rank") is not None)}), flush=True)  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

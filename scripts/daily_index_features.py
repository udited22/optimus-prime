#!/usr/bin/env python3
"""Daily point-in-time index features for H37-H41 and the OD-019 credit-spread direction (completed sessions only).

For each session d, from the sessions strictly before d: prev_high, prev_low, prev_close, prev_range, nr7 (1 when
the previous session's range is the narrowest of the last 7), r20 (previous close / close 20 sessions earlier - 1),
sma200 (mean of the last 200 closes), rsi2 (Wilder RSI(2) of daily closes), trend200 (+1 above sma200, -1 below).
Sessions are NIFTY index 1-minute bars 09:15-15:29 IST from the clean lake; the index from 4-Oct-2021 is used as
warm-up only (pre-registered: no option bar and no trade before 29-Aug-2022 is read for any result). The lake's
index has a gap from Nov-2021 to 24-Jul-2022, so in practice no Part A day feeds any feature.

  scripts/daily_index_features.py [--lake lake] [--out lake/runs/vol/features/daily-index.csv]
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

IST = timezone(timedelta(hours=5, minutes=30))


def sessions(lake: Path) -> dict[date, tuple[float, float, float, float]]:
    root = lake / "clean" / "dhan_intraday" / "label=NIFTY-INDEX" / "interval=1m"
    bars: dict[datetime, tuple[float, float, float, float]] = {}
    for f in sorted(root.glob("*/*.parquet")):
        t = pq.read_table(f, columns=["ts", "open", "high", "low", "close"]).to_pydict()
        for ts, o, h, lo, c in zip(t["ts"], t["open"], t["high"], t["low"], t["close"], strict=True):
            bars[ts] = (float(o), float(h), float(lo), float(c))
    by_day: dict[date, list[tuple[datetime, tuple[float, float, float, float]]]] = defaultdict(list)
    for ts, v in bars.items():
        loc = ts.astimezone(IST)
        if time(9, 15) <= loc.time() <= time(15, 29):
            by_day[loc.date()].append((ts, v))
    out = {}
    for d, rows in sorted(by_day.items()):
        rows.sort()
        if len(rows) < 300:  # a partial session is not a daily bar (vendor gaps); skipped, never filled
            continue
        out[d] = (rows[0][1][0], max(r[1][1] for r in rows), min(r[1][2] for r in rows), rows[-1][1][3])
    return out


def rsi2_series(closes: list[float]) -> list[float | None]:
    out: list[float | None] = [None]
    g = lo = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        up, dn = max(ch, 0.0), max(-ch, 0.0)
        if i <= 2:
            g += up / 2
            lo += dn / 2
        else:
            g = (g + up) / 2
            lo = (lo + dn) / 2
        out.append(None if i < 2 else (100.0 if lo == 0 else 100 - 100 / (1 + g / lo)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lake", type=Path, default=Path("lake"))
    ap.add_argument("--out", type=Path, default=Path("lake/runs/vol/features/daily-index.csv"))
    a = ap.parse_args()
    ses = sessions(a.lake)
    days = sorted(ses)
    closes = [ses[d][3] for d in days]
    # a vendor gap of more than 10 calendar days breaks the series (the lake has Oct-2021, then nothing until
    # 25-Jul-2022): lookbacks never span a break, so the first usable day is after the latest break
    seg = [0] * len(days)
    for i in range(1, len(days)):
        seg[i] = i if (days[i] - days[i - 1]).days > 10 else seg[i - 1]
    rsi: list[float | None] = [None] * len(days)
    for st in sorted(set(seg)):
        idx = [i for i in range(len(days)) if seg[i] == st]
        for i, v in zip(idx, rsi2_series([closes[i] for i in idx]), strict=True):
            rsi[i] = v
    rows = []
    for i in range(1, len(days)):
        j = i - 1  # the previous completed session
        if seg[i] != seg[j]:
            continue  # the previous session is before a break: no feature
        j0 = seg[j]
        _, h, lo, c = ses[days[j]]
        r: dict[str, str] = {"day": days[i].isoformat(), "prev_high": f"{h:.2f}", "prev_low": f"{lo:.2f}",
                             "prev_close": f"{c:.2f}", "prev_range": f"{h - lo:.2f}"}  # fmt: skip
        if j - j0 >= 6:
            rngs = [ses[days[k]][1] - ses[days[k]][2] for k in range(j - 6, j + 1)]
            r["nr7"] = "1" if rngs[-1] <= min(rngs) else "0"
        if j - j0 >= 20:
            r["r20"] = f"{closes[j] / closes[j - 20] - 1:.6f}"
        if j - j0 >= 199:
            sma = sum(closes[j - 199 : j + 1]) / 200
            r["sma200"] = f"{sma:.2f}"
            r["trend200"] = "1" if c > sma else "-1"
        if rsi[j] is not None:
            r["rsi2"] = f"{rsi[j]:.3f}"
        rows.append(r)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["day", "prev_high", "prev_low", "prev_close", "prev_range", "nr7", "r20", "sma200", "trend200", "rsi2"]
    with a.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    first200 = next((r["day"] for r in rows if "sma200" in r), None)
    print(f"{len(ses)} sessions {days[0]}..{days[-1]}; {len(rows)} feature rows; sma200 from {first200} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

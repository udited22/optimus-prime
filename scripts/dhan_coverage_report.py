#!/usr/bin/env python3
"""Dhan backfill coverage + DQ report (D-06). Reads the job store and manifests only: no network, no token.

  scripts/dhan_coverage_report.py --lake lake --out docs/data/dhan-coverage-2026-10-02.md

For every series in the backfill plan it reports chunk status, DQ status (PASS / WARN / BLOCKED = quarantined),
issue codes, rows, the trading days that have bars versus the NSE F&O calendar, and the newest contiguous
range in which every planned chunk is finished (the range a backtest can read). The report holds no secrets,
no account data and no market prices, so it may be committed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.calendar import TradingCalendar, load_holiday_book  # noqa: E402
from project100c.data.dhan import CandleJobSpec, load_dhan_config, plan_chunks  # noqa: E402
from project100c.data.dhan.backfill import (  # noqa: E402
    ACTIVE_FUTURES,
    DEFAULT_ANCHOR,
    BackfillItem,
    plan_backfill,
    plan_sensex_backfill,
)
from project100c.data.dhan.config import DhanConfig  # noqa: E402
from project100c.data.dhan.jobs import DQ_CHECKS_VERSION, PLAN_VERSION, job_id_for  # noqa: E402
from project100c.sessions import IST  # noqa: E402

CONFIGS = REPO / "configs"
FINISHED = ("DONE", "NO_DATA")


@dataclass
class Group:
    """One reported series: e.g. index, VIX, a futures contract, or one option (expiry code, offset, right)."""

    name: str
    tier_windows: dict[str, list[tuple[date, date]]] = field(default_factory=lambda: defaultdict(list))
    status: Counter[str] = field(default_factory=Counter)
    dq: Counter[str] = field(default_factory=Counter)
    issues: Counter[str] = field(default_factory=Counter)
    rows: int = 0
    days: set[date] = field(default_factory=set)
    expected: set[date] = field(default_factory=set)
    windows: dict[tuple[date, date], bool] = field(default_factory=dict)  # window -> finished, none BLOCKED
    quarantined: list[str] = field(default_factory=list)


def _chunk_group(it: BackfillItem, meta: dict[str, object]) -> str:
    if isinstance(it.spec, CandleJobSpec):
        return it.spec.label
    return f"OPT code{meta['expiry_code']} ATM{int(str(meta['strike_offset'])):+d} {meta['right']}"


def _trading_days(cal: TradingCalendar, lo: date, hi_exclusive: date) -> set[date]:
    return {d for d in cal.trading_days(lo, hi_exclusive.fromordinal(hi_exclusive.toordinal() - 1))}


def _days_in_part(path: Path) -> set[date]:
    col = pq.read_table(path, columns=["ts"]).column("ts").to_pylist()
    return {t.astimezone(IST).date() for t in col}


def collect(lake: Path, cfg: DhanConfig, items: Sequence[BackfillItem], cal: TradingCalendar) -> dict[str, Group]:
    path = lake / "jobs" / "dhan_jobs.sqlite"
    db = sqlite3.connect(path if path.exists() else ":memory:", timeout=60)
    has_store = db.execute("SELECT 1 FROM sqlite_master WHERE name='chunks'").fetchone() is not None
    groups: dict[str, Group] = {}
    for it in items:
        jid = job_id_for(it.spec)
        rows = (
            []
            if not has_store
            else db.execute(
                "SELECT chunk_key, meta_json, window_from, window_to, status, rows, dq_status, manifest_path "
                "FROM chunks WHERE job_id=?",
                (jid,),
            ).fetchall()
        )
        if not rows:  # not created yet: count the planned chunks as PENDING
            for ch in plan_chunks(it.spec, cfg):
                g = groups.setdefault(_chunk_group(it, dict(ch.meta)), Group(_chunk_group(it, dict(ch.meta))))
                g.status["PENDING"] += 1
                g.windows[(ch.window_from, ch.window_to)] = False
            continue
        for _key, meta_json, wf, wt, st, n, dqs, mp in rows:
            meta = json.loads(meta_json)
            name = _chunk_group(it, meta)
            g = groups.setdefault(name, Group(name))
            w = (date.fromisoformat(wf), date.fromisoformat(wt))
            g.status[st] += 1
            g.windows[w] = g.windows.get(w, True) and st in FINISHED and dqs != "BLOCKED"
            if st not in FINISHED:
                continue
            g.expected |= _trading_days(cal, *w)
            g.rows += int(n or 0)
            if dqs:
                g.dq[dqs] += 1
            if not mp:
                continue
            man = json.loads((lake / mp).read_text())
            for code in man.get("dq", {}).get("issue_counts", {}):
                g.issues[code] += 1
            if dqs == "BLOCKED":
                blocking = sorted(c.split(":", 1)[1] for c in man["dq"]["issue_counts"] if c.startswith("BLOCKING"))
                g.quarantined.append(f"{wf}..{wt} ({', '.join(blocking)})")
            part = man.get("part")
            if dqs != "BLOCKED" and part and part.get("path") and part.get("rows"):
                g.days |= _days_in_part(lake / part["path"])
    db.close()
    return groups


def newest_contiguous(windows: dict[tuple[date, date], bool]) -> tuple[date, date] | None:
    """Newest-first run of windows whose chunks are all finished: [from, to) or None."""
    lo: date | None = None
    hi: date | None = None
    for (wf, wt), ok in sorted(windows.items(), key=lambda kv: kv[0], reverse=True):
        if not ok:
            break
        if hi is None:
            hi = wt
        lo = wf
    return None if lo is None or hi is None else (lo, hi)


def _fmt_range(r: tuple[date, date] | None) -> str:
    return "none yet" if r is None else f"{r[0]} .. {r[1].fromordinal(r[1].toordinal() - 1)}"


def _missing(g: Group) -> str:
    miss = sorted(g.expected - g.days)
    if not miss:
        return "0"
    head = ", ".join(str(d) for d in miss[:6])
    return f"{len(miss)} ({head}{', ...' if len(miss) > 6 else ''})"


def _status(c: Counter[str]) -> str:
    return " ".join(f"{k}={c[k]}" for k in ("DONE", "NO_DATA", "FAILED", "PENDING") if c[k])


SENSEX_ANCHOR = "2026-10-03"  # = scripts/dhan_backfill.py SENSEX_DEFAULT_ANCHOR


def render(
    groups: dict[str, Group],
    cfg: DhanConfig,
    items: Sequence[BackfillItem],
    generated: datetime,
    market: str = "nifty",
) -> str:
    sensex = market == "sensex"
    total = Counter[str]()
    for g in groups.values():
        total.update(g.status)
    planned = sum(total.values())
    out: list[str] = [
        "# Dhan backfill coverage and DQ report" + (" (SENSEX, BSE)" if sensex else ""),
        "",
        f"Generated {generated.strftime('%d-%b-%Y %H:%M')} IST by `scripts/dhan_coverage_report.py` from the job "
        "store and chunk manifests (no network). **REAL data from the Dhan Data API; this file holds no prices, "
        "no token and no account data.**",
        "",
        f"- Plan: anchor {SENSEX_ANCHOR if sensex else DEFAULT_ANCHOR} (exclusive), plan version {PLAN_VERSION}, "
        f"{len(items)} jobs, "
        f"{planned} chunks (1 chunk = 1 request). Config `{cfg.version}`, DQ checks `{DQ_CHECKS_VERSION}`.",
        f"- Progress: {_status(total)} ({100 * (total['DONE'] + total['NO_DATA']) / max(planned, 1):.1f}% finished).",
        "- DQ status per finished chunk: PASS = no issue; WARN = non-blocking issues (kept, readable with "
        "`accept_warn`); BLOCKED = quarantined, never read by a backtest.",
        "- Usable range = the newest contiguous run of windows whose chunks are all finished and none BLOCKED. Days "
        "with bars (from non-quarantined parts) are counted against the "
        f"{'BSE' if sensex else 'NSE'} F&O trading calendar for finished windows only.",
        "",
        "## Index, VIX and futures",
        "",
        "| Series | Chunks | DQ (PASS/WARN/BLOCKED) | Rows | Usable range (newest contiguous) | Trading days with "
        "bars / expected | Missing days | Issue codes (chunks) |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def dq(g: Group) -> str:
        return f"{g.dq['PASS']}/{g.dq['WARN']}/{g.dq['BLOCKED']}"

    def issues(g: Group) -> str:
        return ", ".join(f"{k} ({v})" for k, v in sorted(g.issues.items())) or "none"

    candles = [g for n, g in groups.items() if not n.startswith("OPT ")]
    for g in candles:
        out.append(
            f"| {g.name} | {_status(g.status)} | {dq(g)} | {g.rows:,} | {_fmt_range(newest_contiguous(g.windows))} | "
            f"{len(g.days & g.expected)} / {len(g.expected)} | {_missing(g)} | {issues(g)} |"
        )
    out += [
        "",
        "Futures: Dhan returns intraday candles only for **active** contracts (expired ones have no reachable "
        "security id), so futures history starts about three months before each contract's expiry.",
        "",
        "## Options (rolling, by expiry code, ATM offset and right)",
        "",
        "Expiry code 1 = nearest expiry on or after the trade date (including the expiry day); code 2 = the next.",
        "",
        "| Option series | Chunks | DQ (PASS/WARN/BLOCKED) | Rows | Usable range | Days with bars / expected "
        "| Missing days | Issue codes (chunks) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    opts = sorted(
        (g for n, g in groups.items() if n.startswith("OPT ")),
        key=lambda g: (g.name.split()[1], int(g.name.split()[2][3:]), g.name.split()[3]),
    )
    for g in opts:
        out.append(
            f"| {g.name} | {_status(g.status)} | {dq(g)} | {g.rows:,} | {_fmt_range(newest_contiguous(g.windows))} | "
            f"{len(g.days & g.expected)} / {len(g.expected)} | {_missing(g)} | {issues(g)} |"
        )
    agg = Counter[str]()
    for g in groups.values():
        agg.update(g.issues)
    out += ["", "## DQ issue codes across the plan (number of chunks)", "", "| Severity:code | Chunks |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in sorted(agg.items())] or ["| none | 0 |"]
    out += ["", "## Quarantined (BLOCKED) chunks", ""]
    q = [(g.name, x) for g in candles + opts for x in g.quarantined]
    out += [f"- {n}: {x}" for n, x in q] or ["- none"]
    if sensex:
        return "\n".join(out)
    out += [
        "",
        "## H01-usable window",
        "",
        "H01 needs index + VIX bars and, per day, options ATM-2..ATM+2 (CE and PE, expiry code 1); the futures "
        "volume input is an ASSUMED proxy (index prices, ATM-band option volume) because expired futures are "
        "not available. The usable range is the overlap of those series' complete ranges:",
        "",
        f"- {_fmt_range(_h01_range(groups))}",
        "",
    ]
    return "\n".join(out)


def _h01_range(groups: dict[str, Group]) -> tuple[date, date] | None:
    names = ["NIFTY-INDEX", "INDIA-VIX"] + [f"OPT code1 ATM{o:+d} {r}" for o in range(-2, 3) for r in ("CE", "PE")]
    rs = [newest_contiguous(groups[n].windows) if n in groups else None for n in names]
    if any(r is None for r in rs):
        return None
    lo = max(r[0] for r in rs if r is not None)
    hi = min(r[1] for r in rs if r is not None)
    return (lo, hi) if lo < hi else None


def main(argv: Iterable[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lake", type=Path, default=REPO / "lake")
    p.add_argument("--out", type=Path)
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--market", choices=("nifty", "sensex"), default="nifty")
    a = p.parse_args(list(argv))
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    if a.market == "sensex":
        items = plan_sensex_backfill(anchor=date.fromisoformat(SENSEX_ANCHOR), years=a.years)
        book = "bse_fo_holidays.toml"
    else:
        items = plan_backfill(anchor=date.fromisoformat(DEFAULT_ANCHOR), years=a.years, futures=ACTIVE_FUTURES)
        book = "nse_fo_holidays.toml"
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / book))
    text = render(collect(a.lake, cfg, items, cal), cfg, items, datetime.now(IST), a.market)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text)
        print(f"wrote {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Checkpointed crypto backfill from Binance's public data archive (DATA ONLY, no API key, no account).

  universe  fetch a keyless market-cap ranking (CoinLore) and Binance spot exchangeInfo, store both raw in the
            crypto lake, and check that configs/data/crypto_universe.toml matches the snapshot's selection
  plan      list the archive for every (dataset, symbol) and register one task per published file up to
            --through (default: yesterday UTC). Re-planning later adds new files and replaces daily files
            whose month has since been published as a monthly file
  run       download + verify .CHECKSUM + ingest + DQ every PENDING/FAILED task of --kinds (checkpoint = SQLite)
  status    task counts per dataset and symbol
  report    write the coverage and DQ report (counts and dates only, no prices) as Markdown

Lake: lake/crypto (gitignored): raw/ (verified zips), clean/ and quarantine/ (Parquet), manifests/, jobs/.
Run in the background with nohup (an interruption loses at most the file in flight):

  nohup .venv/bin/python scripts/crypto_backfill.py run --kinds spot_klines --rps 3 > logs/crypto_spot.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.data.binance.archive import ArchiveKind, BinanceArchive  # noqa: E402
from project100c.data.binance.config import BinanceConfig, load_binance_config  # noqa: E402
from project100c.data.binance.jobs import (  # noqa: E402
    CryptoDownloader,
    CryptoJobStore,
    TaskStatus,
    plan_series,
)
from project100c.data.binance.universe import load_universe, rank_snapshot  # noqa: E402
from project100c.data.http import UrllibTransport  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.data.public_http import PublicGetter, RetryPolicy  # noqa: E402
from project100c.data.ratelimit import RateLimiter  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.errors import Project100CError  # noqa: E402
from project100c.sessions import IST  # noqa: E402

CONFIGS = REPO / "configs"
RANKING_HOST = "api.coinlore.net"
RANKING_URL = "https://api.coinlore.net/api/tickers/?start=0&limit=60"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _getter(cfg: BinanceConfig, store: CryptoJobStore, rps: int, hosts: frozenset[str]) -> PublicGetter:
    lim = RateLimiter(
        per_second=rps,
        per_day=cfg.requests_per_day,
        clock=time.monotonic,
        sleep=time.sleep,
        wall_clock=_now,
        quota=store,
    )
    pol = RetryPolicy(
        cfg.retry_server_max_attempts,
        cfg.retry_rate_limit_max_attempts,
        float(cfg.backoff_base_seconds),
        float(cfg.backoff_cap_seconds),
        float(cfg.http_timeout_seconds),
    )
    return PublicGetter(transport=UrllibTransport(hosts), limiter=lim, policy=pol)


def _ctx(a: argparse.Namespace) -> tuple[BinanceConfig, Lake, CryptoJobStore]:
    cfg = load_binance_config(CONFIGS / "data" / "binance.toml")
    root = Path(a.lake)
    return cfg, Lake(root), CryptoJobStore(root / "jobs" / "crypto_jobs.sqlite", wall_clock=_now)


def _kinds(s: str) -> list[ArchiveKind]:
    return [ArchiveKind(k.strip()) for k in s.split(",") if k.strip()]


def cmd_universe(a: argparse.Namespace) -> int:
    cfg, lake, store = _ctx(a)
    u = load_universe(CONFIGS / "data" / "crypto_universe.toml")
    g = _getter(cfg, store, 1, frozenset({RANKING_HOST}))
    res = g.get(RANKING_URL, accept="application/json")
    if res is None:
        print("ranking source returned 404")
        return 2
    today = _now().date()
    ref = lake.write_raw(
        "coinlore",
        "tickers",
        today,
        "tickers-top60",
        res.response.body,
        sidecar={"fetched_at_utc": _now().isoformat(), "url": RANKING_URL},
    )
    sel = rank_snapshot(
        res.response.body, top_n=u.top_n, excluded=set(u.exclude_stablecoins) | set(u.exclude_wrapped_or_staked)
    )
    got = [s["asset"] for s in sel]
    want = [s.asset for s in u.symbols]
    print(f"{_stamp()} snapshot {ref.path}")
    print(f"  selection now : {got}")
    print(f"  pinned config : {want}  ({u.version}, as of {u.as_of_utc})")
    bg = _getter(cfg, store, 2, frozenset(cfg.allowed_hosts))
    status: dict[str, Any] = {}
    for pair in u.pairs():
        r = bg.get(f"{cfg.rest_base}/api/v3/exchangeInfo?symbol={pair}", accept="application/json")
        if r is None:
            status[pair] = {"spot": "NOT_LISTED"}
            continue
        body = json.loads(r.response.body)
        syms = body.get("symbols") or []
        status[pair] = {"spot": syms[0]["status"] if syms else "NOT_LISTED"}
        lake.write_raw(
            "binance", "spot_exchangeinfo", today, pair, r.response.body, sidecar={"fetched_at_utc": _now().isoformat()}
        )
    print("  Binance spot status:", {k: v["spot"] for k, v in status.items()})
    lake.write_manifest(
        "crypto_universe",
        f"universe-{today.isoformat()}",
        {
            "version": u.version,
            "snapshot_raw": ref.path,
            "selection_now": got,
            "pinned": want,
            "spot_status": status,
            "checked_at_utc": _now().isoformat(),
        },
    )
    return 0 if got == want else 3


def cmd_plan(a: argparse.Namespace) -> int:
    cfg, lake, store = _ctx(a)
    u = load_universe(CONFIGS / "data" / "crypto_universe.toml")
    through = date.fromisoformat(a.through) if a.through else _now().date() - timedelta(days=1)
    arch = BinanceArchive(config=cfg, getter=_getter(cfg, store, a.rps, frozenset(cfg.allowed_hosts)))
    for kind in _kinds(a.kinds):
        for pair in u.pairs():
            r = plan_series(archive=arch, store=store, lake=lake, kind=kind, symbol=pair, through=through)
            print(
                f"{_stamp()} {kind.value:15s} {pair:9s} monthly={r.monthly:3d} daily={r.daily:4d} new={r.new:4d} "
                f"replaced={r.replaced} first={r.first_period} last={r.last_period}"
            )
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    cfg, lake, store = _ctx(a)
    thr = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    arch = BinanceArchive(config=cfg, getter=_getter(cfg, store, a.rps, frozenset(cfg.allowed_hosts)))
    dl = CryptoDownloader(
        archive=arch,
        lake=lake,
        store=store,
        config_version=cfg.version,
        max_missing_fraction=thr.max_missing_candle_fraction,
        thresholds_version=thr.version,
        wall_clock=_now,
        log=lambda m: print(f"{_stamp()} {m}", flush=True),
    )
    print(f"{_stamp()} run kinds={a.kinds} rps={a.rps}", flush=True)
    try:
        s = dl.run(kinds=_kinds(a.kinds), max_tasks=a.max_tasks)
    except Project100CError as e:
        print(f"{_stamp()} STOPPED: {type(e).__name__}: {e}", flush=True)
        return 2
    print(
        f"{_stamp()} finished attempted={s.attempted} done={s.done} quarantined={s.quarantined} "
        f"not_published={s.not_published} failed={s.failed} rows={s.rows}",
        flush=True,
    )
    return 1 if s.failed else 0


def cmd_reingest(a: argparse.Namespace) -> int:
    cfg, lake, store = _ctx(a)
    thr = load_thresholds(CONFIGS / "dq" / "thresholds.toml")
    arch = BinanceArchive(config=cfg, getter=_getter(cfg, store, 1, frozenset(cfg.allowed_hosts)))
    dl = CryptoDownloader(
        archive=arch,
        lake=lake,
        store=store,
        config_version=cfg.version,
        max_missing_fraction=thr.max_missing_candle_fraction,
        thresholds_version=thr.version,
        wall_clock=_now,
    )
    print(f"re-ingested {dl.reingest(kinds=_kinds(a.kinds))} file(s) from stored raw bytes (no network)")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    _cfg, _lake, store = _ctx(a)
    by: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for (k, s, st), n in store.counts().items():
        by[(k, s)][st] = n
    tot: dict[str, int] = defaultdict(int)
    for (k, s), c in sorted(by.items()):
        print(f"{k:15s} {s:9s} " + " ".join(f"{st}={n}" for st, n in sorted(c.items())))
        for st, n in c.items():
            tot[st] += n
    print("TOTAL", dict(tot))
    return 0


def _fmt_ts(s: str | None) -> str:
    return "-" if not s else s[:16].replace("T", " ")


def cmd_report(a: argparse.Namespace) -> int:
    _cfg, lake, store = _ctx(a)
    u = load_universe(CONFIGS / "data" / "crypto_universe.toml")
    rows = store.series_rows()
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    issues: dict[str, int] = defaultdict(int)
    days_over: dict[tuple[str, str], list[str]] = defaultdict(list)
    longest: dict[tuple[str, str], int] = defaultdict(int)
    quarantined: list[str] = []
    for kind, sym, _period, pid, status, nrows, dq, zone, first, last, missing, manifest in rows:
        g = agg.setdefault(
            (kind, sym),
            {
                "files": 0,
                "done": 0,
                "quar": 0,
                "rows": 0,
                "first": None,
                "last": None,
                "missing": 0,
                "warn": 0,
                "replaced": 0,
                "pending": 0,
                "failed": 0,
                "notpub": 0,
            },
        )
        if status == TaskStatus.REPLACED.value:
            g["replaced"] += 1
            continue
        g["files"] += 1
        if status == TaskStatus.DONE.value:
            g["done"] += 1
            if zone == "quarantine":
                g["quar"] += 1
                quarantined.append(f"{kind} {sym} {pid}")
            else:
                g["rows"] += int(nrows or 0)
            g["missing"] += int(missing or 0)
            g["warn"] += dq == "WARN"
            if first and (g["first"] is None or first < g["first"]):
                g["first"] = first
            if last and (g["last"] is None or last > g["last"]):
                g["last"] = last
            if manifest:
                m = lake.read_manifest(manifest)
                for k, n in m["dq"]["issue_counts"].items():
                    issues[f"{kind} {k}"] += int(n)
                st = m["dq"]["stats"]
                days_over[(kind, sym)] += st.get("days_over_threshold", [])
                longest[(kind, sym)] = max(longest[(kind, sym)], int(st.get("longest_gap_slots", 0)))
        elif status in (TaskStatus.PENDING.value,):
            g["pending"] += 1
        elif status == TaskStatus.FAILED.value:
            g["failed"] += 1
        elif status == TaskStatus.NOT_PUBLISHED.value:
            g["notpub"] += 1
    out: list[str] = []
    w = out.append
    w(f"# Crypto coverage and data-quality report ({_now().astimezone(IST).strftime('%d-%b-%Y %H:%M')} IST)\n")
    w(
        "Source: Binance public data archive (https://data.binance.vision), keyless; every file verified against "
        "its published `.CHECKSUM` (SHA-256) before ingest. Data lives in the gitignored `lake/crypto/`; this "
        "report holds counts and dates only, no prices. Generated by `scripts/crypto_backfill.py report`.\n"
    )
    w(
        f"Universe `{u.version}` (as of {u.as_of_utc}, ranking {u.ranking_source}): "
        + ", ".join(f"{s.asset}" for s in u.symbols)
        + f", quoted in {u.quote}.\n"
    )
    labels = {
        "spot_klines": "Spot 1-minute klines",
        "um_klines": "USD-M perpetual 1-minute klines",
        "um_fundingrate": "USD-M perpetual rate settlements (fundingRate)",
        "um_metrics": "USD-M metrics (5-minute open interest, long/short ratios)",
    }
    for kind in ("spot_klines", "um_klines", "um_fundingrate", "um_metrics"):
        keys = [k for k in agg if k[0] == kind]
        if not keys:
            continue
        w(f"\n## {labels[kind]}\n")
        w(
            "| Symbol | Files (done / planned) | Quarantined | Rows in clean | First bar (UTC) | Last bar (UTC) | "
            "Missing slots | Longest gap (slots) | UTC days > 1% missing | Pending / failed |"
        )
        w("|---|---|---|---|---|---|---|---|---|---|")
        tot_rows = 0
        for kd, sym in sorted(keys, key=lambda k: u.pairs().index(k[1]) if k[1] in u.pairs() else 99):
            g = agg[(kd, sym)]
            tot_rows += g["rows"]
            w(
                f"| {sym} | {g['done']:,} / {g['files']:,} | {g['quar']} | {g['rows']:,} | {_fmt_ts(g['first'])} | "
                f"{_fmt_ts(g['last'])} | {g['missing']:,} | {longest[(kd, sym)]:,} | {len(days_over[(kd, sym)])} | "
                f"{g['pending']} / {g['failed']} |"
            )
        w(f"\nTotal rows in clean: **{tot_rows:,}**.\n")
    w("\n## DQ issue counts (issues per file, summed)\n")
    w("| Dataset and issue | Count |\n|---|---|")
    for k, n in sorted(issues.items()):
        w(f"| {k} | {n:,} |")
    w("\n## Quarantined files (BLOCKED; never read by a backtest)\n")
    w("\n".join(f"- {q}" for q in sorted(quarantined)) if quarantined else "None.")
    _write_listings(w, store, u.pairs())
    pending = sum(g["pending"] for g in agg.values())
    _write_notes(w, pending)
    Path(a.out).write_text("\n".join(out) + "\n")
    print(f"wrote {a.out}")
    return 0


def _write_listings(w: Callable[[str], object], store: CryptoJobStore, pairs: Sequence[str]) -> None:
    w("\n## Listing and delisting dates (from the archive's own file listing)\n")
    w(
        "First and last archive period found on data.binance.vision when the plan was made (monthly `YYYY-MM` or "
        "daily `YYYY-MM-DD`). A last period before the scan day means the pair stopped trading on Binance "
        "(delisted); a first period after a market's launch means a later listing.\n"
    )
    w("| Symbol | Spot first | Spot last | USD-M perp first | USD-M perp last | Metrics first |")
    w("|---|---|---|---|---|---|")
    for sym in pairs:
        cells: list[str] = []
        for kind in (ArchiveKind.SPOT_KLINES, ArchiveKind.UM_KLINES, ArchiveKind.UM_METRICS):
            li = store.listing(kind, sym)
            first, last = (li[0], li[1]) if li else (None, None)
            cells.append(first or "-")
            if kind is not ArchiveKind.UM_METRICS:
                cells.append(last or "-")
        w(f"| {sym} | " + " | ".join(cells) + " |")


def _write_notes(w: Callable[[str], object], pending: int) -> None:
    status = (
        f"**SNAPSHOT: {pending:,} files were still pending when this report was generated**; re-run "
        "`scripts/crypto_backfill.py run` (it resumes) and then `report` to refresh."
        if pending
        else "All planned files are finished."
    )
    w("\n## Notes, biases and limits\n")
    w(
        f"- Run status: {status}\n"
        "- **Survivorship bias.** The universe is TODAY's top 10 by market cap (CoinLore snapshot). It was not the "
        "top 10 in earlier years (e.g. LUNA, DOT, MATIC, AVAX, ADA, BCH and LTC ranked high in 2018-2022; HYPE did not "
        "exist). A backtest over this list overstates what a point-in-time strategy could have picked. No free, "
        "keyless point-in-time market-cap membership history was found; a fair test needs one (e.g. a paid "
        "historical-rank feed) or a rule-based universe built from Binance volume itself.\n"
        "- Binance-only coverage: pairs delisted from Binance (XMR spot, 20-Feb-2024) end there even though the "
        "asset still trades elsewhere; prices are Binance USDT prices, not a consolidated index.\n"
        "- The USD-M archive starts 2019-12-31/2020-01 even though the BTC perpetual launched in Sep-2019; the "
        "metrics archive starts 2020-09 for BTC and 2021-12 for the others.\n"
        "- Spot Dec-2017 and Feb-2018 monthly files for BTC, ETH and BNB carry bars stamped off the minute (an "
        "exchange-side artefact: from 4-Dec-2017 06:00 UTC, and after the 8/9-Feb-2018 outage); "
        "they are quarantined, so spot history has holes there. "
        "The 'Longest gap' column includes quarantined files.\n"
        "- Gaps in clean files are real exchange outages or maintenance windows (e.g. 600-minute and 4,320-minute "
        "gaps), kept as WARN and never filled.\n"
        "- The REST API fallback (api.binance.com / fapi.binance.com) is not implemented: both answer HTTP 451 "
        "from this box's region, and the archive covers everything up to yesterday (UTC).\n"
        "- INR prices: CoinDCX publishes public INR-quoted market data and is the candidate if an INR series is ever "
        "needed (Indian tax and TDS treatment differs); it is NOT used here.\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lake", default=str(REPO / "lake" / "crypto"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("universe")
    pl = sub.add_parser("plan")
    pl.add_argument("--kinds", default="spot_klines,um_klines,um_fundingrate,um_metrics")
    pl.add_argument("--through", default=None)
    pl.add_argument("--rps", type=int, default=3)
    r = sub.add_parser("run")
    r.add_argument("--kinds", required=True)
    r.add_argument("--rps", type=int, default=3)
    r.add_argument("--max-tasks", type=int, default=None)
    ri = sub.add_parser("reingest")
    ri.add_argument("--kinds", required=True)
    sub.add_parser("status")
    rep = sub.add_parser("report")
    rep.add_argument("--out", default=str(REPO / "docs" / "data" / "crypto-coverage-2026-10-03.md"))
    a = p.parse_args(argv)
    fn = {
        "universe": cmd_universe,
        "plan": cmd_plan,
        "run": cmd_run,
        "reingest": cmd_reingest,
        "status": cmd_status,
        "report": cmd_report,
    }[a.cmd]
    return fn(a)


if __name__ == "__main__":
    raise SystemExit(main())

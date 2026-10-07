#!/usr/bin/env python3
"""US starter dataset (DATA ONLY). No key needed except for the optional ``alpaca-minute`` command.

  run      --steps constituents,daily,cboe,minute (default: all four). Daily = full history for the S&P 500
           current constituents plus the configured ETFs (SPY, QQQ) and indices (^GSPC, ^NDX); 1-minute = the
           last ~30 days for --minute-scope (etfs | all); Cboe = VIX, VIX9D, VIX3M, VVIX, SKEW history.
  status   task counts per dataset and status
  report   coverage + DQ summary as Markdown (counts and dates only, no prices)
  alpaca-minute  1-minute SIP bars from Alpaca for --symbols over the last --days calendar days. Needs
           ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in the environment (free Basic plan).

Lake: lake/us (gitignored). Resumable: re-running skips finished units. Example:
  nohup .venv/bin/python scripts/us_backfill.py run > logs/us_backfill.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.data.http import UrllibTransport  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.data.public_http import PublicGetter, RetryPolicy  # noqa: E402
from project100c.data.ratelimit import RateLimiter  # noqa: E402
from project100c.data.us import (  # noqa: E402
    DS_1M,
    DS_CBOE,
    DS_CONS,
    DS_DAILY,
    AlpacaBarsClient,
    AlpacaCredentials,
    Status,
    UsDownloader,
    UsJobStore,
    UsMarketConfig,
    load_nyse_sessions,
    load_us_config,
)
from project100c.errors import Project100CError  # noqa: E402
from project100c.sessions import IST  # noqa: E402

CONFIGS = REPO / "configs"
DEFAULT_REPORT = REPO / "docs" / "data" / "us-coverage-2026-10-03.md"
ALPACA_HOST = "data.alpaca.markets"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _log(m: str) -> None:
    print(f"{_stamp()} {m}", flush=True)


def _policy(cfg: UsMarketConfig) -> RetryPolicy:
    return RetryPolicy(
        cfg.retry_server_max_attempts,
        cfg.retry_rate_limit_max_attempts,
        float(cfg.backoff_base_seconds),
        float(cfg.backoff_cap_seconds),
        float(cfg.http_timeout_seconds),
    )


def _limiter(cfg: UsMarketConfig, store: UsJobStore, rps: int) -> RateLimiter:
    return RateLimiter(
        per_second=rps,
        per_day=cfg.requests_per_day,
        clock=time.monotonic,
        sleep=time.sleep,
        wall_clock=_now,
        quota=store,
    )


def _ctx(a: argparse.Namespace) -> tuple[UsMarketConfig, Lake, UsJobStore, UsDownloader]:
    cfg = load_us_config(CONFIGS / "data" / "us_market.toml")
    nyse = load_nyse_sessions(CONFIGS / "calendar" / "nyse_sessions.toml")
    root = Path(a.lake)
    lake = Lake(root)
    store = UsJobStore(root / "jobs" / "us_jobs.sqlite", wall_clock=_now)
    hosts = frozenset(h for h in cfg.allowed_hosts if h != ALPACA_HOST)  # the keyless getter never goes there
    getter = PublicGetter(
        transport=UrllibTransport(hosts), limiter=_limiter(cfg, store, cfg.requests_per_second), policy=_policy(cfg)
    )
    dl = UsDownloader(
        config=cfg,
        nyse=nyse,
        getter=getter,
        lake=lake,
        store=store,
        wall_clock=_now,
        thresholds_version=cfg.version,
        max_missing_fraction=cfg.max_missing_fraction,
        log=_log,
    )
    return cfg, lake, store, dl


def cmd_run(a: argparse.Namespace) -> int:
    cfg, _lake, _store, dl = _ctx(a)
    steps = {s.strip() for s in a.steps.split(",") if s.strip()}
    # last completed US session by default: yesterday in New York (today's bars may still be forming)
    as_of = date.fromisoformat(a.as_of) if a.as_of else dl.today_ny() - timedelta(days=1)
    need_members = bool(steps & {"constituents", "daily"}) or ("minute" in steps and a.minute_scope == "all")
    members = dl.constituents(dl.today_ny()) if need_members else []
    if members:
        _log(f"constituents: {len(members)} current S&P 500 members (survivorship-biased list)")
    universe = [*cfg.etfs, *cfg.indices, *members]
    if "daily" in steps:
        _log(f"daily as_of={as_of}: {dl.daily(universe, as_of, max_symbols=a.max_symbols).line()}")
    if "cboe" in steps:
        _log(f"cboe as_of={as_of}: {dl.cboe(as_of).line()}")
    if "minute" in steps:
        syms = [*cfg.etfs, *cfg.indices] if a.minute_scope == "etfs" else universe
        _log(f"minute: {dl.minute(syms, max_symbols=a.max_symbols).line()}")
    return 0


def cmd_alpaca_minute(a: argparse.Namespace) -> int:
    cfg, _lake, store, dl = _ctx(a)
    try:
        creds = AlpacaCredentials.from_env(os.environ)
    except Project100CError as e:
        print(e)
        return 2
    client = AlpacaBarsClient(
        base=cfg.alpaca_data_base,
        transport=UrllibTransport(frozenset({ALPACA_HOST})),
        limiter=_limiter(cfg, store, cfg.alpaca_requests_per_second),
        policy=_policy(cfg),
        credentials=creds,
        tz=load_nyse_sessions(CONFIGS / "calendar" / "nyse_sessions.toml").tz,
    )
    today = dl.today_ny()
    days = [today - timedelta(days=i) for i in range(a.days, 0, -1)]
    s = dl.alpaca_minute(client, [x.strip() for x in a.symbols.split(",") if x.strip()], feed=a.feed, days=days)
    _log(f"alpaca minute: {s.line()}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    _cfg, _lake, store, _dl = _ctx(a)
    c: Counter[tuple[str, str, str]] = Counter()
    rows: dict[str, int] = defaultdict(int)
    for t in store.tasks():
        c[(t.dataset, t.status.value, t.dq_status or "")] += 1
        rows[t.dataset] += t.rows or 0
    for (ds, st, dq), n in sorted(c.items()):
        print(f"{ds:22s} {st:10s} {dq:8s} {n:6d}")
    for ds, n in sorted(rows.items()):
        print(f"{ds:22s} rows={n}")
    return 0


def _issue_counts(lake: Lake, manifest: str | None) -> Counter[str]:
    out: Counter[str] = Counter()
    if not manifest:
        return out
    m = lake.read_manifest(manifest)
    if "years" in m:
        for y in m["years"].values():
            out.update(y["dq"]["issue_counts"])
    elif "dq" in m:
        out.update(m["dq"]["issue_counts"])
    return out


def _report_lines(cfg: UsMarketConfig, lake: Lake, store: UsJobStore) -> list[str]:
    by_ds: dict[str, list[Any]] = defaultdict(list)
    for t in store.tasks():
        by_ds[t.dataset].append(t)
    out: list[str] = [
        "# US starter dataset: coverage and data quality",
        "",
        f"Generated {_stamp()} by `scripts/us_backfill.py report` from `lake/us/jobs/us_jobs.sqlite` and the",
        f"manifests (counts and dates only; no prices). Config `{cfg.version}`; DQ rules in",
        "`src/project100c/data/us/dq.py`. Lake: `lake/us/` (gitignored).",
        "",
        "Sources (no key): Yahoo Finance chart endpoint (unofficial; personal research use under Yahoo's terms,",
        "no redistribution), Cboe index-history CSVs, and the Open Knowledge S&P 500 constituents package.",
        "",
        "**Survivorship bias.** The symbol list is the S&P 500 *as of the snapshot date*. Companies that left the",
        "index (acquired, bankrupt, demoted) are missing, and Yahoo has no data for most delisted tickers anyway.",
        "A cross-sectional backtest over this list overstates returns and understates drawdowns. A point-in-time",
        "membership history needs a licensed source (CRSP, Norgate, Sharadar, ...) or a reconstruction from S&P",
        "Dow Jones Indices' change announcements. Each symbol's history also starts at its own listing, long",
        "before it joined the index: membership dates are not applied.",
        "",
    ]
    cons = sorted(by_ds.get(DS_CONS, []), key=lambda t: t.period_id)
    if cons:
        out += ["## Constituents", "", f"Snapshot {cons[-1].period_id}: {cons[-1].rows} members.", ""]
    daily = by_ds.get(DS_DAILY, [])
    if daily:
        st = Counter(t.status.value for t in daily)
        dq = Counter(t.dq_status for t in daily if t.status is Status.DONE)
        out += [
            "## Daily bars (full history per symbol, one part per calendar year)",
            "",
            f"Symbols: {len(daily)}; status {dict(st)}; worst-year DQ per symbol {dict(dq)};"
            f" rows {sum(t.rows or 0 for t in daily):,}.",
            "",
        ]
        agg: Counter[str] = Counter()
        firsts: list[str] = []
        rows_tbl: list[str] = []
        for t in sorted(daily, key=lambda t: t.symbol):
            if t.status is not Status.DONE or not t.manifest_path:
                rows_tbl.append(f"| {t.symbol} | - | - | - | {t.status.value} | - | - | {(t.last_error or '')[:60]} |")
                continue
            m = lake.read_manifest(t.manifest_path)
            q = sum(1 for y in m["years"].values() if y["zone"] == "quarantine")
            miss = sum(int(y["dq"]["stats"].get("missing_vs_reference", 0)) for y in m["years"].values())
            ic = _issue_counts(lake, t.manifest_path)
            agg.update(ic)
            firsts.append(m["first_session"])
            if t.dq_status != "PASS" or miss:
                issues = ", ".join(f"{k} x{v}" for k, v in sorted(ic.items()) if not k.startswith("INFO"))
                rows_tbl.append(
                    f"| {t.symbol} | {t.rows:,} | {m['first_session']} | {m['last_session']} | {t.dq_status} | {q} |"
                    f" {miss} | {issues[:110]} |"
                )
        if firsts:
            fd = sorted(firsts)
            out += [f"First session: earliest {fd[0]}, median {fd[len(fd) // 2]}, latest {fd[-1]}.", ""]
        out += ["Issue counts over all symbol-years:", ""] + [f"- {k}: {v:,}" for k, v in sorted(agg.items())] + [""]
        if rows_tbl:
            out += [
                "Symbols with a WARN/BLOCKED year or dates missing versus SPY:",
                "",
                "| symbol | rows | first | last | DQ (worst year) | quarantined years | missing vs SPY | issues |",
                "|---|---:|---|---|---|---:|---:|---|",
                *rows_tbl,
                "",
            ]
    cb = by_ds.get(DS_CBOE, [])
    if cb:
        out += [
            "## Cboe index history",
            "",
            "| series | years | rows | DQ (worst year) | issues |",
            "|---|---|---:|---|---|",
        ]
        for t in sorted(cb, key=lambda t: t.symbol):
            span = ""
            if t.manifest_path:
                ys = lake.read_manifest(t.manifest_path)["years"]
                qy = [str(y) for y, v in ys.items() if v["zone"] == "quarantine"]
                span = f"{min(ys)}..{max(ys)}" + (f"; quarantined years {', '.join(qy)}" if qy else "")
            icb = dict(_issue_counts(lake, t.manifest_path))
            out.append(f"| {t.symbol} | {span} | {t.rows or 0:,} | {t.dq_status} | {icb} |")
        out.append("")
    mins = by_ds.get(DS_1M, [])
    if mins:
        st = Counter(t.status.value for t in mins)
        dq = Counter(t.dq_status for t in mins if t.status is Status.DONE)
        sessions = sorted({t.period_id for t in mins})
        agg = Counter()
        for t in mins:
            agg.update(_issue_counts(lake, t.manifest_path))
        out += [
            "## 1-minute bars (Yahoo keeps only the last 30 days; each run extends the archive)",
            "",
            f"Symbols: {len({t.symbol for t in mins})}; sessions {sessions[0]}..{sessions[-1]} ({len(sessions)});"
            f" symbol-sessions {len(mins)}: status {dict(st)}; DQ {dict(dq)}; rows {sum(t.rows or 0 for t in mins):,}.",
            "",
            "Issue counts over all symbol-sessions:",
            "",
            *[f"- {k}: {v:,}" for k, v in sorted(agg.items())],
            "",
        ]
    return out


def cmd_report(a: argparse.Namespace) -> int:
    cfg, lake, store, _dl = _ctx(a)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(_report_lines(cfg, lake, store)) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lake", default=str(REPO / "lake" / "us"))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--steps", default="constituents,daily,cboe,minute")
    r.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: yesterday in New York)")
    r.add_argument("--minute-scope", choices=("etfs", "all"), default="all")
    r.add_argument("--max-symbols", type=int, default=None)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    rp = sub.add_parser("report")
    rp.add_argument("--out", default=str(DEFAULT_REPORT))
    rp.set_defaults(fn=cmd_report)
    al = sub.add_parser("alpaca-minute")
    al.add_argument("--symbols", default="SPY,QQQ")
    al.add_argument("--days", type=int, default=7)
    al.add_argument("--feed", choices=("sip", "iex"), default="sip")
    al.set_defaults(fn=cmd_alpaca_minute)
    a = p.parse_args(argv)
    try:
        return int(a.fn(a))
    except Project100CError as e:
        print(f"{_stamp()} ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

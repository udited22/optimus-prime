#!/usr/bin/env python3
"""Dhan Data API historical downloader CLI (backlog D-06, OD-011). DATA ONLY.

  plan        print the chunk plan and time estimate for a job (no token, no network)
  instruments fetch the PUBLIC instrument list into the lake (no token)
  run         create/resume a job and download (needs DHAN_ACCESS_TOKEN in the environment)
  status      chunk counts for a job
  verify      re-hash every committed part of a job

DHAN_CLIENT_ID is read from the environment, or from the gitignored configs/local/dhan.env if unset.
The access token is read ONLY from the environment variable DHAN_ACCESS_TOKEN and is never printed or stored.

Examples:
  scripts/dhan_download.py plan rolling --from 2026-08-03 --to 2026-09-02
  DHAN_ACCESS_TOKEN=... scripts/dhan_download.py run rolling --from 2026-08-03 --to 2026-09-02
  DHAN_ACCESS_TOKEN=... scripts/dhan_download.py run index --from 2026-07-01 --to 2026-09-30
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.calendar import TradingCalendar, load_holiday_book  # noqa: E402
from project100c.data.dhan import (  # noqa: E402
    CandleJobSpec,
    DhanCredentials,
    DhanDataClient,
    DhanDownloader,
    IngestContext,
    JobStore,
    RollingOptionJobSpec,
    download_instrument_list,
    estimate,
    load_dhan_config,
    plan_chunks,
    verify_job,
)
from project100c.data.dhan.credentials import LOCAL_ENV_FILE, merged_env  # noqa: E402
from project100c.data.dhan.jobs import job_id_for  # noqa: E402
from project100c.data.http import UrllibTransport  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.data.ratelimit import RateLimiter  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.errors import MissingCredentialError, Project100CError  # noqa: E402
from project100c.sessions import SessionCalendar, load_exchange_sessions, load_trading_windows  # noqa: E402

CONFIGS = REPO / "configs"


def _now() -> datetime:
    return datetime.now(UTC)


def _spec(a: argparse.Namespace) -> RollingOptionJobSpec | CandleJobSpec:
    f, t = date.fromisoformat(a.from_date), date.fromisoformat(a.to_date)
    if a.dataset == "rolling":
        offs = tuple(range(-a.atm_range, a.atm_range + 1))
        return RollingOptionJobSpec(
            from_date=f, to_date=t, expiry_flag=a.expiry_flag, expiry_codes=(a.expiry_code,), strike_offsets=offs
        )
    if a.dataset == "index":
        return CandleJobSpec(
            label="NIFTY-INDEX",
            security_id="13",
            exchange_segment="IDX_I",
            instrument="INDEX",
            from_date=f,
            to_date=t,
            window_days=90,
        )
    if not a.security_id:
        raise SystemExit("futures needs --security-id (see the 'instruments' command output)")
    return CandleJobSpec(
        label=f"NIFTY-FUT-{a.security_id}",
        security_id=a.security_id,
        exchange_segment="NSE_FNO",
        instrument="FUTIDX",
        oi=True,
        from_date=f,
        to_date=t,
        window_days=90,
    )


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["plan", "instruments", "run", "status", "verify"])
    p.add_argument("dataset", nargs="?", choices=["rolling", "index", "futures"], default="rolling")
    p.add_argument("--from", dest="from_date", default="2026-08-03")
    p.add_argument("--to", dest="to_date", default="2026-09-02", help="exclusive")
    p.add_argument("--expiry-flag", choices=["WEEK", "MONTH"], default="WEEK")
    p.add_argument("--expiry-code", type=int, default=1)
    p.add_argument("--atm-range", type=int, default=10)
    p.add_argument("--security-id")
    p.add_argument("--lake", type=Path, default=REPO / "lake")
    p.add_argument("--max-chunks", type=int)
    a = p.parse_args(argv)
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    lake = Lake(a.lake)
    try:
        if a.command == "instruments":
            snap = download_instrument_list(
                transport=UrllibTransport(frozenset(cfg.allowed_hosts)), config=cfg, lake=lake, wall_clock=_now
            )
            print(
                f"instrument list: {snap.report.rows_total} rows, {snap.report.rows_used} NIFTY F&O -> {snap.part.path}"
            )
            for sid, exp in [(c.exchange_token, c.expiry) for c in snap.master.contracts if c.kind.value == "FUTURE"]:
                print(f"  NIFTY future expiry {exp}: security id {sid}")
            return 0
        spec = _spec(a)
        chunks = plan_chunks(spec, cfg)
        if a.command == "plan":
            est = estimate(chunks, cfg)
            print(
                f"job {job_id_for(spec)}: {est.chunks} requests; >= {est.min_minutes_at_rate:.1f} min at "
                f"{cfg.requests_per_second}/s; {est.days_at_daily_budget} day(s) of the "
                f"{cfg.requests_per_day}/day budget"
            )
            return 0
        store = JobStore(a.lake / "jobs" / "dhan_jobs.sqlite", wall_clock=_now)
        jid = store.ensure_job(spec, chunks)
        if a.command == "status":
            print(jid, store.counts(jid))
            return 0
        if a.command == "verify":
            problems = verify_job(lake, store, jid)
            print(jid, "OK" if not problems else "\n".join(problems))
            return 0 if not problems else 1
        full_env, envfile = merged_env(env, REPO / LOCAL_ENV_FILE)
        if envfile.ignored_keys:
            print(
                f"note: ignored keys in {LOCAL_ENV_FILE}: {list(envfile.ignored_keys)} (the token must be an env var)"
            )
        creds = DhanCredentials.from_env(full_env)  # MissingCredentialError before anything is sent
        limiter = RateLimiter(
            per_second=cfg.requests_per_second,
            per_day=cfg.requests_per_day,
            clock=time.monotonic,
            sleep=time.sleep,
            wall_clock=_now,
            quota=store,
        )
        client = DhanDataClient(
            credentials=creds,
            config=cfg,
            transport=UrllibTransport(frozenset(cfg.allowed_hosts)),
            limiter=limiter,
            wall_clock=_now,
        )
        print("preflight:", dict(client.profile()))
        ctx = IngestContext(
            TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml")),
            SessionCalendar(
                load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
                load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
            ),
            load_thresholds(CONFIGS / "dq" / "thresholds.toml"),
            cfg.nifty_strike_step,
        )
        dl = DhanDownloader(client=client, config=cfg, lake=lake, store=store, ctx=ctx)
        s = dl.run(jid, max_chunks=a.max_chunks)
        print(
            f"job {jid}: done={s.done} no_data={s.no_data} quarantined={s.quarantined} rows={s.rows} "
            f"requests={s.requests}; totals {store.counts(jid)}"
        )
        return 0
    except MissingCredentialError as e:
        print(f"ERROR (no download happened): {e}", file=sys.stderr)
        return 2
    except Project100CError as e:
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))

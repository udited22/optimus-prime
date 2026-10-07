#!/usr/bin/env python3
"""Checkpointed, resumable Dhan backfill (D-06, OD-011). DATA ONLY; the token comes only from DHAN_ACCESS_TOKEN.

  plan     print the ordered job list, request counts and a time estimate (no token, no network)
  run      ensure every job exists in the job store, preflight (profile + one tiny data call), then start
           --workers worker processes that claim jobs in plan order (most recent 12 months first)
  status   per tier/series chunk counts, rows, quarantined chunks, and the ETA from the recent request rate
  reingest re-run ingest + DQ from the stored raw bytes for quarantined chunks (after a DQ-rule fix; no network)

Checkpoint = the SQLite job store (lake/jobs/dhan_jobs.sqlite): a chunk is DONE only after its raw bytes, Parquet
part and manifest are on disk. Killing the run, or the token expiring (DH-901/807 -> the worker stops), leaves
the remaining chunks PENDING; ``run`` again with a fresh token resumes. Re-running a finished plan sends nothing.

Pacing: the vendor allows 5 data requests/s and 100,000/day. Each worker gets floor(requests_per_second/workers)
requests/s (at least 1, so --workers must be <= requests_per_second), and the daily counter is shared in SQLite.
Logs go to logs/dhan_backfill/<run id>/ (gitignored). Nothing here prints or stores the token or client id.

  scripts/dhan_backfill.py plan
  DHAN_ACCESS_TOKEN=... scripts/dhan_backfill.py run --workers 4 &
  scripts/dhan_backfill.py status

--market sensex (3-Oct-2026) runs the same pipeline for BSE: the SENSEX index (IDX_I 51) and SENSEX weekly options
(BSE_FNO, nearest ATM+-10 and next ATM+-3, from the 15-May-2023 relaunch), against the BSE calendar
(configs/calendar/bse_fo_holidays.toml) and a 100-point strike grid. Same job store, same DQ and quarantine rules:

  nohup scripts/dhan_backfill.py run --market sensex --workers 4 > logs/dhan_sensex.log 2>&1 &
  scripts/dhan_backfill.py reingest --market sensex      # OD-015 re-check with the SENSEX index as reference
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.calendar import TradingCalendar, load_holiday_book  # noqa: E402
from project100c.data.dhan import (  # noqa: E402
    DhanCredentials,
    DhanDataClient,
    DhanDownloader,
    IngestContext,
    JobStore,
    load_dhan_config,
    plan_chunks,
)
from project100c.data.dhan.backfill import (  # noqa: E402
    ACTIVE_FUTURES,
    DEFAULT_ANCHOR,
    INDEX_LABEL,
    SENSEX_INDEX_LABEL,
    BackfillItem,
    plan_backfill,
    plan_sensex_backfill,
)
from project100c.data.dhan.config import DhanConfig  # noqa: E402
from project100c.data.dhan.dq import DQStatus  # noqa: E402
from project100c.data.dhan.jobs import (  # noqa: E402  # noqa: E402
    CandleJobSpec,
    ChunkStatus,
    job_id_for,
    reingest_blocked,
)
from project100c.data.http import UrllibTransport  # noqa: E402
from project100c.data.lake import Lake  # noqa: E402
from project100c.data.ratelimit import RateLimiter  # noqa: E402
from project100c.dq.checks import load_thresholds  # noqa: E402
from project100c.errors import (  # noqa: E402
    DownloadIncompleteError,
    MissingCredentialError,
    Project100CError,
    QuotaExhaustedError,
    VendorAuthError,
    VendorRateLimitError,
    VendorSubscriptionError,
)
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows  # noqa: E402

CONFIGS = REPO / "configs"
CLAIMS = """CREATE TABLE IF NOT EXISTS backfill_claims(run_id TEXT NOT NULL, job_id TEXT NOT NULL,
  worker INTEGER NOT NULL, claimed_utc TEXT NOT NULL, finished_utc TEXT, outcome TEXT, PRIMARY KEY (run_id, job_id))"""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


SENSEX_DEFAULT_ANCHOR = "2026-10-03"  # exclusive; fixed so the SENSEX job ids are stable across re-runs


def _anchor(a: argparse.Namespace) -> str:
    return str(a.anchor or (SENSEX_DEFAULT_ANCHOR if a.market == "sensex" else DEFAULT_ANCHOR))


def _plan(a: argparse.Namespace) -> list[BackfillItem]:
    if a.market == "sensex":
        return plan_sensex_backfill(anchor=date.fromisoformat(_anchor(a)), years=a.years)
    return plan_backfill(anchor=date.fromisoformat(_anchor(a)), years=a.years, futures=ACTIVE_FUTURES)


def _db(lake: Path) -> Path:
    return lake / "jobs" / "dhan_jobs.sqlite"


def _client(cfg: DhanConfig, store: JobStore, env: Mapping[str, str], per_second: int) -> DhanDataClient:
    creds = DhanCredentials.from_env({k: v for k, v in env.items() if k == "DHAN_ACCESS_TOKEN"})
    limiter = RateLimiter(
        per_second=per_second,
        per_day=cfg.requests_per_day,
        clock=time.monotonic,
        sleep=time.sleep,
        wall_clock=_now,
        quota=store,
    )
    return DhanDataClient(
        credentials=creds,
        config=cfg,
        transport=UrllibTransport(frozenset(cfg.allowed_hosts)),
        limiter=limiter,
        wall_clock=_now,
    )


def _ctx(cfg: DhanConfig, market: str = "nifty") -> IngestContext:
    """NIFTY: NSE F&O calendar, 50-point strikes. SENSEX: BSE F&O calendar, 100-point strikes. Both use the same
    exchange-session config: BSE equity-derivatives hours equal NSE's (09:15-15:30, 15:40 from 3-Aug-2026 per the
    BSE notice of 12-Jun-2026; see configs/sessions/README.md)."""
    book = "bse_fo_holidays.toml" if market == "sensex" else "nse_fo_holidays.toml"
    return IngestContext(
        TradingCalendar(load_holiday_book(CONFIGS / "calendar" / book)),
        SessionCalendar(
            load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
            load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
        ),
        load_thresholds(CONFIGS / "dq" / "thresholds.toml"),
        cfg.sensex_strike_step if market == "sensex" else cfg.nifty_strike_step,
    )


def cmd_plan(a: argparse.Namespace, cfg: DhanConfig) -> int:
    items = _plan(a)
    by: dict[tuple[str, str], list[int]] = {}
    for it in items:
        v = by.setdefault((it.tier, it.series), [0, 0])
        v[0] += 1
        v[1] += len(plan_chunks(it.spec, cfg))
    total = sum(v[1] for v in by.values())
    for (tier, series), (jobs, reqs) in by.items():
        print(f"tier {tier} {series:13s} jobs={jobs:3d} requests={reqs}")
    print(
        f"total requests={total}; >= {total / cfg.requests_per_second / 60:.0f} min at the rate limit "
        f"(observed latency 1-10 s/request makes it latency-bound: run several workers)"
    )
    return 0


def cmd_status(a: argparse.Namespace, cfg: DhanConfig) -> int:
    path = _db(a.lake)
    if not path.exists():
        print("no job store yet")
        return 0
    db = sqlite3.connect(path, timeout=60)
    items = _plan(a)
    agg: dict[tuple[str, str], dict[str, int]] = {}
    for it in items:
        jid = job_id_for(it.spec)
        d = agg.setdefault((it.tier, it.series), {})
        rows = db.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(rows),0), SUM(dq_status='BLOCKED'), SUM(dq_status='WARN') "
            "FROM chunks WHERE job_id=? GROUP BY status",
            (jid,),
        ).fetchall()
        if not rows:
            d["NOT_CREATED"] = d.get("NOT_CREATED", 0) + len(plan_chunks(it.spec, cfg))
        for st, n, r, q, w in rows:
            d[st] = d.get(st, 0) + int(n)
            d["rows"] = d.get("rows", 0) + int(r)
            d["quarantined"] = d.get("quarantined", 0) + int(q or 0)
            d["dq_warn"] = d.get("dq_warn", 0) + int(w or 0)
    remaining = 0
    for (tier, series), d in agg.items():
        remaining += d.get("PENDING", 0) + d.get("FAILED", 0) + d.get("NOT_CREATED", 0)
        print(f"tier {tier} {series:13s} " + " ".join(f"{k}={v}" for k, v in sorted(d.items())))
    since = datetime.fromtimestamp(time.time() - 1800, UTC).isoformat()
    recent = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE status IN ('DONE','NO_DATA') AND updated_utc >= ?", (since,)
    ).fetchone()[0]
    day = datetime.now(IST).date().isoformat()
    used = db.execute("SELECT requests FROM quota WHERE day=?", (day,)).fetchone()
    print(
        f"remaining chunks={remaining}; finished in the last 30 min={recent}; requests today ({day} IST)="
        f"{0 if used is None else used[0]}/{cfg.requests_per_day}"
    )
    if recent:
        print(f"ETA at the last-30-min rate: {remaining / (recent / 30):.0f} min")
    runs = (
        db.execute(
            "SELECT run_id, COUNT(*), SUM(finished_utc IS NOT NULL) FROM backfill_claims "
            "GROUP BY run_id ORDER BY run_id"
        ).fetchall()
        if db.execute("SELECT name FROM sqlite_master WHERE name='backfill_claims'").fetchone()
        else []
    )
    for rid, n, f in runs[-3:]:
        print(f"run {rid}: claimed jobs={n} finished={f}")
    return 0


def _index_minutes(
    lake: Path, store: JobStore, items: Sequence[BackfillItem], label: str = INDEX_LABEL
) -> dict[date, frozenset[datetime]]:
    """Index bar starts by IST date, from the non-quarantined index parts (OD-015 reference)."""
    by_day: dict[date, set[datetime]] = {}
    for it in items:
        if not (isinstance(it.spec, CandleJobSpec) and it.spec.label == label):
            continue
        for r in store.chunks(job_id_for(it.spec), frozenset({ChunkStatus.DONE})):
            if r.dq_status == DQStatus.BLOCKED.value or not r.part_path:
                continue
            for t in pq.read_table(lake / r.part_path, columns=["ts"]).column("ts").to_pylist():
                by_day.setdefault(t.astimezone(IST).date(), set()).add(t)
    return {d: frozenset(v) for d, v in by_day.items()}


def cmd_reingest(a: argparse.Namespace, cfg: DhanConfig) -> int:
    """Re-check quarantined chunks from their stored raw bytes (no request). Candles (index, VIX, futures) go
    first; then options and futures get the NIFTY index minutes as the OD-015 opening-gap reference."""
    store = JobStore(_db(a.lake), wall_clock=_now)
    lake, ctx = Lake(a.lake), _ctx(cfg, a.market)
    items = [it for it in _plan(a) if store.counts(job_id_for(it.spec))]

    def phase(name: str, sel: Sequence[BackfillItem], c: IngestContext) -> None:
        changed: dict[str, int] = {}
        for it in sel:
            for _key, old, new in reingest_blocked(
                lake=lake, store=store, ctx=c, config=cfg, job_id=job_id_for(it.spec)
            ):
                changed[f"{old}->{new}"] = changed.get(f"{old}->{new}", 0) + 1
        print(f"{_stamp()} re-ingested quarantined {name} chunks: {changed or 'none'}", flush=True)

    index_like = [it for it in items if isinstance(it.spec, CandleJobSpec) and it.series in ("index", "vix")]
    phase("index/VIX", index_like, ctx)
    label = SENSEX_INDEX_LABEL if a.market == "sensex" else INDEX_LABEL
    ref = _index_minutes(a.lake, store, items, label)
    print(f"{_stamp()} OD-015 reference: {label} minutes on {len(ref)} day(s)", flush=True)
    ctx_ref = dataclasses.replace(ctx, reference_minutes=lambda d: ref.get(d, frozenset()))
    phase("futures/options", [it for it in items if it not in index_like], ctx_ref)
    return 0


def cmd_run(a: argparse.Namespace, cfg: DhanConfig, env: Mapping[str, str]) -> int:
    if a.workers < 1 or a.workers > cfg.requests_per_second:
        raise SystemExit(f"--workers must be 1..{cfg.requests_per_second} (requests_per_second)")
    items = _plan(a)
    store = JobStore(_db(a.lake), wall_clock=_now)
    for it in items:
        store.ensure_job(it.spec, plan_chunks(it.spec, cfg))
    client = _client(cfg, store, env, 1)
    print(f"{_stamp()} preflight profile: {dict(client.profile())}", flush=True)
    bars = client.data_probe(datetime.now(IST).date())
    print(f"{_stamp()} preflight data probe: OK ({bars} NIFTY index bars in the last 5 days)", flush=True)
    store.close()
    codes: list[int] = []
    for rnd in range(1, a.rounds + 1):  # later rounds retry FAILED chunks (timeouts, 5xx)
        run_id = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
        logdir = REPO / "logs" / "dhan_backfill" / run_id
        logdir.mkdir(parents=True, exist_ok=True)
        print(
            f"{_stamp()} round {rnd} run {run_id}: {len(items)} jobs, {a.workers} workers, logs in {logdir}", flush=True
        )
        procs = []
        for w in range(a.workers):
            log = (logdir / f"worker-{w}.log").open("a")
            cmd = [
                sys.executable,
                __file__,
                "_worker",
                "--run-id",
                run_id,
                "--worker",
                str(w),
                "--workers",
                str(a.workers),
                "--lake",
                str(a.lake),
                "--anchor",
                _anchor(a),
                "--market",
                a.market,
                "--years",
                str(a.years),
            ]
            procs.append(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ)))
        codes = [p.wait() for p in procs]
        print(f"{_stamp()} run {run_id} finished; worker exit codes {codes}", flush=True)
        if any(c != 0 for c in codes):
            break  # STOP (token expired, quota, persistent rate limit) or a crash: resume later with `run`
        db = sqlite3.connect(_db(a.lake), timeout=60)
        jids = [job_id_for(it.spec) for it in items]
        failed = sum(
            db.execute("SELECT COUNT(*) FROM chunks WHERE job_id=? AND status='FAILED'", (j,)).fetchone()[0]
            for j in jids
        )
        db.close()
        if failed == 0:
            break
        print(f"{_stamp()} {failed} FAILED chunk(s) left; starting another round", flush=True)
    cmd_status(a, cfg)
    return max(codes)


RETRY_AFTER_SECONDS = 300  # an in-run retry of a job's FAILED chunks waits this long after its first pass


def _claim(db: sqlite3.Connection, run_id: str, worker: int, job_sequence: Sequence[str]) -> tuple[str, str] | None:
    """Claim the next job in plan order; returns (claim id, job id).

    A job is claimed once per run. If its first pass finished more than RETRY_AFTER_SECONDS ago and left FAILED
    chunks (gateway 504s), it is claimed once more under ``<run>#retry``. Plan order is newest first, so recent
    failures are retried before older history is started, instead of waiting for the end-of-run round.
    """
    retry_id = f"{run_id}#retry"
    db.execute("BEGIN IMMEDIATE")
    try:
        first = {
            r[0]: r[1] for r in db.execute("SELECT job_id, finished_utc FROM backfill_claims WHERE run_id=?", (run_id,))
        }
        retried = {r[0] for r in db.execute("SELECT job_id FROM backfill_claims WHERE run_id=?", (retry_id,))}
        cutoff = datetime.fromtimestamp(time.time() - RETRY_AFTER_SECONDS, UTC).isoformat()
        for jid in job_sequence:
            if jid in first:
                done_at = first[jid]
                if jid in retried or done_at is None or done_at > cutoff:
                    continue
                n = db.execute("SELECT COUNT(*) FROM chunks WHERE job_id=? AND status='FAILED'", (jid,)).fetchone()[0]
                if n == 0:
                    continue
                claim = retry_id
            else:
                left = db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE job_id=? AND status IN ('PENDING','FAILED')", (jid,)
                ).fetchone()[0]
                if left == 0:
                    continue
                claim = run_id
            db.execute(
                "INSERT INTO backfill_claims VALUES(?,?,?,?,NULL,NULL)", (claim, jid, worker, _now().isoformat())
            )
            db.execute("COMMIT")
            return claim, str(jid)
        db.execute("COMMIT")
        return None
    except BaseException:
        db.execute("ROLLBACK")
        raise


def cmd_worker(a: argparse.Namespace, cfg: DhanConfig, env: Mapping[str, str]) -> int:
    items = _plan(a)
    meta = {job_id_for(it.spec): it for it in items}
    job_sequence = list(meta)
    store = JobStore(_db(a.lake), wall_clock=_now)
    db = sqlite3.connect(_db(a.lake), isolation_level=None, timeout=60)
    db.execute(CLAIMS)
    client = _client(cfg, store, env, max(1, cfg.requests_per_second // a.workers))
    dl = DhanDownloader(client=client, config=cfg, lake=Lake(a.lake), store=store, ctx=_ctx(cfg, a.market))
    print(f"{_stamp()} worker {a.worker} started (run {a.run_id})", flush=True)
    while True:
        got = _claim(db, a.run_id, a.worker, job_sequence)
        if got is None:
            print(f"{_stamp()} worker {a.worker}: nothing left to claim", flush=True)
            return 0
        claim_id, jid = got
        it = meta[jid]
        retry = " retry" if claim_id != a.run_id else ""
        what = f"{it.tier}/{it.series} {it.spec.from_date}..{it.spec.to_date} ({jid}{retry})"
        t0 = time.monotonic()
        outcome = "OK"
        try:
            s = dl.run(jid)
            print(
                f"{_stamp()} {what}: done={s.done} no_data={s.no_data} quarantined={s.quarantined} rows={s.rows} "
                f"requests={s.requests} in {time.monotonic() - t0:.0f}s",
                flush=True,
            )
        except DownloadIncompleteError as e:
            outcome = "INCOMPLETE"
            print(f"{_stamp()} {what}: INCOMPLETE {e}", flush=True)
        except (
            VendorAuthError,
            VendorSubscriptionError,
            MissingCredentialError,
            QuotaExhaustedError,
            VendorRateLimitError,
        ) as e:
            db.execute(
                "UPDATE backfill_claims SET finished_utc=?, outcome=? WHERE run_id=? AND job_id=?",
                (_now().isoformat(), f"STOP:{type(e).__name__}", claim_id, jid),
            )
            print(
                f"{_stamp()} {what}: STOP {type(e).__name__}: {e}. Remaining chunks stay PENDING; re-run "
                "with a fresh token to resume.",
                flush=True,
            )
            return 3
        db.execute(
            "UPDATE backfill_claims SET finished_utc=?, outcome=? WHERE run_id=? AND job_id=?",
            (_now().isoformat(), outcome, claim_id, jid),
        )


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["plan", "run", "status", "reingest", "_worker"])
    p.add_argument("--lake", type=Path, default=REPO / "lake")
    p.add_argument(
        "--anchor",
        default=None,
        help=f"exclusive end date of the plan (default {DEFAULT_ANCHOR}; sensex {SENSEX_DEFAULT_ANCHOR})",
    )
    p.add_argument("--market", choices=["nifty", "sensex"], default="nifty")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--rounds", type=int, default=3, help="extra rounds retry FAILED chunks")
    p.add_argument("--run-id")
    p.add_argument("--worker", type=int, default=0)
    a = p.parse_args(argv)
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    try:
        if a.command == "plan":
            return cmd_plan(a, cfg)
        if a.command == "status":
            return cmd_status(a, cfg)
        if a.command == "run":
            return cmd_run(a, cfg, env)
        if a.command == "reingest":
            return cmd_reingest(a, cfg)
        return cmd_worker(a, cfg, env)
    except MissingCredentialError as e:
        print(f"ERROR (no download happened): {e}", file=sys.stderr)
        return 2
    except Project100CError as e:
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))

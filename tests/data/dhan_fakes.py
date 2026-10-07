"""Deterministic fake Dhan Data API server for tests. SYNTHETIC PRICES: shapes follow the public docs
(S31, S57); values are generated and must never be treated as market data."""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from project100c.calendar import TradingCalendar, load_holiday_book
from project100c.data.dhan import (
    DhanConfig,
    DhanCredentials,
    DhanDataClient,
    DhanDownloader,
    IngestContext,
    JobStore,
    load_dhan_config,
)
from project100c.data.dhan.credentials import ENV_ACCESS_TOKEN
from project100c.data.http import HttpRequest, HttpResponse, ScriptedTransport
from project100c.data.lake import Lake
from project100c.data.ratelimit import RateLimiter, SimClock
from project100c.dq.checks import load_thresholds
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows

REPO = Path(__file__).resolve().parents[2]
CONFIGS = REPO / "configs"
FAKE_TOKEN = "FAKE-TOKEN-for-tests-only-7f3a9c"  # not a credential: a marker we grep for to prove redaction
WALL = datetime(2026, 10, 1, 1, 30, tzinfo=IST)
STEP = Decimal(50)
TICK = Decimal("0.05")


def wall() -> datetime:
    return WALL


@lru_cache(maxsize=1)
def truth_calendar() -> TradingCalendar:
    return TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"), allow_unverified=True)


@lru_cache(maxsize=1)
def sessions() -> SessionCalendar:
    return SessionCalendar(
        load_exchange_sessions(CONFIGS / "sessions" / "exchange_sessions.toml"),
        load_trading_windows(CONFIGS / "sessions" / "trading_window.toml"),
    )


def ingest_ctx(cfg: DhanConfig) -> IngestContext:
    return IngestContext(
        TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml")),
        sessions(),
        load_thresholds(CONFIGS / "dq" / "thresholds.toml"),
        cfg.nifty_strike_step,
    )


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def _q(x: Decimal, step: Decimal = TICK) -> Decimal:
    return (x / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def _session_minutes(d: date, index: bool) -> list[datetime]:
    s = sessions().exchange
    if d < date(2021, 1, 1):  # before session-config coverage: the fake assumes 09:15-15:30 (fake only)
        t, end = datetime.combine(d, time(9, 15), IST), datetime.combine(d, time(15, 30), IST)
    else:
        sess = s.cash_for(d) if index else s.fo_for(d)
        t = datetime.combine(d, sess.normal_open, IST)
        end = datetime.combine(d, sess.normal_close, IST)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(minutes=1)
    return out


def _days(d0: date, d1_excl: date) -> list[date]:
    cal = truth_calendar()
    out, d = [], d0
    while d < d1_excl:
        covered = cal.covers(d)
        if (covered and cal.is_trading_day(d)) or (not covered and d.weekday() < 5):
            out.append(d)
        d += timedelta(days=1)
    return out


def spot_at(t: datetime) -> Decimal:
    day = t.date()
    base = Decimal(24000) + Decimal(_seed("spot", day) % 400) - 200
    m = int((t - datetime.combine(day, time(9, 15), IST)).total_seconds() // 60)
    wiggle = Decimal((_seed("w", day, m // 7) % 41) - 20) / 2
    return _q(base + Decimal(m % 60) - 30 + wiggle)


def _num(d: Decimal) -> str:
    return format(d.normalize(), "f") if d == d.to_integral_value() else format(d, "f")


def rolling_series(payload: dict[str, Any]) -> dict[str, list[str]]:
    off = 0 if payload["strike"] == "ATM" else int(payload["strike"][3:])
    call = payload["drvOptionType"] == "CALL"
    cols: dict[str, list[str]] = {
        k: [] for k in ("open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot", "timestamp")
    }
    # toDate is INCLUSIVE, as the real endpoint behaves (verified 2-Oct-2026; the docs say non-inclusive)
    d_to = date.fromisoformat(payload["toDate"]) + timedelta(days=1)
    for d in _days(date.fromisoformat(payload["fromDate"]), d_to):
        for t in _session_minutes(d, index=False):
            spot = spot_at(t)
            strike = _q(spot, STEP) + off * STEP
            intrinsic = max(Decimal(0), (spot - strike) if call else (strike - spot))
            tv = Decimal(60) + Decimal(_seed("tv", t, off, call) % 200) / 10 - abs(off) * 4
            o = _q(max(TICK * 2, intrinsic + tv))
            c = _q(max(TICK * 2, o + Decimal((_seed("c", t, off, call) % 21) - 10) / 10))
            h, lo = max(o, c) + Decimal("0.5"), max(TICK, min(o, c) - Decimal("0.5"))
            cols["open"].append(_num(o))
            cols["high"].append(_num(h))
            cols["low"].append(_num(lo))
            cols["close"].append(_num(c))
            cols["iv"].append(_num(Decimal(12) + Decimal(_seed("iv", t, off) % 500) / 100))
            cols["volume"].append(str(_seed("v", t, off) % 5000))
            cols["strike"].append(_num(strike))
            cols["oi"].append(str(100000 + _seed("oi", d, off) % 900000))
            cols["spot"].append(_num(spot))
            cols["timestamp"].append(str(int(t.timestamp())))
    return cols


def candle_series(payload: dict[str, Any]) -> dict[str, list[str]]:
    index = payload["instrument"] == "INDEX"
    d0 = date.fromisoformat(payload["fromDate"][:10])
    d1 = date.fromisoformat(payload["toDate"][:10]) + timedelta(days=1)
    cols: dict[str, list[str]] = {
        k: [] for k in ("open", "high", "low", "close", "volume", "timestamp", "open_interest")
    }
    for d in _days(d0, d1):
        for t in _session_minutes(d, index=index):
            s = spot_at(t) + (0 if index else 60)
            c = s + Decimal((_seed("cc", t) % 9) - 4) / 2
            cols["open"].append(_num(s))
            cols["high"].append(_num(max(s, c) + 1))
            cols["low"].append(_num(min(s, c) - 1))
            cols["close"].append(_num(c))
            cols["volume"].append("0" if index else str(_seed("fv", t) % 20000))
            cols["timestamp"].append(str(int(t.timestamp())))
            cols["open_interest"].append("0" if index else str(10_000_000 + _seed("foi", d) % 100000))
    return cols


def to_json(obj: Any) -> bytes:
    """Emit numbers exactly as the strings given (json.dumps would quote them)."""
    if isinstance(obj, dict):
        return b"{" + b",".join(json.dumps(k).encode() + b":" + to_json(v) for k, v in obj.items()) + b"}"
    if isinstance(obj, list) and all(isinstance(v, str) for v in obj):
        return ("[" + ",".join(obj) + "]").encode()
    if isinstance(obj, list):
        return b"[" + b",".join(to_json(v) for v in obj) + b"]"
    if obj is None:
        return b"null"
    if isinstance(obj, str) and obj and (obj[0].isdigit() or obj[0] == "-"):
        return obj.encode()
    return json.dumps(obj).encode()


@dataclass
class FakeDhan:
    """Responder: routes by URL, records every request, supports injected faults per call index."""

    clock: SimClock
    faults: dict[int, HttpResponse] = field(default_factory=dict)
    mutate: Callable[[dict[str, Any], dict[str, list[str]]], None] | None = None
    no_data_strikes: frozenset[str] = frozenset()
    calls: int = 0
    sent_at: list[float] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    def __call__(self, req: HttpRequest) -> HttpResponse:
        self.calls += 1
        self.sent_at.append(self.clock())
        self.urls.append(req.url)
        if self.calls in self.faults:
            return self.faults[self.calls]
        if req.headers.get("access-token") != FAKE_TOKEN:
            return HttpResponse(
                401, b'{"errorType":"Invalid_Authentication","errorCode":"DH-901","errorMessage":"bad token"}'
            )
        path = req.url.split("/v2/", 1)[1]
        if path == "profile":
            return HttpResponse(
                200,
                (
                    b'{"dhanClientId":"1000000001","tokenValidity":"02/10/2026 01:00",'
                    b'"dataPlan":"Active","dataValidity":"2026-10-31 00:00:00.0"}'
                ),
            )
        payload = json.loads(req.body or b"{}")
        if path == "charts/rollingoption":
            if payload["strike"] in self.no_data_strikes:
                return HttpResponse(
                    400, b'{"errorType":"Data_Error","errorCode":"DH-907","errorMessage":"No data present"}'
                )
            cols = rolling_series(payload)
            if self.mutate:
                self.mutate(payload, cols)
            side = {k: v for k, v in cols.items() if k == "timestamp" or k in payload["requiredData"]}
            key = "ce" if payload["drvOptionType"] == "CALL" else "pe"
            return HttpResponse(200, to_json({"data": {key: side, ("pe" if key == "ce" else "ce"): None}}))
        if path == "charts/intraday":
            cols = candle_series(payload)
            if self.mutate:
                self.mutate(payload, cols)
            if not payload.get("oi"):
                cols.pop("open_interest")
            return HttpResponse(200, to_json(cols))
        return HttpResponse(404, b'{"errorType":"Input_Exception","errorCode":"DH-905","errorMessage":"unknown path"}')


@dataclass
class Rig:
    cfg: DhanConfig
    clock: SimClock
    fake: FakeDhan
    transport: ScriptedTransport
    store: JobStore
    lake: Lake
    downloader: DhanDownloader
    client: DhanDataClient
    limiter: RateLimiter


def make_rig(
    tmp: Path,
    *,
    fake: FakeDhan | None = None,
    hook: Callable[[str], None] | None = None,
    per_day: int | None = None,
    env: dict[str, str] | None = None,
    cfg_update: dict[str, Any] | None = None,
) -> Rig:
    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    if cfg_update:
        cfg = cfg.model_copy(update=cfg_update)
    clock = fake.clock if fake is not None else SimClock()
    fake = fake or FakeDhan(clock)
    transport = ScriptedTransport(fake)
    store = JobStore(tmp / "jobs.sqlite", wall_clock=wall)
    limiter = RateLimiter(
        per_second=cfg.requests_per_second,
        per_day=per_day or cfg.requests_per_day,
        clock=clock,
        sleep=clock.sleep,
        wall_clock=wall,
        quota=store,
    )
    creds = DhanCredentials.from_env(env if env is not None else {ENV_ACCESS_TOKEN: FAKE_TOKEN})
    client = DhanDataClient(credentials=creds, config=cfg, transport=transport, limiter=limiter, wall_clock=wall)
    lake = Lake(tmp / "lake")
    dl = DhanDownloader(client=client, config=cfg, lake=lake, store=store, ctx=ingest_ctx(cfg), before_commit=hook)
    return Rig(cfg, clock, fake, transport, store, lake, dl, client, limiter)

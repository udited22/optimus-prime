"""US starter downloader (Yahoo chart, Cboe, S&P 500 list) and the ready Alpaca client: offline tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest

from project100c.data.http import HttpRequest, HttpResponse, ScriptedTransport
from project100c.data.lake import Lake
from project100c.data.public_http import PublicGetter, RetryPolicy
from project100c.data.ratelimit import MemoryQuotaStore, RateLimiter
from project100c.data.us import (
    DS_ALPACA_1M,
    AlpacaBarsClient,
    AlpacaCredentials,
    Status,
    UsDownloader,
    UsJobStore,
    bars_url,
    chart_url,
    check_minute,
    f32_decimal,
    lake_symbol,
    load_nyse_sessions,
    load_us_config,
    minute_windows,
    parse_cboe_csv,
    parse_chart,
    parse_constituents,
    vendor_symbol,
)
from project100c.dq.checks import DQCode
from project100c.errors import CalendarCoverageError, MissingCredentialError, VendorAuthError, VendorResponseError

REPO = Path(__file__).resolve().parents[2]
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)  # Saturday 12:00 New York
CFG = load_us_config(REPO / "configs" / "data" / "us_market.toml")
NYSE = load_nyse_sessions(REPO / "configs" / "calendar" / "nyse_sessions.toml")
POLICY = RetryPolicy(server_max_attempts=2, rate_limit_max_attempts=2, backoff_base_s=1, backoff_cap_s=2, timeout_s=5)


def _f32(x: float) -> float:
    import struct

    return float(struct.unpack("<f", struct.pack("<f", x))[0])


def chart(ts: list[int], *, price: float = 100.25, daily: bool = True, extra: dict[str, Any] | None = None) -> bytes:
    n = len(ts)
    q = {
        "open": [_f32(price)] * n,
        "high": [_f32(price + 1)] * n,
        "low": [_f32(price - 1)] * n,
        "close": [_f32(price + 0.5)] * n,
        "volume": [1000] * n,
    }
    ind: dict[str, Any] = {"quote": [q]}
    if daily:
        ind["adjclose"] = [{"adjclose": [price + 0.4] * n}]
    res: dict[str, Any] = {
        "meta": {"exchangeTimezoneName": "America/New_York", "instrumentType": "EQUITY"},
        "timestamp": ts,
        "indicators": ind,
    }
    res |= extra or {}
    return json.dumps({"chart": {"result": [res], "error": None}}).encode()


def session_ts(days: list[date]) -> list[int]:
    return [int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY).timestamp()) for d in days]


def weekdays(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


def minutes(d: date, n: int = 390, skip: tuple[int, ...] = ()) -> list[int]:
    t0 = int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY).timestamp())
    return [t0 + 60 * i for i in range(n) if i not in skip]


CONS = (
    "Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded\n"
    + "".join(f"S{i:03d},Co {i},Industrials,Machinery,X,2001-01-0{1 + i % 9},{1000 + i},1950\n" for i in range(450))
    + "BRK.B,Berkshire,Financials,Insurance,Omaha,2010-02-16,1067983,1839\n"
).encode()


class FakeUS:
    def __init__(self) -> None:
        self.daily: dict[str, bytes] = {}
        self.minute: Callable[[str, int, int], bytes] | None = None
        self.urls: list[str] = []

    def __call__(self, req: HttpRequest) -> HttpResponse:
        self.urls.append(req.url)
        u = urlsplit(req.url)
        if u.hostname == "raw.githubusercontent.com":
            return HttpResponse(200, CONS)
        if u.hostname == "cdn-api.cboe.com":
            if u.path.endswith("/VIX_History.csv"):
                return HttpResponse(
                    200, b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2026,17.1,18,16.5,17.5\n01/05/2026,17,17.2,16,16.4\n"
                )
            return HttpResponse(404, b"")
        q = parse_qs(u.query)
        sym = u.path.rsplit("/", 1)[1].replace("%5E", "^")
        if q["interval"] == ["1d"]:
            body = self.daily.get(sym)
            return HttpResponse(200, body) if body else HttpResponse(404, b'{"chart":{"result":null}}')
        assert self.minute is not None
        return HttpResponse(200, self.minute(sym, int(q["period1"][0]), int(q["period2"][0])))


def rig(tmp: Path, fake: FakeUS) -> tuple[UsDownloader, UsJobStore, Lake]:
    clock = [0.0]

    def _sleep(s: float) -> None:
        clock[0] += s

    lim = RateLimiter(
        per_second=100, per_day=10_000, clock=lambda: clock[0], sleep=_sleep, wall_clock=lambda: NOW,
        quota=MemoryQuotaStore(),
    )  # fmt: skip
    getter = PublicGetter(transport=ScriptedTransport(fake), limiter=lim, policy=POLICY)
    lake = Lake(tmp / "us")
    store = UsJobStore(tmp / "us" / "jobs" / "us.sqlite", wall_clock=lambda: NOW)
    dl = UsDownloader(
        config=CFG, nyse=NYSE, getter=getter, lake=lake, store=store, wall_clock=lambda: NOW,
        thresholds_version="t", max_missing_fraction=Decimal("0.01"),
    )  # fmt: skip
    return dl, store, lake


# ------------------------------------------------------------------ parsing
def test_symbols_and_urls() -> None:
    assert vendor_symbol("BRK.B") == "BRK-B" and lake_symbol("^GSPC") == "INDEX-GSPC" and lake_symbol("BF.B") == "BF.B"
    url = chart_url(CFG.yahoo_chart_base, "^GSPC", interval="1d", period1=0, period2=10)
    assert "/%5EGSPC?" in url and "events=div%2Csplit" in url and "includePrePost=false" in url
    with pytest.raises(ValueError):
        vendor_symbol("../x")


def test_f32_values_decode_to_the_quoted_decimal() -> None:
    assert f32_decimal(501.9800109863281) == Decimal("501.98")
    assert f32_decimal(_f32(0.128348)) == Decimal("0.128348")
    assert f32_decimal(0.1) == Decimal("0.1")  # a true double


def test_parse_chart_drops_all_null_rows_and_records_partial_and_actions() -> None:
    days = weekdays(date(2026, 9, 1), date(2026, 9, 4))
    ts = session_ts(days)
    doc = json.loads(chart(ts))
    q = doc["chart"]["result"][0]["indicators"]["quote"][0]
    for k in q:
        q[k][1] = None
    q["volume"][2] = None
    doc["chart"]["result"][0]["events"] = {
        "dividends": {str(ts[0]): {"amount": 0.25, "date": ts[0]}},
        "splits": {str(ts[3]): {"date": ts[3], "numerator": 4, "denominator": 1}},
    }
    p = parse_chart(json.dumps(doc).encode(), symbol="AAA", interval="1d")
    assert p.bars.num_rows == 3 and p.all_null_dates == [days[1]] and p.partial_null_dates == [days[2]]
    assert p.bars["close"][0].as_py() == Decimal("100.75")
    assert p.bars["session_date"].to_pylist() == [days[0], days[2], days[3]]
    acts = p.actions.to_pylist()
    assert [(a["kind"], a["amount"], a["numerator"]) for a in acts] == [
        ("dividend", Decimal("0.25"), None),
        ("split", None, 4),
    ]
    with pytest.raises(VendorResponseError):
        parse_chart(b'{"chart":{"result":null,"error":{"code":"Not Found"}}}', symbol="X", interval="1d")


def test_parse_cboe_and_constituents() -> None:
    t, _ = parse_cboe_csv(b"DATE,VVIX\n03/06/2006,71.73\n03/07/2006,72.1\n", series="VVIX")
    assert t["close"].to_pylist() == [Decimal("71.73"), Decimal("72.1")] and t["open"].null_count == 2
    with pytest.raises(VendorResponseError):
        parse_cboe_csv(b"WHEN,X\n", series="X")
    c = parse_constituents(CONS)
    assert c.num_rows == 451 and "BRK.B" in c["symbol"].to_pylist() and c["cik"][0].as_py() == "0000001000"
    with pytest.raises(VendorResponseError):
        parse_constituents(CONS.splitlines(keepends=True)[0] + b"AAA,x,y,z,w,2001-01-01,1,1\n")


# ------------------------------------------------------------------ calendar + DQ
def test_nyse_calendar_holidays_early_close_and_coverage() -> None:
    assert not NYSE.is_trading_day(date(2026, 9, 7))  # Labor Day
    assert NYSE.session(date(2026, 11, 27))[1].hour == 13
    with pytest.raises(CalendarCoverageError):
        NYSE.is_trading_day(date(2025, 12, 1))


def _minute_check(ts: list[int], d: date) -> set[tuple[str, str]]:
    p = parse_chart(chart(ts, daily=False), symbol="AAA", interval="1m")
    r = check_minute(
        p.bars, key="k", day=d, is_index=False, all_null_rows=0, partial_null=0, rounded=0, nyse=NYSE,
        max_missing_fraction=Decimal("0.01"), thresholds_version="t",
    )  # fmt: skip
    return {(i.severity.value, i.code.value) for i in r.report.issues}


def test_minute_dq_gaps_out_of_session_and_holiday() -> None:
    d = date(2026, 9, 29)
    assert _minute_check(minutes(d), d) == set()
    gaps = _minute_check(minutes(d, skip=tuple(range(10, 20))), d)
    assert ("WARNING", DQCode.MISSING_CANDLE.value) in gaps and ("WARNING", DQCode.COVERAGE_GAP.value) in gaps
    late = _minute_check([*minutes(d), minutes(d)[-1] + 60], d)
    assert ("WARNING", DQCode.OUT_OF_SESSION_BAR.value) in late
    hol = date(2026, 9, 7)
    assert ("BLOCKING", DQCode.NON_TRADING_DAY_BAR.value) in _minute_check(minutes(hol), hol)
    off = [*minutes(d)[:-1], minutes(d)[-1] + 7]
    assert ("BLOCKING", DQCode.MISALIGNED_BAR.value) in _minute_check(off, d)


def test_minute_windows_respect_span() -> None:
    ds = weekdays(date(2026, 9, 3), date(2026, 10, 2))
    ws = minute_windows(ds, 7)
    assert all((w[-1] - w[0]).days < 7 for w in ws) and sum(len(w) for w in ws) == len(ds)


# ------------------------------------------------------------------ downloader end to end
def test_daily_run_reference_check_year_parts_quarantine_and_resume(tmp_path: Path) -> None:
    fake = FakeUS()
    ref_days = weekdays(date(2025, 12, 1), date(2026, 1, 30))
    ref_days = [d for d in ref_days if d not in (date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19))]
    fake.daily["SPY"] = chart(session_ts(ref_days))
    fake.daily["AAA"] = chart(session_ts([d for d in ref_days if d != date(2026, 1, 13)]))
    bad = json.loads(chart(session_ts(ref_days)))
    bad["chart"]["result"][0]["indicators"]["quote"][0]["high"][-1] = 1.0  # impossible OHLC in 2026
    fake.daily["BBB"] = json.dumps(bad).encode()
    dl, store, lake = rig(tmp_path, fake)
    s = dl.daily(["SPY", "AAA", "BBB", "ZZZ"], date(2026, 10, 2))
    assert (s.done, s.not_found, s.failed) == (3, 1, 0)
    aaa = store.get("daily:AAA:2026-10-02")
    assert aaa is not None and aaa.dq_status == "WARN" and len(aaa.parts) == 2  # 2025 and 2026 parts
    m = lake.read_manifest(aaa.manifest_path or "")
    assert m["years"]["2026"]["dq"]["stats"]["missing_vs_reference"] == 1
    bbb = store.get("daily:BBB:2026-10-02")
    assert bbb is not None and bbb.dq_status == "BLOCKED"
    zones = sorted(p.split("/")[0] for p in bbb.parts)
    assert zones == ["clean", "quarantine"]  # only the bad year is quarantined
    assert pq.read_table(lake.abspath(aaa.parts[0])).schema.field("close").type.scale == 10
    n = len(fake.urls)
    again = dl.daily(["SPY", "AAA", "BBB", "ZZZ"], date(2026, 10, 2))
    assert again.skipped == 4 and len(fake.urls) == n


def test_daily_refuses_without_reference(tmp_path: Path) -> None:
    fake = FakeUS()
    fake.daily["AAA"] = chart(session_ts([date(2026, 9, 1)]))
    dl, store, _ = rig(tmp_path, fake)
    with pytest.raises(VendorResponseError, match="reference"):
        dl.daily(["AAA"], date(2026, 10, 2))  # SPY is fetched first and is 404 here
    spy = store.get("daily:SPY:2026-10-02")
    assert spy is not None and spy.status is Status.NOT_FOUND
    assert store.get("daily:AAA:2026-10-02") is None


def test_minute_run_writes_sessions_skips_holidays_and_resumes(tmp_path: Path) -> None:
    fake = FakeUS()

    def serve(sym: str, p1: int, p2: int) -> bytes:
        ts: list[int] = []
        d = datetime.fromtimestamp(p1, NY).date()
        while int(datetime(d.year, d.month, d.day, tzinfo=NY).timestamp()) < p2:
            if NYSE.is_trading_day(d) and d != date(2026, 9, 15):
                ts += minutes(d)
            d += timedelta(days=1)
        return chart(ts, daily=False)

    fake.minute = serve
    dl, store, _ = rig(tmp_path, fake)
    s = dl.minute(["SPY"])
    sessions = [d for d in weekdays(date(2026, 9, 4), date(2026, 10, 2)) if d != date(2026, 9, 7)]
    assert s.done == len(sessions) - 1 and s.no_data == 1 and s.failed == 0
    t = store.get("1m:SPY:2026-09-15")
    assert t is not None and t.status is Status.NO_DATA
    assert store.get("1m:SPY:2026-09-07") is None  # Labor Day is never requested
    assert dl.minute(["SPY"]).attempted == 0


def test_cboe_years_and_404(tmp_path: Path) -> None:
    dl, store, _ = rig(tmp_path, FakeUS())
    s = dl.cboe(date(2026, 10, 2))
    assert s.done == 1 and s.not_found == len(CFG.cboe_series) - 1
    v = store.get("cboe:VIX:2026-10-02")
    assert v is not None and v.rows == 2 and v.dq_status == "PASS"


# ------------------------------------------------------------------ Alpaca (ready, keyed)
def test_alpaca_credentials_and_url() -> None:
    with pytest.raises(MissingCredentialError, match="ALPACA_API_KEY_ID"):
        AlpacaCredentials.from_env({})
    c = AlpacaCredentials.from_env({"ALPACA_API_KEY_ID": "PKTEST", "ALPACA_API_SECRET_KEY": "sec"})
    assert "PKTEST" not in repr(c) and c.redact("x PKTEST sec") == "x <redacted> <redacted>"
    u = bars_url(
        CFG.alpaca_data_base, ["SPY"], timeframe="1Min", start=datetime(2026, 9, 1, tzinfo=UTC),
        end=datetime(2026, 9, 2, tzinfo=UTC), feed="sip",
    )  # fmt: skip
    assert u.startswith("https://data.alpaca.markets/v2/stocks/bars?") and "feed=sip" in u


def _alpaca_rig(tmp: Path, responder: Callable[[HttpRequest], HttpResponse]) -> AlpacaBarsClient:
    lim = RateLimiter(
        per_second=100, per_day=1000, clock=lambda: 0.0, sleep=lambda _s: None, wall_clock=lambda: NOW,
        quota=MemoryQuotaStore(),
    )  # fmt: skip
    return AlpacaBarsClient(
        base=CFG.alpaca_data_base, transport=ScriptedTransport(responder), limiter=lim, policy=POLICY,
        credentials=AlpacaCredentials("PKTEST", "sec"), tz=NY,
    )  # fmt: skip


def test_alpaca_paginates_and_ingests_sessions(tmp_path: Path) -> None:
    d = date(2026, 10, 1)
    t0 = datetime(2026, 10, 1, 9, 30, tzinfo=NY).astimezone(UTC)
    bars = [
        {"t": (t0 + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"), "o": 10.5, "h": 11, "l": 10, "c": 10.75,
         "v": 100, "n": 3, "vw": 10.6}
        for i in range(390)
    ]  # fmt: skip
    pages = [
        json.dumps({"bars": {"SPY": bars[:200]}, "next_page_token": "abc"}).encode(),
        json.dumps({"bars": {"SPY": bars[200:]}, "next_page_token": None}).encode(),
    ]
    seen: list[HttpRequest] = []

    def resp(req: HttpRequest) -> HttpResponse:
        seen.append(req)
        return HttpResponse(200, pages[len(seen) - 1])

    client = _alpaca_rig(tmp_path, resp)
    dl, store, _ = rig(tmp_path, FakeUS())
    s = dl.alpaca_minute(client, ["SPY"], days=[d])
    assert (s.done, s.rows) == (1, 390)
    assert "page_token=abc" in seen[1].url and seen[0].headers["APCA-API-KEY-ID"] == "PKTEST"
    t = store.get(f"{DS_ALPACA_1M}:SPY:2026-10-01")
    assert t is not None and t.dq_status == "PASS"


def test_alpaca_auth_error_is_redacted(tmp_path: Path) -> None:
    client = _alpaca_rig(tmp_path, lambda _r: HttpResponse(403, b"forbidden for PKTEST"))
    with pytest.raises(VendorAuthError) as e:
        client.bars(["SPY"], timeframe="1Day", start=datetime(2026, 9, 1, tzinfo=UTC), end=NOW)
    assert "PKTEST" not in str(e.value)

"""Binance archive downloader: listing, checksum verification, parsing, DQ, planning, resume (all offline)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project100c.data.binance import (
    ArchiveKind,
    TaskStatus,
    check_klines,
    load_universe,
    parse_klines,
    parse_metrics,
    parse_rate,
    period_of,
    plan_series,
    prefix,
    rank_snapshot,
)
from project100c.dq.checks import DQCode, DQSeverity
from project100c.errors import ChecksumMismatchError, VendorResponseError
from tests.data.binance_fakes import (
    REPO,
    FakeArchive,
    day_key,
    days,
    kline_csv,
    kline_rows,
    month_key,
    rig,
    zip_one,
)

D0 = date(2026, 9, 1)


def _codes(res: object) -> set[tuple[str, str]]:
    return {(i.severity.value, i.code.value) for i in res.report.issues}  # type: ignore[attr-defined]


def _check(rows: list[str], d: date = D0, *, micro: bool = False) -> object:
    p = parse_klines(zip_one("x.csv", kline_csv(rows, header=not micro)), "k", symbol="BTCUSDT", market="spot")
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return check_klines(
        p.table,
        p.raw_strings,
        key="k",
        period_start=start,
        period_end=start.replace(day=d.day + 1),
        expect_from=None,
        expect_to=None,
        max_missing_fraction=Decimal("0.01"),
        thresholds_version="t",
    )


# ------------------------------------------------------------------ keys
def test_prefixes_and_periods() -> None:
    assert prefix(ArchiveKind.SPOT_KLINES, "BTCUSDT", "daily") == "data/spot/daily/klines/BTCUSDT/1m/"
    assert prefix(ArchiveKind.UM_RATE, "ETHUSDT", "monthly") == "data/futures/um/monthly/fundingRate/ETHUSDT/"
    assert period_of(day_key("um", "BTCUSDT", D0)) == "2026-09-01"
    assert period_of(month_key("spot", "BTCUSDT", "2024-02")) == "2024-02"
    with pytest.raises(ValueError):
        prefix(ArchiveKind.UM_RATE, "BTCUSDT", "daily")
    with pytest.raises(ValueError):
        prefix(ArchiveKind.SPOT_KLINES, "../etc", "daily")


# ------------------------------------------------------------------ listing + checksum
def test_listing_paginates_and_skips_checksums(tmp_path: Path) -> None:
    fake = FakeArchive(page_size=2)
    for d in days(D0, 5):
        fake.add(day_key("spot", "BTCUSDT", d), kline_csv(kline_rows(d, micro=True), header=False))
    archive, *_ = rig(tmp_path, fake)
    keys = archive.list_zips(prefix(ArchiveKind.SPOT_KLINES, "BTCUSDT", "daily"))
    assert keys == sorted(day_key("spot", "BTCUSDT", d) for d in days(D0, 5))
    assert sum("amazonaws" in u for u in fake.requests) >= 3


def test_fetch_verified_checks_sha256(tmp_path: Path) -> None:
    fake = FakeArchive()
    k = day_key("um", "BTCUSDT", D0)
    fake.add(k, kline_csv(kline_rows(D0, micro=False), header=True))
    archive, *_ = rig(tmp_path, fake)
    got = archive.fetch_verified(k)
    assert got is not None and got.body == fake.files[k]
    fake.bad_sum.add(k)
    with pytest.raises(ChecksumMismatchError, match="twice"):
        archive.fetch_verified(k)
    with pytest.raises(ValueError):
        archive.fetch_verified("data/../x.zip")


def test_missing_checksum_is_an_error_not_a_pass(tmp_path: Path) -> None:
    archive, *_ = rig(tmp_path, FakeArchive())
    with pytest.raises(ChecksumMismatchError, match=r"no \.CHECKSUM"):
        archive.fetch_verified(day_key("um", "BTCUSDT", D0))


# ------------------------------------------------------------------ parse
def test_parse_detects_ms_and_us_and_header() -> None:
    ms = parse_klines(
        zip_one("a.csv", kline_csv(kline_rows(D0, micro=False), header=True)), "a", symbol="X", market="um"
    )
    us = parse_klines(
        zip_one("b.csv", kline_csv(kline_rows(D0, micro=True), header=False)), "b", symbol="X", market="spot"
    )
    assert (ms.timestamp_unit, us.timestamp_unit) == ("ms", "us")
    assert ms.table["ts"].equals(us.table["ts"])
    assert ms.table.num_rows == 1440
    assert ms.table["ts"][0].as_py() == datetime(2026, 9, 1, tzinfo=UTC)
    assert ms.table.schema.field("close").type == pa.decimal128(38, 8)
    assert ms.table["close"][0].as_py() == Decimal("100.5")


def test_parse_rejects_mixed_units_and_garbage() -> None:
    rows = kline_rows(D0, micro=False, minutes=[0]) + kline_rows(D0, micro=True, minutes=[1])
    with pytest.raises(VendorResponseError):
        parse_klines(zip_one("a.csv", kline_csv(rows, header=False)), "a", symbol="X", market="um")
    with pytest.raises(VendorResponseError):
        parse_klines(b"not a zip", "a", symbol="X", market="um")


def test_parse_rate_and_metrics_keep_empty_as_null() -> None:
    rate = (
        b"calc_time,funding_interval_hours,last_funding_rate\n1756684800000,8,0.00010000\n1756713600000,8,-0.0000125\n"
    )
    r = parse_rate(zip_one("r.csv", rate), "r", symbol="BTCUSDT")
    assert [x.as_py() for x in r.table["rate"]] == [Decimal("0.0001"), Decimal("-0.0000125")]
    met = (
        b"create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
        b"sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        b"2026-09-01 00:05:00,BTCUSDT,100.5,1000000.25,,1.5,1.2,0.9\n"
    )
    m = parse_metrics(zip_one("m.csv", met), "m", symbol="BTCUSDT")
    assert m.table["count_toptrader_long_short_ratio"][0].as_py() is None
    assert m.table["sum_open_interest"][0].as_py() == Decimal("100.5")


# ------------------------------------------------------------------ DQ
def test_clean_day_passes() -> None:
    res = _check(kline_rows(D0, micro=False))
    assert not [i for i in res.report.issues if i.severity is not DQSeverity.INFO]  # type: ignore[attr-defined]


def test_identical_duplicates_collapse_with_warning_conflicting_block() -> None:
    rows = kline_rows(D0, micro=False)
    res = _check(rows + rows[:3])
    assert ("WARNING", DQCode.DUPLICATE_BAR.value) in _codes(res)
    assert res.table.num_rows == 1440  # type: ignore[attr-defined]
    other = kline_rows(D0, micro=False, minutes=[5], price="100.1")
    res2 = _check(rows + other)
    assert ("BLOCKING", DQCode.CONFLICTING_DUPLICATE_BAR.value) in _codes(res2)


def test_bad_ohlc_and_misaligned_block() -> None:
    rows = kline_rows(D0, micro=False)
    bad = [*rows[:10], rows[10].replace(",101,99,", ",98,99,"), *rows[11:]]
    assert ("BLOCKING", DQCode.BAD_OHLC.value) in _codes(_check(bad))
    first = rows[0].split(",")
    first[0] = str(int(first[0]) + 1000)
    assert ("BLOCKING", DQCode.MISALIGNED_BAR.value) in _codes(_check([",".join(first), *rows[1:]]))


def test_gaps_warn_and_large_missing_day_flagged() -> None:
    small = _check(kline_rows(D0, micro=False, minutes=[m for m in range(1440) if m not in (100, 101)]))
    assert ("WARNING", DQCode.MISSING_CANDLE.value) in _codes(small)
    assert ("WARNING", DQCode.COVERAGE_GAP.value) not in _codes(small)  # 2/1440 is below the 1% day threshold
    big = _codes(_check(kline_rows(D0, micro=False, minutes=range(1000))))
    assert ("WARNING", DQCode.COVERAGE_GAP.value) in big
    assert not any(s == "BLOCKING" for s, _ in big)


def test_zero_volume_bar_is_a_warning() -> None:
    rows = kline_rows(D0, micro=False)
    rows[3] = rows[3].replace(",2.5,", ",0,", 1)
    assert ("WARNING", DQCode.ZERO_VOLUME_BAR.value) in _codes(_check(rows))


# ------------------------------------------------------------------ plan / run / resume
def test_plan_run_resume_and_replace_daily_by_monthly(tmp_path: Path) -> None:
    fake = FakeArchive()
    for d in days(D0, 3):
        fake.add(day_key("spot", "BTCUSDT", d), kline_csv(kline_rows(d, micro=True), header=False))
    archive, lake, store, dl = rig(tmp_path, fake)
    p = plan_series(
        archive=archive,
        store=store,
        lake=lake,
        kind=ArchiveKind.SPOT_KLINES,
        symbol="BTCUSDT",
        through=date(2026, 9, 2),
    )
    assert (p.monthly, p.daily, p.new) == (0, 2, 2)  # the 3rd is after `through`
    s = dl.run(kinds=[ArchiveKind.SPOT_KLINES])
    assert (s.done, s.failed, s.rows) == (2, 0, 2880)
    tasks = store.tasks(kinds=[ArchiveKind.SPOT_KLINES])
    assert {t.status for t in tasks} == {TaskStatus.DONE}
    part = store.part_of(tasks[0].key)
    assert part is not None and "symbol=BTCUSDT" in part and "interval=1m" in part
    assert pq.read_table(lake.abspath(part)).num_rows == 1440
    # resume: nothing left to do, no new network fetches of zips
    n = len(fake.requests)
    assert dl.run(kinds=[ArchiveKind.SPOT_KLINES]).attempted == 0
    assert len(fake.requests) == n
    # the monthly file appears: daily tasks of that month are replaced and their parts removed
    month = [line for d in days(D0, 30) for line in kline_rows(d, micro=True)]
    fake.add(month_key("spot", "BTCUSDT", "2026-09"), kline_csv(month, header=False))
    p2 = plan_series(
        archive=archive,
        store=store,
        lake=lake,
        kind=ArchiveKind.SPOT_KLINES,
        symbol="BTCUSDT",
        through=date(2026, 10, 2),
    )
    assert (p2.monthly, p2.replaced) == (1, 2)
    assert not lake.abspath(part).exists()
    s2 = dl.run(kinds=[ArchiveKind.SPOT_KLINES])
    assert (s2.done, s2.rows) == (1, 43200)


def test_checksum_failure_marks_failed_and_retries_next_run(tmp_path: Path) -> None:
    fake = FakeArchive()
    k = day_key("um", "ETHUSDT", D0)
    fake.add(k, kline_csv(kline_rows(D0, micro=False), header=True))
    fake.bad_sum.add(k)
    archive, lake, store, dl = rig(tmp_path, fake)
    plan_series(archive=archive, store=store, lake=lake, kind=ArchiveKind.UM_KLINES, symbol="ETHUSDT", through=D0)
    s = dl.run(kinds=[ArchiveKind.UM_KLINES])
    assert (s.failed, s.done) == (1, 0)
    assert store.tasks(kinds=[ArchiveKind.UM_KLINES])[0].status is TaskStatus.FAILED
    fake.bad_sum.clear()
    assert dl.run(kinds=[ArchiveKind.UM_KLINES]).done == 1


def test_blocked_file_goes_to_quarantine_with_manifest(tmp_path: Path) -> None:
    fake = FakeArchive()
    rows = kline_rows(D0, micro=False)
    rows[7] = rows[7].replace(",101,99,", ",98,99,")
    fake.add(day_key("um", "BTCUSDT", D0), kline_csv(rows, header=True))
    archive, lake, store, dl = rig(tmp_path, fake)
    plan_series(archive=archive, store=store, lake=lake, kind=ArchiveKind.UM_KLINES, symbol="BTCUSDT", through=D0)
    s = dl.run(kinds=[ArchiveKind.UM_KLINES])
    assert (s.done, s.quarantined) == (1, 1)
    part = store.part_of(store.tasks(kinds=[ArchiveKind.UM_KLINES])[0].key)
    assert part is not None and "quarantine" in part


# ------------------------------------------------------------------ universe
def test_rank_snapshot_excludes_stables_and_wrapped() -> None:
    data = [
        {"symbol": "BTC", "rank": 1, "market_cap_usd": "2000", "name": "Bitcoin"},
        {"symbol": "USDT", "rank": 3, "market_cap_usd": "150", "name": "Tether"},
        {"symbol": "ETH", "rank": 2, "market_cap_usd": "400", "name": "Ethereum"},
        {"symbol": "STETH", "rank": 4, "market_cap_usd": "30", "name": "Lido"},
        {"symbol": "SOL", "rank": 5, "market_cap_usd": "80", "name": "Solana"},
    ]
    out = rank_snapshot(json.dumps({"data": data}).encode(), top_n=2, excluded={"usdt", "steth"})
    assert [x["asset"] for x in out] == ["BTC", "ETH", "SOL"]
    with pytest.raises(VendorResponseError):
        rank_snapshot(b"{}", top_n=2, excluded=set())


def test_pinned_universe_loads() -> None:
    u = load_universe(REPO / "configs" / "data" / "crypto_universe.toml")
    syms = u.pairs()
    assert syms[0] == "BTCUSDT" and len(syms) == 11 and len(set(syms)) == 11
    assert not {"USDTUSDT", "USDCUSDT", "WBTCUSDT", "STETHUSDT"} & set(syms)


# ------------------------------------------------------------------ keyless GET seam
def test_public_getter_retries_then_succeeds_and_maps_404() -> None:
    from project100c.data.http import HttpResponse, ScriptedTransport
    from project100c.data.public_http import PublicGetter, RetryPolicy
    from project100c.data.ratelimit import MemoryQuotaStore, RateLimiter
    from project100c.errors import VendorRequestError

    slept: list[float] = []
    lim = RateLimiter(
        per_second=50,
        per_day=1000,
        clock=lambda: 0.0,
        sleep=slept.append,
        wall_clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
        quota=MemoryQuotaStore(),
    )
    script = [
        HttpResponse(503, b""),
        HttpResponse(429, b"", {"retry-after": "7"}),
        HttpResponse(200, b"ok"),
        HttpResponse(404, b""),
        HttpResponse(400, b"bad"),
    ]
    tr = ScriptedTransport(script)
    g = PublicGetter(
        transport=tr,
        limiter=lim,
        policy=RetryPolicy(
            server_max_attempts=3, rate_limit_max_attempts=3, backoff_base_s=1, backoff_cap_s=4, timeout_s=5
        ),
    )
    got = g.get("https://example.invalid/a")
    assert got is not None and got.response.body == b"ok" and got.attempts == 3
    assert any(s >= 7 for s in slept)  # Retry-After honoured
    assert g.get("https://example.invalid/b") is None
    with pytest.raises(VendorRequestError):
        g.get("https://example.invalid/c")
    assert all("Authorization" not in r.headers for r in tr.requests)

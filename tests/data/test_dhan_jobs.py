"""D-06 acceptance tests against the fake Dhan server (no network):
30-day pull completes with manifests; kill mid-run -> resume without duplicates; 429 handled; DQ quarantine."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from project100c.data.dhan import (
    CandleJobSpec,
    ChunkStatus,
    RollingOptionJobSpec,
    estimate,
    ingest_chunk,
    plan_chunks,
    verify_job,
)
from project100c.data.dhan.jobs import DATASET_ROLLING, job_id_for, parse_spec
from project100c.data.http import HttpResponse
from project100c.data.lake import RawRef, Zone
from project100c.data.ratelimit import SimClock
from project100c.errors import (
    DownloadIncompleteError,
    DownloadJobError,
    QuotaExhaustedError,
    VendorAuthError,
    VendorRateLimitError,
)
from tests.data.dhan_fakes import FAKE_TOKEN, FakeDhan, make_rig

AUG = date(2026, 8, 3)
SEP2 = date(2026, 9, 2)


def _small(**kw: Any) -> RollingOptionJobSpec:
    base: dict[str, Any] = dict(from_date=AUG, to_date=date(2026, 8, 10), strike_offsets=(-1, 0, 1))
    base.update(kw)
    return RollingOptionJobSpec(**base)


def _all_rows(rig: Any, zone: Zone = Zone.CLEAN) -> pa.Table:
    parts = rig.lake.parts(zone, DATASET_ROLLING)
    return pa.concat_tables([rig.lake.read_part(p) for p in parts]) if parts else pa.table({})


def _dupes(t: pa.Table) -> int:
    keys = zip(
        t["expiry_flag"].to_pylist(),
        t["expiry_code"].to_pylist(),
        t["strike_label"].to_pylist(),
        t["right"].to_pylist(),
        t["ts"].to_pylist(),
        strict=True,
    )
    return sum(n - 1 for n in Counter(keys).values() if n > 1)


def test_at_30_day_pull_completes_with_manifests(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    spec = RollingOptionJobSpec(from_date=AUG, to_date=SEP2)  # NIFTY weekly near expiry, ATM±10, CE+PE
    jid = rig.downloader.create(spec)
    s = rig.downloader.run(jid)
    assert s.done == 42 and s.failed == s.rejected == s.no_data == 0
    assert rig.store.counts(jid) == {"DONE": 42}
    assert verify_job(rig.lake, rig.store, jid) == []
    t = _all_rows(rig)
    # 22 trading days (15-Aug holiday) x 385 one-minute bars (15:40 close from 3-Aug-2026) x 42 series
    assert t.num_rows == s.rows == 22 * 385 * 42
    assert _dupes(t) == 0
    row = rig.store.chunks(jid)[0]
    assert row.manifest_path is not None and row.part_path is not None
    m = rig.lake.read_manifest(row.manifest_path)
    assert m["dq"]["status"] == "PASS" and m["rows"] == 22 * 385
    assert m["lineage"]["raw_sha256"] == m["raw"]["sha256"] and m["lineage"]["data_version"].startswith(DATASET_ROLLING)
    lin = rig.lake.lineage_of(row.part_path)
    assert lin == m["lineage"] and lin["chunk_key"] == row.chunk.key
    # pacing held for the whole job
    ts = rig.fake.sent_at
    assert all(sum(1 for t2 in ts if t1 <= t2 < t1 + 1.0) <= rig.cfg.requests_per_second for t1 in ts)
    # every request went to an allowlisted data endpoint
    assert set(rig.fake.urls) == {"https://api.dhan.co/v2/charts/rollingoption"}


class SimulatedCrash(BaseException):
    """Like kill -9: not an Exception, so nothing in the downloader can catch it."""


def test_at_kill_mid_run_then_resume_without_duplicates(tmp_path: Path) -> None:
    seen: list[str] = []

    def crash_on_fifth(key: str) -> None:
        seen.append(key)
        if len(seen) == 5:
            raise SimulatedCrash()

    rig = make_rig(tmp_path, hook=crash_on_fifth)
    spec = _small()
    jid = rig.downloader.create(spec)
    with pytest.raises(SimulatedCrash):
        rig.downloader.run(jid)
    assert rig.store.counts(jid) == {"DONE": 4, "PENDING": 2}
    rig.store.close()
    # "restart": a fresh process state over the same lake and job DB
    rig2 = make_rig(tmp_path, fake=FakeDhan(SimClock()))
    assert rig2.downloader.create(spec) == jid
    s = rig2.downloader.run(jid)
    assert s.done == 2 and rig2.fake.calls == 2  # only the unfinished chunks were fetched again
    assert rig2.store.counts(jid) == {"DONE": 6}
    t = _all_rows(rig2)
    assert _dupes(t) == 0 and len(rig2.lake.parts(Zone.CLEAN, DATASET_ROLLING)) == 6
    assert verify_job(rig2.lake, rig2.store, jid) == []
    assert rig2.downloader.run(jid).attempted == 0  # idempotent


def test_at_429_handled_inside_a_job(tmp_path: Path) -> None:
    clock = SimClock()
    faults = {2: HttpResponse(429, b"", {"retry-after": "3"}), 3: HttpResponse(429, b'{"errorCode":"DH-904"}')}
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults=faults))
    jid = rig.downloader.create(_small(strike_offsets=(0,)))
    s = rig.downloader.run(jid)
    assert s.done == 2 and s.requests == 4 and 3.0 in clock.sleeps


def test_persistent_rate_limit_stops_job_resumable(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={i: HttpResponse(429, b"") for i in range(2, 30)}))
    jid = rig.downloader.create(_small(strike_offsets=(0,)))
    with pytest.raises(VendorRateLimitError):
        rig.downloader.run(jid)
    assert rig.store.counts(jid) == {"DONE": 1, "FAILED": 1}
    rig.fake.faults.clear()
    assert rig.downloader.run(jid).done == 1
    assert rig.store.counts(jid) == {"DONE": 2}


def test_auth_failure_stops_and_leaves_chunk_pending(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={1: HttpResponse(401, b'{"errorCode":"DH-901"}')}))
    jid = rig.downloader.create(_small(strike_offsets=(0,)))
    with pytest.raises(VendorAuthError):
        rig.downloader.run(jid)
    rows = rig.store.chunks(jid)
    assert [r.status for r in rows] == [ChunkStatus.PENDING, ChunkStatus.PENDING]
    assert rows[0].last_error is not None and "DH-901" in rows[0].last_error


def test_quota_exhausted_stops_cleanly(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, per_day=3)
    jid = rig.downloader.create(_small())
    with pytest.raises(QuotaExhaustedError):
        rig.downloader.run(jid)
    assert rig.store.counts(jid) == {"DONE": 3, "PENDING": 3}


def test_no_data_and_rejected_chunks_are_explicit(tmp_path: Path) -> None:
    clock = SimClock()
    fake = FakeDhan(
        clock, no_data_strikes=frozenset({"ATM+1"}), faults={1: HttpResponse(400, b'{"errorCode":"DH-905"}')}
    )
    rig = make_rig(tmp_path, fake=fake)
    jid = rig.downloader.create(_small())
    with pytest.raises(DownloadIncompleteError, match="1 REJECTED"):
        rig.downloader.run(jid)
    c = rig.store.counts(jid)
    assert c == {"REJECTED": 1, "NO_DATA": 2, "DONE": 3}
    nd = next(r for r in rig.store.chunks(jid) if r.status is ChunkStatus.NO_DATA)
    assert nd.manifest_path is not None
    assert "DH-907" in rig.lake.read_manifest(nd.manifest_path)["no_data_reason"]
    assert rig.downloader.run(jid).attempted == 0  # REJECTED is not retried automatically


def test_server_failures_marked_failed_and_retried_next_run(tmp_path: Path) -> None:
    clock = SimClock()
    rig = make_rig(
        tmp_path,
        fake=FakeDhan(clock, faults={1: HttpResponse(503, b""), 2: HttpResponse(503, b""), 3: HttpResponse(503, b"")}),
    )
    jid = rig.downloader.create(_small(strike_offsets=(0,)))
    with pytest.raises(DownloadIncompleteError, match="1 FAILED"):
        rig.downloader.run(jid)
    assert rig.downloader.run(jid).done == 1
    assert rig.store.counts(jid) == {"DONE": 2}


def test_malformed_response_fails_chunk_but_raw_is_kept(tmp_path: Path) -> None:
    clock = SimClock()
    ragged = HttpResponse(
        200, b'{"data":{"ce":{"open":[1,2],"high":[1],"low":[1],"close":[1],"timestamp":[1754192700]},"pe":null}}'
    )
    rig = make_rig(tmp_path, fake=FakeDhan(clock, faults={1: ragged}))
    jid = rig.downloader.create(
        _small(strike_offsets=(0,), rights=("CE",), required_data=("open", "high", "low", "close"))
    )
    with pytest.raises(DownloadIncompleteError):
        rig.downloader.run(jid)
    r = rig.store.chunks(jid)[0]
    assert r.status is ChunkStatus.FAILED and r.last_error is not None and "ragged" in r.last_error
    assert any(rig.lake.root.joinpath("raw").rglob("*.json.gz"))  # evidence preserved


def _mutator(kind: str) -> Any:
    def m(payload: dict[str, Any], cols: dict[str, list[str]]) -> None:
        if payload.get("strike") != "ATM" or payload.get("drvOptionType") != "CALL":
            return
        if kind == "bad_ohlc":
            cols["high"][10] = "0.05"
        elif kind == "gap":
            for k in cols:
                del cols[k][100:200]
        elif kind == "negative_oi":
            cols["oi"][5] = "-1"
        elif kind == "bad_strike":
            cols["strike"][7] = "24013"
        elif kind == "iv":
            cols["iv"][3] = "999"
        elif kind == "dupe":
            for k in cols:
                cols[k].insert(1, cols[k][0])

    return m


@pytest.mark.parametrize(
    ("kind", "code", "blocked"),
    [
        ("bad_ohlc", "BAD_OHLC", True),
        ("gap", "MISSING_CANDLE", True),
        ("negative_oi", "NEGATIVE_SIZE", True),
        ("bad_strike", "BAD_STRIKE", False),
        ("iv", "IMPLAUSIBLE_IV", False),
        ("dupe", "NON_MONOTONIC_TS", True),
    ],
)
def test_ingest_dq_detects_injected_defects(tmp_path: Path, kind: str, code: str, blocked: bool) -> None:
    clock = SimClock()
    rig = make_rig(tmp_path, fake=FakeDhan(clock, mutate=_mutator(kind)))
    jid = rig.downloader.create(_small(strike_offsets=(0,), rights=("CE",)))
    s = rig.downloader.run(jid)
    r = rig.store.chunks(jid)[0]
    assert r.manifest_path is not None
    m = rig.lake.read_manifest(r.manifest_path)
    assert any(i["code"] == code for i in m["dq"]["issues"])
    assert (m["dq"]["status"] == "BLOCKED") is blocked
    assert (m["part"]["zone"] == "quarantine") is blocked and s.quarantined == int(blocked)
    assert (rig.lake.parts(Zone.CLEAN, DATASET_ROLLING) == []) is blocked


def test_dates_before_calendar_coverage_are_flagged_not_trusted(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    jid = rig.downloader.create(
        _small(from_date=date(2020, 12, 7), to_date=date(2020, 12, 12), strike_offsets=(0,), rights=("CE",))
    )
    rig.downloader.run(jid)
    r = rig.store.chunks(jid)[0]
    assert r.manifest_path is not None
    m = rig.lake.read_manifest(r.manifest_path)
    assert m["dq"]["status"] == "WARN" and any(i["code"] == "COVERAGE_GAP" for i in m["dq"]["issues"])


def test_reingest_from_raw_is_byte_identical(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    spec = _small(strike_offsets=(0,), rights=("CE",))
    jid = rig.downloader.create(spec)
    rig.downloader.run(jid)
    r = rig.store.chunks(jid)[0]
    assert r.manifest_path is not None and r.part_sha256 is not None
    m = rig.lake.read_manifest(r.manifest_path)
    raw = RawRef(m["raw"]["path"], m["raw"]["sha256"], m["raw"]["size"], m["raw"]["fetched_at_utc"])
    res = ingest_chunk(
        lake=rig.lake,
        ctx=rig.downloader._ctx,
        config_version=rig.cfg.version,
        job_id=jid,
        spec=spec,
        chunk=r.chunk,
        raw=raw,
        no_data_code=None,
    )
    assert res.part is not None and res.part.sha256 == r.part_sha256
    assert rig.lake.read_manifest(r.manifest_path) == m


def test_corrupt_part_detected_by_verify(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    jid = rig.downloader.create(_small(strike_offsets=(0,), rights=("CE",)))
    rig.downloader.run(jid)
    r = rig.store.chunks(jid)[0]
    assert r.part_path is not None
    (rig.lake.root / r.part_path).write_bytes(b"x")
    assert any("sha256" in p for p in verify_job(rig.lake, rig.store, jid))


def test_token_never_written_to_disk(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    jid = rig.downloader.create(_small(strike_offsets=(0,)))
    rig.downloader.run(jid)
    rig.store.close()
    for p in tmp_path.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert FAKE_TOKEN.encode() not in data, p
            if p.suffix == ".gz":
                import gzip

                assert FAKE_TOKEN.encode() not in gzip.decompress(data), p


def test_intraday_index_and_futures_jobs(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    idx = CandleJobSpec(
        label="NIFTY-INDEX",
        security_id="13",
        exchange_segment="IDX_I",
        instrument="INDEX",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 9, 30),
        window_days=90,
    )
    s = rig.downloader.run(rig.downloader.create(idx))
    assert s.done == 2 and s.quarantined == 0
    fut = CandleJobSpec(
        label="NIFTY-FUT-2026-10-27",
        security_id="48704",
        exchange_segment="NSE_FNO",
        instrument="FUTIDX",
        oi=True,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 30),
    )
    jid = rig.downloader.create(fut)
    assert rig.downloader.run(jid).done == 1
    r = rig.store.chunks(jid)[0]
    assert r.part_path is not None and r.chunk.payload["toDate"] == "2026-09-29 16:00:00"
    t = rig.lake.read_part(r.part_path)
    assert t.column("oi").null_count == 0 and set(t.column("label").to_pylist()) == {"NIFTY-FUT-2026-10-27"}


def test_spec_validation_and_plan() -> None:
    from project100c.data.dhan import load_dhan_config
    from tests.data.dhan_fakes import CONFIGS

    cfg = load_dhan_config(CONFIGS / "data" / "dhan.toml")
    with pytest.raises(ValueError):
        RollingOptionJobSpec(from_date=AUG, to_date=AUG)
    with pytest.raises(ValueError):
        RollingOptionJobSpec(from_date=AUG, to_date=SEP2, strike_offsets=(11,))
    with pytest.raises(ValueError):
        CandleJobSpec(
            label="x", security_id="13", exchange_segment="NSE_FNO", instrument="INDEX", from_date=AUG, to_date=SEP2
        )
    with pytest.raises(DownloadJobError):
        plan_chunks(RollingOptionJobSpec(from_date=AUG, to_date=SEP2, window_days=31), cfg)
    with pytest.raises(DownloadJobError):
        parse_spec({"kind": "orders"})
    five_years = RollingOptionJobSpec(from_date=date(2021, 10, 1), to_date=date(2026, 10, 1))
    chunks = plan_chunks(five_years, cfg)
    est = estimate(chunks, cfg)
    assert est.chunks == 61 * 42 and est.days_at_daily_budget == 1 and est.min_minutes_at_rate < 15
    assert [c.payload["toDate"] for c in chunks[:1]] == ["2021-10-30"]  # inclusive last day of [1-Oct, 31-Oct)
    assert job_id_for(five_years) == job_id_for(
        RollingOptionJobSpec(from_date=date(2021, 10, 1), to_date=date(2026, 10, 1))
    )


def test_resume_with_changed_spec_is_a_new_job_and_tampered_plan_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    a = rig.downloader.create(_small())
    b = rig.downloader.create(_small(strike_offsets=(0,)))
    assert a != b
    rig.store._db.execute("DELETE FROM chunks WHERE job_id=? AND seq=0", (a,))
    with pytest.raises(DownloadJobError, match="plan differs"):
        rig.downloader.create(_small())


def test_rows_are_decimal_exact(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    jid = rig.downloader.create(_small(strike_offsets=(0,), rights=("CE",)))
    rig.downloader.run(jid)
    t = _all_rows(rig)
    assert pa.types.is_decimal(t.schema.field("close").type) and pa.types.is_decimal(t.schema.field("iv").type)
    assert t.schema.field("ts").type == pa.timestamp("ns", tz="UTC")
    assert FAKE_TOKEN not in str(t.schema.metadata)

"""Fixes from the first REAL Dhan pull (2-Oct-2026): what the real API does that the docs/our client assumed
differently. Fixtures in tests/fixtures/dhan/shape/ are SHAPE-FAITHFUL: field names, types, nulls, the timestamp
grid and float quirks are copied from real responses, but every price, volume, OI and IV number is replaced by a
synthetic value (no market data is committed). The DH-902 body is Dhan's error text verbatim."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import project100c.data.dhan.jobs as jobs_mod
from project100c.core_types import OptionRight
from project100c.data.dhan import CandleJobSpec, ChunkStatus, RollingOptionJobSpec, plan_chunks
from project100c.data.dhan.backfill import (
    INDEX_LABEL,
    NEAR_OFFSETS,
    NEXT_OFFSETS,
    VIX_LABEL,
    VIX_SECURITY_ID,
    ActiveFuture,
    plan_backfill,
)
from project100c.data.dhan.client import DataEndpoint, Outcome, classify, loads_decimal
from project100c.data.dhan.dq import DQStatus, SeriesKind, check_ingest, dq_status
from project100c.data.dhan.jobs import job_id_for, reingest_blocked
from project100c.data.dhan.parse import ROLLING_FIELDS, ParsedColumns, parse_candles, parse_rolling_option
from project100c.data.http import HttpResponse, ScriptedTransport
from project100c.dq.checks import DQCode, DQIssue, DQReport, DQSeverity
from project100c.errors import VendorAuthError, VendorSubscriptionError
from project100c.sessions import IST
from tests.data.dhan_fakes import REPO, ingest_ctx, make_rig

REAL = REPO / "tests" / "fixtures" / "dhan" / "shape"


def _real(name: str) -> bytes:
    return (REAL / name).read_bytes()


# ------------------------------------------------------------------ real response shapes
def test_real_intraday_index_shape_parses() -> None:
    cols = parse_candles(loads_decimal(_real("intraday_index_1m_shape.json")), with_oi=False)
    assert cols.rows == 20 and cols.no_data_reason is None
    # bar START timestamps: the first 1-minute bar of the session is 09:15 IST
    assert cols.ts[0].astimezone(IST).time() == time(9, 15)
    assert all(b - a == timedelta(minutes=1) for a, b in pairwise(cols.ts))
    # the index DOES carry a volume (not traded volume); Dhan sends the first as an int, the rest as floats (501000.0)
    assert b"501000.0" in _real("intraday_index_1m_shape.json")
    assert all(isinstance(v, int) and v > 0 for v in cols.cols["volume"])


def test_real_rolling_option_shape_and_expiry_day_settlement() -> None:
    parsed = loads_decimal(_real("rollingoption_code1_atm_call_expiry_roll_shape.json"))
    assert parsed["data"]["pe"] is None  # a CALL request returns pe=null
    cols = parse_rolling_option(parsed, OptionRight.CE, ROLLING_FIELDS)
    ist = [t.astimezone(IST) for t in cols.ts]
    # code 1 on its expiry day (Tue 22-Sep-2026): the last bar is 15:39 (F&O close 15:40 from 3-Aug-2026) and the
    # OTM ATM call settles at the 0.05 floor with IV 0: code 1 = the nearest expiry, expiry day included
    last_22 = max(i for i, t in enumerate(ist) if t.date() == date(2026, 9, 22))
    assert ist[last_22].time() == time(15, 39)
    assert cols.cols["close"][last_22] == Decimal("0.05") and cols.cols["iv"][last_22] == 0
    assert cols.cols["strike"][last_22] > cols.cols["spot"][last_22]  # OTM call
    # next day the same rolling code points at the next weekly expiry: premium is back above 100
    first_23 = min(i for i, t in enumerate(ist) if t.date() == date(2026, 9, 23))
    assert cols.cols["close"][first_23] > 100


def test_real_dh902_envelope_is_a_subscription_stop_even_on_http_401() -> None:
    outcome, code, msg, _ = classify(HttpResponse(401, _real("error_dh902_invalid_access.json")))
    assert outcome is Outcome.SUBSCRIPTION and code == "DH-902" and "subscribed" in msg


def test_real_fixtures_hold_no_account_data() -> None:
    for f in REAL.glob("*.json"):
        text = f.read_text()
        for marker in ("dhanClientId", "client", "access-token", "accessToken", "eyJ"):
            assert marker not in text, f"{f.name} contains {marker!r}"


# ------------------------------------------------------------------ DH-911 and the data-probe preflight
def _err(status: int, code: str) -> HttpResponse:
    return HttpResponse(status, json.dumps({"errorType": "x", "errorCode": code, "errorMessage": "m"}).encode())


def test_dh911_static_ip_is_a_stop_not_a_retry(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.client._transport = t = ScriptedTransport([_err(403, "DH-911")])
    with pytest.raises(VendorAuthError, match="static IP"):
        rig.client.call(DataEndpoint.INTRADAY, {"securityId": "13"})
    assert len(t.requests) == 1


def test_data_probe_catches_dh902_that_profile_misses(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.client._transport = t = ScriptedTransport([HttpResponse(401, _real("error_dh902_invalid_access.json"))])
    with pytest.raises(VendorSubscriptionError, match="regenerate the token"):
        rig.client.data_probe(date(2026, 10, 2))
    (req,) = t.requests
    assert req.url.endswith("/charts/intraday") and req.method == "POST"
    body = json.loads(req.body or b"{}")
    assert body["securityId"] == "13" and body["exchangeSegment"] == "IDX_I"
    assert body["fromDate"] == "2026-09-27 09:00:00" and body["toDate"] == "2026-10-02 16:00:00"


def test_data_probe_ok_counts_bars_and_accepts_an_empty_holiday_window(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    empty: dict[str, list[int]] = {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")}
    rig.client._transport = ScriptedTransport(
        [
            HttpResponse(200, _real("intraday_index_1m_shape.json")),
            HttpResponse(200, json.dumps(empty).encode()),
        ]
    )
    assert rig.client.data_probe(date(2026, 10, 2)) == 20
    assert rig.client.data_probe(date(2026, 10, 2)) == 0


# ------------------------------------------------------------------ expiry codes and the inclusive toDate
def test_expiry_code_zero_is_rejected_because_dhan_rejects_it() -> None:
    with pytest.raises(ValueError, match=r"1\.\.3"):
        RollingOptionJobSpec(from_date=date(2026, 9, 1), to_date=date(2026, 10, 1), expiry_codes=(0,))
    RollingOptionJobSpec(from_date=date(2026, 9, 1), to_date=date(2026, 10, 1), expiry_codes=(1, 2, 3))


def test_rolling_todate_is_the_inclusive_last_day_and_windows_do_not_overlap(tmp_path: Path) -> None:
    cfg = make_rig(tmp_path).cfg
    spec = RollingOptionJobSpec(
        from_date=date(2026, 8, 3), to_date=date(2026, 10, 2), strike_offsets=(0,), rights=(OptionRight.CE,)
    )
    pays = [c.payload for c in plan_chunks(spec, cfg)]
    assert [(p["fromDate"], p["toDate"]) for p in pays] == [("2026-08-03", "2026-09-01"), ("2026-09-02", "2026-10-01")]


def test_plan_version_is_part_of_the_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RollingOptionJobSpec(from_date=date(2026, 9, 1), to_date=date(2026, 10, 1))
    a = job_id_for(spec)
    monkeypatch.setattr(jobs_mod, "PLAN_VERSION", jobs_mod.PLAN_VERSION + 1)
    assert job_id_for(spec) != a  # a changed request mapping can never collide with a stored plan


def test_fake_server_rolling_window_has_no_out_of_window_rows(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    spec = RollingOptionJobSpec(
        from_date=date(2026, 8, 3), to_date=date(2026, 8, 10), strike_offsets=(0,), rights=(OptionRight.CE,)
    )
    rig.downloader.run(rig.downloader.create(spec))
    (row,) = rig.store.chunks(job_id_for(spec))
    m = rig.lake.read_manifest(row.manifest_path or "")
    assert "WARNING:OUTSIDE_REQUEST_WINDOW" not in m["dq"]["issue_counts"]
    assert m["dq_checks_version"] == jobs_mod.DQ_CHECKS_VERSION


# ------------------------------------------------------------------ special sessions in DQ
def _bars_on(day: date, start: time, n: int) -> ParsedColumns:
    t0 = datetime.combine(day, start, IST)
    ts = [t0 + timedelta(minutes=i) for i in range(n)]
    px = [Decimal(25000)] * n
    return ParsedColumns(ts, {"open": px, "high": px, "low": px, "close": px, "volume": [0] * n})


def _check(cols: ParsedColumns, d0: date, d1: date) -> DQReport:
    rig_cfg = make_rig_cfg()
    return check_ingest(
        cols,
        series_key="NIFTY-INDEX",
        kind=SeriesKind.INDEX,
        window_from=d0,
        window_to_exclusive=d1,
        interval_min=1,
        strike_offset=None,
        ctx=ingest_ctx(rig_cfg),
    )


def make_rig_cfg() -> Any:
    from project100c.data.dhan import load_dhan_config

    return load_dhan_config(REPO / "configs" / "data" / "dhan.toml")


def test_muhurat_bars_are_a_warning_not_a_quarantine() -> None:
    rep = _check(_bars_on(date(2025, 10, 21), time(13, 45), 60), date(2025, 10, 21), date(2025, 10, 22))
    codes = {(i.code, i.severity) for i in rep.issues}
    assert (DQCode.SPECIAL_SESSION_BAR, DQSeverity.WARNING) in codes
    assert all(i.code is not DQCode.NON_TRADING_DAY_BAR for i in rep.issues)
    assert dq_status(rep) is DQStatus.WARN


def test_bars_on_a_plain_holiday_still_block() -> None:
    rep = _check(_bars_on(date(2025, 10, 22), time(9, 15), 30), date(2025, 10, 22), date(2025, 10, 23))
    assert any(i.code is DQCode.NON_TRADING_DAY_BAR and i.severity is DQSeverity.BLOCKING for i in rep.issues)
    assert dq_status(rep) is DQStatus.BLOCKED


# ------------------------------------------------------------------ re-ingest after a DQ-rule fix (no network)
def test_reingest_moves_a_wrongly_blocked_chunk_to_clean_without_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path)
    spec = CandleJobSpec(
        label="NIFTY-INDEX",
        security_id="13",
        exchange_segment="IDX_I",
        instrument="INDEX",
        from_date=date(2026, 8, 3),
        to_date=date(2026, 8, 8),
        window_days=90,
    )
    real_check = check_ingest

    def blocking(cols: ParsedColumns, **kw: Any) -> DQReport:
        rep = real_check(cols, **kw)
        bad = DQIssue(DQCode.NON_TRADING_DAY_BAR, DQSeverity.BLOCKING, "NIFTY-INDEX", None, "old rule")
        return DQReport((*rep.issues, bad), rep.checked, rep.thresholds_version)

    monkeypatch.setattr(jobs_mod, "check_ingest", blocking)
    jid = rig.downloader.create(spec)
    rig.downloader.run(jid)
    (row,) = rig.store.chunks(jid)
    assert row.dq_status == "BLOCKED" and (row.part_path or "").startswith("quarantine/")
    sent = len(rig.transport.requests)
    monkeypatch.setattr(jobs_mod, "check_ingest", real_check)
    changed = reingest_blocked(lake=rig.lake, store=rig.store, ctx=ingest_ctx(rig.cfg), config=rig.cfg, job_id=jid)
    assert changed == [(row.chunk.key, "BLOCKED", "PASS")] and len(rig.transport.requests) == sent
    (after,) = rig.store.chunks(jid)
    assert after.status is ChunkStatus.DONE and (after.part_path or "").startswith("clean/")
    assert not rig.lake.abspath(row.part_path or "").exists()  # the quarantine part is gone
    assert reingest_blocked(lake=rig.lake, store=rig.store, ctx=ingest_ctx(rig.cfg), config=rig.cfg, job_id=jid) == []


# ------------------------------------------------------------------ the backfill plan
def test_backfill_plan_order_coverage_and_determinism() -> None:
    anchor = date(2026, 10, 2)
    fut = [ActiveFuture("NIFTY-FUT-2026-10-27", "48704")]
    items = plan_backfill(anchor=anchor, futures=fut)
    assert [job_id_for(i.spec) for i in items] == [
        job_id_for(i.spec) for i in plan_backfill(anchor=anchor, futures=fut)
    ]
    tiers = [i.tier for i in items]
    assert tiers == sorted(tiers)  # every tier-A job comes before any tier-B job
    assert {i.series for i in items if i.tier == "A"} == {"index", "vix", "futures", "options_near", "options_next"}
    start = date(2021, 10, 2)
    for series, days in (("index", 90), ("vix", 90), ("options_near", 30), ("options_next", 30)):
        ws = [(i.spec.from_date, i.spec.to_date) for i in items if i.series == series]
        assert ws == sorted(ws, reverse=True)  # newest first within a series (tier A, then tier B)
        assert ws[0][1] == anchor and ws[-1][0] == start  # exactly [anchor - 5y, anchor)
        assert all(a[0] == b[1] for a, b in pairwise(ws))  # contiguous, no gaps or overlaps
        assert all((t - f).days <= days for f, t in ws)
    a_recent = [i for i in items if i.tier == "A" and i.series == "options_near"]
    assert a_recent[-1].spec.from_date <= anchor - timedelta(days=365)  # tier A covers the last 12 months
    near = next(i.spec for i in items if i.series == "options_near")
    nxt = next(i.spec for i in items if i.series == "options_next")
    assert isinstance(near, RollingOptionJobSpec) and isinstance(nxt, RollingOptionJobSpec)
    assert near.expiry_codes == (1,) and near.strike_offsets == NEAR_OFFSETS
    assert nxt.expiry_codes == (2,) and nxt.strike_offsets == NEXT_OFFSETS
    vix = next(i.spec for i in items if i.series == "vix")
    assert isinstance(vix, CandleJobSpec) and vix.label == VIX_LABEL and vix.security_id == VIX_SECURITY_ID
    idx = next(i.spec for i in items if i.series == "index")
    assert isinstance(idx, CandleJobSpec) and idx.label == INDEX_LABEL


def test_backfill_plan_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        plan_backfill(anchor=date(2026, 10, 2), years=0)

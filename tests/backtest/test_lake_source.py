"""B-01 <-> lake: the backtester reads the Dhan downloader's lake through manifests, with lineage, and refuses
anything it cannot trace or trust (quarantine, WARN without opt-in, hash/lineage mismatch, mixed versions,
incomplete downloads, conflicting duplicates)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from project100c.backtest.lake_source import EXPIRY_CODE_ASSUMPTION, DhanLakeReader, LakeData, contract_key
from project100c.core_types import OptionRight
from project100c.data.dhan.jobs import DATASET_CANDLES, DATASET_ROLLING, job_id_for
from project100c.data.lake import Zone
from project100c.data.ratelimit import SimClock
from project100c.errors import BacktestDataError, LakeError
from project100c.sessions import IST
from tests.backtest.lake_fixture import (
    INDEX_LABEL,
    FixtureLake,
    build,
    candle_specs,
    expiry_calendar,
    lot_history,
    rolling_spec,
)
from tests.data.dhan_fakes import FakeDhan

D0, D1 = date(2026, 8, 3), date(2026, 8, 7)


def _reader(fx: FixtureLake, **kw: Any) -> DhanLakeReader:
    return DhanLakeReader(fx.rig.lake, config=fx.config, expiries=expiry_calendar(), lots=lot_history(), **kw)


def _load(fx: FixtureLake, **kw: Any) -> LakeData:
    return _reader(fx, **kw).load(option_jobs=fx.rolling, candle_jobs=fx.candles[:1], date_from=D0, date_to=D1)


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> FixtureLake:
    return build(tmp_path_factory.mktemp("lake"), [rolling_spec()], candle_specs(D0, date(2026, 8, 10)))


def test_loads_per_contract_series_with_lineage(fx: FixtureLake) -> None:
    data = _load(fx)
    opt, cnd = data.reports
    assert opt.dataset == DATASET_ROLLING and cnd.dataset == DATASET_CANDLES
    assert opt.job_ids == (job_id_for(fx.rolling[0]),)
    assert len(opt.parts) == 6 and not opt.skipped  # 3 offsets x 2 rights x 1 window
    manifest = fx.rig.lake.read_manifest(opt.parts[0].manifest)
    assert opt.parts[0].raw_sha256 == manifest["raw"]["sha256"] and opt.parts[0].sha256 == manifest["part"]["sha256"]
    # 5 trading days x 385 one-minute F&O bars (09:15-15:40 session) x 3 offsets x 2 rights, re-keyed by actual
    # strike, nothing lost
    assert len(data.option_bars) == sum(p.rows_used for p in opt.parts) == 5 * 385 * 6
    assert Counter(b.instrument_key for b in data.option_bars).total() == len(data.option_bars)
    exps = {c.expiry for c in data.contracts.values()}
    assert exps == {date(2026, 8, 4), date(2026, 8, 11)}  # Mon 3-Aug -> Tue 4-Aug; Wed-Fri -> 11-Aug
    assert all(c.lot_size == 65 for c in data.contracts.values())  # NSE/FAOP/70616
    b = data.option_bars[0]
    c = data.contracts[b.instrument_key]
    assert b.instrument_key == contract_key("NIFTY", c.expiry, c.strike, c.right)
    assert data.spot_at(b.instrument_key, b.start) is not None and b.start.tzinfo is IST
    assert EXPIRY_CODE_ASSUMPTION in data.metadata()["assumptions"]
    assert len(data.candles[INDEX_LABEL]) == 5 * 375


def test_rekeying_matches_the_rolling_strike(fx: FixtureLake) -> None:
    data = _load(fx)
    for key, c in data.contracts.items():
        _, exp, strike, right = key.split("|")
        assert (date.fromisoformat(exp), Decimal(strike), OptionRight(right)) == (c.expiry, c.strike, c.right)
    for b in data.option_bars[:2000]:
        spot = data.spot_at(b.instrument_key, b.start)
        assert spot is not None
        assert abs(data.contracts[b.instrument_key].strike - spot) <= Decimal(75)  # within ATM+-1 (step 50)


def test_fingerprint_is_stable_and_sensitive(fx: FixtureLake) -> None:
    a, b = _load(fx), _load(fx)
    assert a.metadata() == b.metadata()
    short = _reader(fx).load(option_jobs=fx.rolling, candle_jobs=fx.candles[:1], date_from=D0, date_to=D0)
    assert short.reports[0].fingerprint() != a.reports[0].fingerprint()


def test_explicit_complete_selection_required(fx: FixtureLake, tmp_path: Path) -> None:
    r = _reader(fx)
    with pytest.raises(BacktestDataError, match="at least one"):
        r.load(option_jobs=[], candle_jobs=[], date_from=D0, date_to=D1)
    never_run = rolling_spec(strike_offsets=(5,))
    with pytest.raises(BacktestDataError, match="download incomplete"):
        r.load(option_jobs=[never_run], candle_jobs=fx.candles[:1], date_from=D0, date_to=D1)
    with pytest.raises(BacktestDataError, match="wrong kind"):
        r.load(option_jobs=fx.candles[:1], candle_jobs=fx.candles[:1], date_from=D0, date_to=D1)  # type: ignore[arg-type]


def test_one_dataset_may_be_loaded_alone(fx: FixtureLake) -> None:
    """Options and candles can be read over different windows (e.g. a longer index warm-up than the options)."""
    both = _load(fx)
    cand = _reader(fx).load(option_jobs=[], candle_jobs=fx.candles[:1], date_from=D0, date_to=D1)
    assert not cand.option_bars and not cand.contracts and len(cand.reports) == 1
    assert cand.candles == both.candles and cand.reports[0].fingerprint() == both.reports[1].fingerprint()
    opt = _reader(fx).load(option_jobs=fx.rolling, candle_jobs=[], date_from=D0, date_to=D1)
    assert not opt.candles and len(opt.reports) == 1
    assert opt.option_bars == both.option_bars and opt.reports[0].fingerprint() == both.reports[0].fingerprint()


def _one_rolling(tmp: Path, fake: FakeDhan | None = None, **kw: Any) -> FixtureLake:
    return build(tmp, [rolling_spec(strike_offsets=(0,), **kw)], candle_specs(D0, date(2026, 8, 10))[:1], fake)


def _mutating(kind: str) -> FakeDhan:
    def m(payload: dict[str, Any], cols: dict[str, list[str]]) -> None:
        if payload.get("drvOptionType") != "CALL" or "strike" not in payload:
            return
        if kind == "bad_ohlc":
            cols["high"][10] = "0.05"
        elif kind == "iv":
            cols["iv"][3] = "999"

    return FakeDhan(SimClock(), mutate=m)


def test_quarantined_parts_are_never_read(tmp_path: Path) -> None:
    fx = _one_rolling(tmp_path, _mutating("bad_ohlc"))
    assert fx.rig.lake.parts(Zone.QUARANTINE, DATASET_ROLLING)
    data = _load(fx)
    assert data.reports[0].skipped_by_reason() == {"QUARANTINED": 1}
    assert {c.right for c in data.contracts.values()} == {OptionRight.PE}  # the CE chunk is quarantined


def test_warn_parts_need_opt_in(tmp_path: Path) -> None:
    fx = _one_rolling(tmp_path, _mutating("iv"))
    assert _load(fx).reports[0].skipped_by_reason() == {"DQ_WARN_EXCLUDED": 1}
    data = _load(fx, accept_warn=True)
    assert data.reports[0].as_metadata()["dq_warn_parts"] == 1 and not data.reports[0].skipped


def test_corrupt_part_is_refused(tmp_path: Path) -> None:
    fx = _one_rolling(tmp_path)
    part = fx.rig.lake.parts(Zone.CLEAN, DATASET_ROLLING)[0]
    p = fx.rig.lake.abspath(part)
    p.write_bytes(p.read_bytes()[:-9] + b"corrupted")
    with pytest.raises(LakeError, match="sha256 mismatch"):
        _load(fx)


def _edit_manifest(fx: FixtureLake, edit: Any) -> None:
    rel = fx.rig.lake.manifests(DATASET_ROLLING)[0]
    p = fx.rig.lake.abspath(rel)
    m = json.loads(p.read_bytes())
    edit(m)
    p.write_text(json.dumps(m))


def test_lineage_mismatch_is_refused(tmp_path: Path) -> None:
    fx = _one_rolling(tmp_path)
    _edit_manifest(fx, lambda m: m["lineage"].update(raw_sha256="0" * 64))
    with pytest.raises(LakeError, match="lineage differs"):
        _load(fx)


def test_manifest_for_another_request_is_refused(tmp_path: Path) -> None:
    fx = _one_rolling(tmp_path)
    _edit_manifest(fx, lambda m: m["request"].update(toDate="2026-08-11"))
    with pytest.raises(LakeError, match="planned request"):
        _load(fx)


def test_mixed_data_versions_are_refused(tmp_path: Path) -> None:
    fx = _one_rolling(tmp_path)
    _edit_manifest(fx, lambda m: m.update(data_version="dhan_rolling_option@schema0+old"))
    with pytest.raises(BacktestDataError, match="data_version"):
        _load(fx)


def test_overlapping_jobs_dedupe_identical_rows(tmp_path: Path) -> None:
    a = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,))
    b = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,), from_date=date(2026, 8, 5), window_days=2)
    fx = build(tmp_path, [a, b], candle_specs(D0, date(2026, 8, 10))[:1])
    data = _load(fx)
    # b: windows [5,7) [7,9) [9,10) -> 5, 6, 7 Aug requested twice; 9-Aug (Sunday) is an explicit NO_DATA chunk
    assert data.reports[0].identical_duplicates == 3 * 385
    assert data.reports[0].skipped_by_reason() == {"NO_DATA": 1}
    assert len(data.option_bars) == 5 * 385
    assert len(data.reports[0].job_ids) == 2


def test_conflicting_duplicates_whole_days_are_excluded(tmp_path: Path) -> None:
    a = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,))
    fx = build(tmp_path, [a], candle_specs(D0, date(2026, 8, 10))[:1])

    def bump(payload: dict[str, Any], cols: dict[str, list[str]]) -> None:
        cols["volume"] = [str(int(v) + 1) for v in cols["volume"]]  # same shape, different values: DQ cannot see it

    b = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,), from_date=date(2026, 8, 5), window_days=2)
    fx.rig.fake.mutate = bump
    fx.rig.downloader.run(fx.rig.downloader.create(b))
    fx.rolling.append(b)
    # OD-016: conflicting rows are dropped (the minute is missing), not a run-stopping error. Here every minute of
    # 5..7 Aug (b's two chunks, inside D0..D1) conflicts, so both parts' series-days far exceed the 1%
    # missing-minute threshold and are excluded.
    data = _load(fx)
    rep = data.reports[0]
    assert rep.conflict_minutes == 3 * 385 and rep.conflict_rows_dropped == 2 * 3 * 385
    assert sorted(d for _, d in rep.conflict_series_days_excluded) == sorted(
        ["2026-08-05", "2026-08-06", "2026-08-07"] * 2
    )
    days = {x.start.date() for x in data.option_bars}
    assert days == {date(2026, 8, 3), date(2026, 8, 4)}
    assert len(data.option_bars) == 2 * 385 == sum(p.rows_used for p in rep.parts)
    assert rep.as_metadata()["conflict_minutes"] == 3 * 385


def test_one_conflicting_minute_is_dropped_and_counted(tmp_path: Path) -> None:
    a = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,))
    fx = build(tmp_path, [a], candle_specs(D0, date(2026, 8, 10))[:1])
    clean = _load(fx)

    def bump_one(payload: dict[str, Any], cols: dict[str, list[str]]) -> None:
        cols["volume"] = [str(int(v) + 1) if i == 100 else v for i, v in enumerate(cols["volume"])]

    b = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,), from_date=date(2026, 8, 5), window_days=2)
    fx.rig.fake.mutate = bump_one
    fx.rig.downloader.run(fx.rig.downloader.create(b))
    fx.rolling.append(b)
    data = _load(fx)
    rep = data.reports[0]
    assert (rep.conflict_minutes, rep.conflict_rows_dropped, rep.conflict_series_days_excluded) == (
        2,
        4,
        [],
    )  # row 100 of each of b's two chunks
    assert len(data.option_bars) == len(clean.option_bars) - 2  # only those contract-minutes are missing
    gone = {(x.instrument_key, x.start) for x in clean.option_bars} - {
        (x.instrument_key, x.start) for x in data.option_bars
    }
    assert sorted(t.date() for _, t in gone) == [date(2026, 8, 5), date(2026, 8, 7)]
    assert rep.fingerprint() != clean.reports[0].fingerprint()


def test_conflicts_count_toward_the_missing_minute_threshold(tmp_path: Path) -> None:
    a = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,))
    fx = build(tmp_path, [a], candle_specs(D0, date(2026, 8, 10))[:1])

    def bump_five(payload: dict[str, Any], cols: dict[str, list[str]]) -> None:
        cols["volume"] = [str(int(v) + 1) if 100 <= i < 105 else v for i, v in enumerate(cols["volume"])]

    b = rolling_spec(strike_offsets=(0,), rights=(OptionRight.CE,), from_date=date(2026, 8, 5), window_days=2)
    fx.rig.fake.mutate = bump_five
    fx.rig.downloader.run(fx.rig.downloader.create(b))
    fx.rolling.append(b)
    # 5 conflicting minutes on 5-Aug and on 7-Aug: 5/375 > 1% -> both parts' series-days excluded; 6-Aug is kept
    assert _load(fx, max_missing_fraction=Decimal("0.02")).reports[0].conflict_series_days_excluded == []
    rep = _load(fx).reports[0]
    assert sorted(d for _, d in rep.conflict_series_days_excluded) == [
        "2026-08-05",
        "2026-08-05",
        "2026-08-07",
        "2026-08-07",
    ]


def test_expiry_code_mapping(fx: FixtureLake) -> None:
    r = _reader(fx)
    assert r.expiry_for("WEEK", 1, date(2026, 8, 4)) == date(2026, 8, 4)  # expiry day maps to itself
    assert r.expiry_for("WEEK", 2, date(2026, 8, 4)) == date(2026, 8, 11)
    assert r.expiry_for("MONTH", 1, date(2026, 8, 26)) == date(2026, 9, 29)
    assert r.expiry_for("WEEK", 1, date(2025, 8, 28)) == date(2025, 8, 28)  # Thursday regime
    with pytest.raises(BacktestDataError, match="code 0"):
        r.expiry_for("WEEK", 0, date(2026, 8, 4))

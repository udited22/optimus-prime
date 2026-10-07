"""Tick recorder against a scripted fake feed: persistence, duplicates, out-of-order, gaps, manifest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.errors import RecorderError
from project100c.market_types import Quote
from project100c.recorder import FakeFeed, FeedMessage, MsgKind, TickRecorder, read_gaps, read_ticks
from project100c.sessions import IST

K1 = "NSE_FO|NIFTY06OCT2625000CE"
K2 = "NSE_FO|NIFTY06OCT2625000PE"
T0 = datetime(2026, 10, 5, 9, 15, tzinfo=IST)


def tick(sec: float, key: str = K1, bid: str = "10.00", ask: str = "10.05", ex_lag: float = 0.05) -> FeedMessage:
    rts = T0 + timedelta(seconds=sec)
    q = Quote(key, rts - timedelta(seconds=ex_lag), rts, D(bid), D(ask), 650, 650, D(ask), 1000)
    return FeedMessage(MsgKind.TICK, rts, q)


def msg(kind: MsgKind, sec: float, detail: str = "") -> FeedMessage:
    return FeedMessage(kind, T0 + timedelta(seconds=sec), detail=detail)


def _day(root: Path) -> Path:
    return root / "date=2026-10-05"


def test_ticks_round_trip_losslessly(tmp_path: Path) -> None:
    feed = FakeFeed([tick(0), tick(0.5, bid="10.05", ask="10.10"), tick(0.7, K2, "20.00", "20.05")])
    rec = TickRecorder(tmp_path)
    stats = rec.run(feed)
    man = rec.close()
    assert stats.ticks_written == 3 and stats.duplicates == 0
    rows = list(read_ticks(_day(tmp_path) / "instrument=NSE_FO_NIFTY06OCT2625000CE" / "ticks.jsonl"))
    assert [r[0].bid for r in rows] == [D("10.00"), D("10.05")]
    assert rows[0][0].exchange_ts == T0 - timedelta(seconds=0.05) and rows[0][0].exchange_ts.tzinfo is not None
    assert set(man["files"]) == {
        "date=2026-10-05/instrument=NSE_FO_NIFTY06OCT2625000CE/ticks.jsonl",
        "date=2026-10-05/instrument=NSE_FO_NIFTY06OCT2625000PE/ticks.jsonl",
    }
    on_disk = json.loads((tmp_path / "manifest.json").read_text())
    assert on_disk["stats"]["ticks_written"] == 3


def test_duplicates_dropped_out_of_order_flagged(tmp_path: Path) -> None:
    a = tick(1)
    late = tick(2, bid="9.95", ex_lag=1.5)  # exchange ts 0.5 s: older than the previous tick
    rec = TickRecorder(tmp_path)
    rec.run(FakeFeed([a, a, tick(1.2, bid="10.05"), late]))
    rec.close()
    assert rec.stats.duplicates == 1 and rec.stats.out_of_order == 1
    rows = list(read_ticks(_day(tmp_path) / "instrument=NSE_FO_NIFTY06OCT2625000CE" / "ticks.jsonl"))
    assert [ooo for _, ooo in rows] == [False, False, True]


def test_disconnect_and_silence_become_explicit_gaps(tmp_path: Path) -> None:
    feed = FakeFeed(
        [
            tick(0),
            msg(MsgKind.HEARTBEAT, 1),
            tick(2),
            msg(MsgKind.DISCONNECT, 3, "ws closed 1006"),
            msg(MsgKind.RECONNECT, 40),
            tick(41, bid="10.10", ask="10.15"),
            tick(50, bid="10.20", ask="10.25"),  # 9 s silence > 3 s threshold
        ]
    )
    rec = TickRecorder(tmp_path)
    rec.run(feed)
    rec.close()
    gaps = read_gaps(_day(tmp_path) / "gaps.jsonl")
    assert [(g[0] - T0).total_seconds() for g in gaps] == [3, 41]
    assert [(g[1] - T0).total_seconds() for g in gaps] == [41, 50]
    assert "disconnect" in gaps[0][2] and "silent" in gaps[1][2]
    assert rec.stats.heartbeats == 1 and rec.stats.gaps == 2


def test_open_gap_at_close_needs_explicit_end(tmp_path: Path) -> None:
    rec = TickRecorder(tmp_path)
    rec.run(FakeFeed([tick(0), msg(MsgKind.DISCONNECT, 1)]))
    with pytest.raises(RecorderError, match="open gap"):
        rec.close()
    rec.close(at=T0 + timedelta(hours=6))
    assert len(read_gaps(_day(tmp_path) / "gaps.jsonl")) == 1
    with pytest.raises(RecorderError, match="closed"):
        rec.on_message(tick(10))


def test_invalid_inputs_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(RecorderError):
        FeedMessage(MsgKind.TICK, T0)  # no quote
    with pytest.raises(RecorderError):
        FeedMessage(MsgKind.HEARTBEAT, datetime(2026, 10, 5, 9, 15))  # naive
    with pytest.raises(RecorderError):
        TickRecorder(tmp_path, gap_threshold_s=D(0))
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"instrument_key": 1}\n')
    with pytest.raises(RecorderError, match="malformed"):
        list(read_ticks(bad))
    with pytest.raises(RecorderError):
        list(read_ticks(tmp_path / "missing.jsonl"))
    assert read_gaps(tmp_path / "none.jsonl") == []


def test_write_failure_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "root"
    blocker.write_text("a file where a directory must go")
    rec = TickRecorder(blocker)
    with pytest.raises(RecorderError, match="cannot write"):
        rec.on_message(tick(0))

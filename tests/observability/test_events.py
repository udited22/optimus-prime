"""EventBus and DashEvent: the backbone of the SSE stream."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from decimal import Decimal

import pytest

from project100c.observability.dashboard import SIMULATED_LABEL, DashEvent, EventBus, EventKind, Flow
from project100c.observability.dashboard.events import DashboardError
from project100c.sessions import IST

T = datetime(2026, 10, 5, 9, 30, tzinfo=IST)


def ev(kind: EventKind = EventKind.TICK, **data: object) -> DashEvent:
    return DashEvent(T, kind, data or {"spot": Decimal("24800.05")})


def test_event_json_is_labelled_simulated_and_keeps_decimals_exact() -> None:
    d = json.loads(ev().with_seq(7).to_json())
    assert d["seq"] == 7 and d["simulated"] is True and d["label"] == SIMULATED_LABEL == "SIMULATED"
    assert d["data"]["spot"] == "24800.05"  # string, never a float
    assert d["ts"] == "2026-10-05T09:30:00+05:30"
    f = DashEvent(T, EventKind.INTENT, {}, (Flow("allocator", "risk_governor", "intent"),)).as_dict()
    assert f["flows"] == [["allocator", "risk_governor", "intent"]]


def test_naive_datetimes_and_unserialisable_values_are_errors() -> None:
    with pytest.raises(DashboardError):
        DashEvent(datetime(2026, 10, 5, 9, 30), EventKind.TICK, {}).to_json()
    with pytest.raises(DashboardError):
        DashEvent(T, EventKind.TICK, {"x": object()}).to_json()


def test_publish_assigns_monotonic_seq_and_since_returns_only_newer() -> None:
    bus = EventBus()
    out = bus.publish([ev(), ev(EventKind.RISK, nav=Decimal(1)), ev()])
    assert [e.seq for e in out] == [1, 2, 3] and bus.last_seq == 3
    assert [e.seq for e in bus.since(1)] == [2, 3]
    assert bus.since(3) == []
    assert [e.seq for e in bus.since(0, limit=2)] == [1, 2]


def test_snapshot_is_latest_per_kind_and_tail_filters_by_kind() -> None:
    bus = EventBus()
    bus.publish([ev(spot=Decimal(1)), ev(EventKind.LOG, level="INFO", text="a"), ev(spot=Decimal(2))])
    bus.publish([ev(EventKind.LOG, level="OK", text="b")])
    snap = bus.snapshot()
    assert snap["TICK"]["data"]["spot"] == "2" and snap["LOG"]["data"]["text"] == "b"
    assert [e.data["text"] for e in bus.tail(EventKind.LOG, 5)] == ["a", "b"]
    assert [e.data["text"] for e in bus.tail(EventKind.LOG, 1)] == ["b"]


def test_current_session_is_the_day_so_far_without_excluded_kinds() -> None:
    bus = EventBus()
    assert bus.current_session() == []
    bus.publish([ev(EventKind.SESSION, n=1), ev(spot=Decimal(1)), ev(EventKind.SESSION, n=2)])
    bus.publish([ev(spot=Decimal(2)), ev(EventKind.RISK, nav="1"), ev(EventKind.LOG, level="INFO", text="x")])
    day = bus.current_session(frozenset({EventKind.RISK}))
    assert [e.kind for e in day] == [EventKind.SESSION, EventKind.TICK, EventKind.LOG]
    assert day[0].data["n"] == 2 and day[1].data["spot"] == Decimal(2)


def test_history_is_bounded() -> None:
    bus = EventBus(history=5)
    bus.publish([ev() for _ in range(12)])
    assert [e.seq for e in bus.since(0)] == [8, 9, 10, 11, 12]


def test_since_blocks_until_publish_or_timeout() -> None:
    bus = EventBus()
    t0 = time.monotonic()
    assert bus.since(0, timeout=0.15) == []
    assert time.monotonic() - t0 >= 0.14
    threading.Timer(0.05, lambda: bus.publish([ev()])).start()
    got = bus.since(0, timeout=5)
    assert [e.seq for e in got] == [1]


def test_close_wakes_waiters() -> None:
    bus = EventBus()
    threading.Timer(0.05, bus.close).start()
    t0 = time.monotonic()
    assert bus.since(0, timeout=5) == []
    assert time.monotonic() - t0 < 2 and bus.closed

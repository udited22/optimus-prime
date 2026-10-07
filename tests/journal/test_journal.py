"""Journal (K-01): lossless codec, append-only chain, tamper detection, replay, crash safety (kill -9 fuzz)."""

from __future__ import annotations

import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from project100c.errors import JournalError
from project100c.journal import Journal, JournalRecord, decode, encode

REPO = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(milliseconds=5)
        return self.t


def test_codec_round_trip_is_lossless() -> None:
    p: dict[str, Any] = {
        "price": Decimal("101.05"),
        "ts": datetime(2026, 10, 1, 9, 15, 0, 123456, tzinfo=UTC),
        "d": date(2026, 10, 6),
        "qty": 65,
        "ok": True,
        "none": None,
        "legs": [{"side": "BUY", "px": Decimal("0.05")}],
    }
    assert decode(encode(p)) == p
    assert encode(p) == encode(dict(reversed(list(p.items()))))  # canonical key order


@pytest.mark.parametrize(
    "bad",
    [
        {"x": 1.5},
        {"x": datetime(2026, 10, 1, 9, 15)},
        {"x": Decimal("NaN")},
        {"$dec": "1"},
        {"x": {1: "a"}},
        {"x": object()},
    ],
)
def test_codec_rejects_unsafe_values(bad: dict[str, Any]) -> None:
    with pytest.raises(JournalError):
        encode(bad)


def test_append_read_verify(tmp_path: Path) -> None:
    with Journal(tmp_path / "j.db", clock=Clock()) as j:
        r1 = j.append("ORDER_NEW", {"qty": 65, "px": Decimal("100.5")}, correlation_id="intent-1")
        r2 = j.append("ORDER_ACK", {"broker_id": "B1"}, correlation_id="intent-1")
        assert (r1.seq, r2.seq) == (1, 2)
        recs = list(j.records())
        assert [r.event_type for r in recs] == ["ORDER_NEW", "ORDER_ACK"]
        assert recs[0].payload["px"] == Decimal("100.5")
        assert j.verify() == (2, r2.hash) == (2, j.head[1])


def test_reopen_continues_chain(tmp_path: Path) -> None:
    clock = Clock()
    with Journal(tmp_path / "j.db", clock=clock) as j:
        j.append("A", {})
    with Journal(tmp_path / "j.db", clock=clock) as j:
        assert j.head[0] == 1
        j.append("B", {})
        assert j.verify()[0] == 2


def test_update_delete_blocked_and_bypass_detected(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    with Journal(db, clock=Clock()) as j:
        for i in range(3):
            j.append("FILL", {"qty": 65, "i": i})
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE journal SET payload='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM journal WHERE seq=1")
    conn.executescript(
        "DROP TRIGGER journal_no_update; DROP TRIGGER journal_no_delete;"
        "UPDATE journal SET payload=replace(payload, '65', '130') WHERE seq=2;"
    )
    conn.commit()
    conn.close()
    with Journal(db, clock=Clock()) as j:
        with pytest.raises(JournalError, match="content hash mismatch at seq 2"):
            j.verify()
        with pytest.raises(JournalError, match="JOURNAL_TAMPER"):
            j.replay(lambda s, r: s, 0)


def test_deleted_row_detected(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    with Journal(db, clock=Clock()) as j:
        for i in range(3):
            j.append("X", {"i": i})
    conn = sqlite3.connect(db)
    conn.executescript("DROP TRIGGER journal_no_delete; DELETE FROM journal WHERE seq=2;")
    conn.close()
    with Journal(db, clock=Clock()) as j, pytest.raises(JournalError, match="expected seq 2"):
        j.verify()


def test_write_failure_raises_typed_error(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.db", clock=Clock())
    j.close()
    with pytest.raises(JournalError, match="append failed"):
        j.append("X", {})
    with pytest.raises(JournalError):
        Journal(tmp_path / "no" / "such" / "dir" / "j.db")
    with (
        Journal(tmp_path / "k.db", clock=lambda: datetime(2026, 10, 1)) as k,
        pytest.raises(JournalError, match="naive"),
    ):
        k.append("X", {})


def test_replay_reducer(tmp_path: Path) -> None:
    def reducer(state: dict[str, int], rec: JournalRecord) -> dict[str, int]:
        if rec.event_type == "FILL":
            key = str(rec.payload["instrument"])
            sign = 1 if rec.payload["side"] == "BUY" else -1
            return {**state, key: state.get(key, 0) + sign * int(rec.payload["qty"])}
        return state

    with Journal(tmp_path / "j.db", clock=Clock()) as j:
        j.append("FILL", {"instrument": "CE1", "side": "BUY", "qty": 65})
        j.append("NOTE", {"text": "x"})
        j.append("FILL", {"instrument": "CE1", "side": "SELL", "qty": 65})
        j.append("FILL", {"instrument": "PE1", "side": "BUY", "qty": 65})
        empty: dict[str, int] = {}
        assert j.replay(reducer, empty) == {"CE1": 0, "PE1": 65}


_CHILD = """
import sys
from pathlib import Path
from project100c.journal import Journal
j = Journal(Path(sys.argv[1]))
i = j.head[0]
while True:
    j.append("TICK", {"n": i})
    i += 1
"""


def test_kill_minus_nine_fuzz(tmp_path: Path) -> None:
    """SIGKILL the writer at random points; the journal must always verify, be gap-free, and rebuild exactly.
    CI runs 20 iterations (the backlog AT asks for 1,000; run with P100C_CRASH_ITERS=1000 for the full AT)."""
    iters = int(os.environ.get("P100C_CRASH_ITERS", "20"))
    rng = random.Random(1234)
    db = tmp_path / "crash.db"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    last = 0
    for _ in range(iters):
        p = subprocess.Popen([sys.executable, "-c", _CHILD, str(db)], env=env)
        time.sleep(0.15 + rng.random() * 0.2)
        p.send_signal(signal.SIGKILL)
        p.wait()
        with Journal(db) as j:
            n, _ = j.verify()
            assert n >= last

            def step(acc: tuple[int, bool], r: JournalRecord) -> tuple[int, bool]:
                return acc[0] + 1, acc[1] and int(r.payload["n"]) == acc[0]

            count, contiguous = j.replay(step, (0, True))
            assert count == n and contiguous, "rebuild must be exactly the committed prefix"
            last = n
    assert last > 0

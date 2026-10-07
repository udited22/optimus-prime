"""Append-only, hash-chained event journal (backlog K-01). The system of record.

- SQLite in WAL mode with synchronous=FULL: a committed append survives a process crash (kill -9).
- Triggers abort UPDATE/DELETE; every row carries sha256(prev_hash, seq, ts, type, correlation, payload) so edits
  that bypass the triggers are detected by verify().
- Any write failure raises JournalError. Callers treat it as SYSTEM_INTEGRITY_KILL (docs/risk/risk-engine.md §9.4).
- State is rebuilt by replaying records through a pure reducer (replay()).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from project100c.errors import JournalError
from project100c.journal.codec import decode, encode

GENESIS = "0" * 64
S = TypeVar("S")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq            INTEGER PRIMARY KEY,
    ts_utc         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    payload        TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
"""


@dataclass(frozen=True, slots=True)
class JournalRecord:
    seq: int
    ts: datetime
    event_type: str
    correlation_id: str
    payload: dict[str, Any]
    hash: str


def _row_hash(prev: str, seq: int, ts: str, et: str, corr: str, payload: str) -> str:
    material = json.dumps([prev, seq, ts, et, corr, payload], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(material.encode()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Journal:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._path = path
        self._clock = clock
        try:
            self._conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        except sqlite3.Error as e:
            raise JournalError(f"cannot open journal {path}: {e}") from e
        self._head_seq, self._head_hash = (0, GENESIS) if row is None else (int(row[0]), str(row[1]))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def head(self) -> tuple[int, str]:
        return self._head_seq, self._head_hash

    def append(self, event_type: str, payload: dict[str, Any], *, correlation_id: str = "") -> JournalRecord:
        if not event_type:
            raise JournalError("event_type is empty")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise JournalError("journal clock returned a naive datetime")
        ts = now.astimezone(UTC).isoformat()
        body = encode(payload)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (1, GENESIS) if row is None else (int(row[0]) + 1, str(row[1]))
            h = _row_hash(prev, seq, ts, event_type, correlation_id, body)
            self._conn.execute(
                "INSERT INTO journal(seq, ts_utc, event_type, correlation_id, payload, prev_hash, hash) "
                "VALUES (?,?,?,?,?,?,?)",
                (seq, ts, event_type, correlation_id, body, prev, h),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # the original error is what matters; it is raised below
            raise JournalError(f"journal append failed ({event_type}): {e}") from e
        self._head_seq, self._head_hash = seq, h
        return JournalRecord(seq, datetime.fromisoformat(ts), event_type, correlation_id, decode(body), h)

    def records(self, *, from_seq: int = 1) -> Iterator[JournalRecord]:
        try:
            cur = self._conn.execute(
                "SELECT seq, ts_utc, event_type, correlation_id, payload, hash FROM journal "
                "WHERE seq >= ? ORDER BY seq",
                (from_seq,),
            )
            for seq, ts, et, corr, payload, h in cur:
                yield JournalRecord(int(seq), datetime.fromisoformat(ts), et, corr, decode(payload), h)
        except sqlite3.Error as e:
            raise JournalError(f"journal read failed: {e}") from e

    def verify(self) -> tuple[int, str]:
        """Recompute the chain; raise JournalError on gaps, reordering or edited rows. Returns (count, head)."""
        prev, expected, n = GENESIS, 1, 0
        try:
            cur = self._conn.execute(
                "SELECT seq, ts_utc, event_type, correlation_id, payload, prev_hash, hash FROM journal ORDER BY seq"
            )
            for seq, ts, et, corr, payload, prev_hash, h in cur:
                if seq != expected:
                    raise JournalError(f"JOURNAL_TAMPER: expected seq {expected}, found {seq}")
                if prev_hash != prev:
                    raise JournalError(f"JOURNAL_TAMPER: prev_hash mismatch at seq {seq}")
                if _row_hash(prev_hash, seq, ts, et, corr, payload) != h:
                    raise JournalError(f"JOURNAL_TAMPER: content hash mismatch at seq {seq}")
                prev, expected, n = h, expected + 1, n + 1
        except sqlite3.Error as e:
            raise JournalError(f"journal read failed: {e}") from e
        return n, prev

    def replay(self, reducer: Callable[[S, JournalRecord], S], initial: S, *, verify: bool = True) -> S:
        if verify:
            self.verify()
        state = initial
        for rec in self.records():
            state = reducer(state, rec)
        return state

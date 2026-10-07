"""Feed-agnostic tick recorder (backlog D-10 groundwork), exercised against a scripted fake feed only.

No broker connection, no credentials: the Upstox Market Data Feed V3 client (protobuf over WebSocket) is a later
module that will produce the same FeedMessage stream. Storage is append-only JSONL per trading day and instrument,
encoded with the journal codec (Decimal/datetime lossless; floats and naive datetimes rejected). Gaps (feed
silence > threshold, disconnects) are written as explicit GAP records so downstream DQ/backtests never mistake
missing data for a quiet market. Duplicates are dropped and counted; out-of-order ticks are kept and flagged.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from project100c.errors import JournalError, RecorderError
from project100c.journal.codec import decode, encode
from project100c.market_types import Quote
from project100c.sessions import IST


class MsgKind(StrEnum):
    TICK = "TICK"
    HEARTBEAT = "HEARTBEAT"
    DISCONNECT = "DISCONNECT"
    RECONNECT = "RECONNECT"


@dataclass(frozen=True, slots=True)
class FeedMessage:
    kind: MsgKind
    ts: datetime  # receive time
    quote: Quote | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise RecorderError("FeedMessage.ts must be timezone-aware")
        if (self.kind is MsgKind.TICK) != (self.quote is not None):
            raise RecorderError("TICK messages (and only TICK) carry a quote")


class FakeFeed:
    """A scripted feed: yields the given messages in order. Build scripts with the helpers below."""

    def __init__(self, messages: Iterable[FeedMessage]) -> None:
        self._msgs = list(messages)

    def __iter__(self) -> Iterator[FeedMessage]:
        return iter(self._msgs)


@dataclass
class RecorderStats:
    ticks_written: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    heartbeats: int = 0
    files: dict[str, int] = field(default_factory=dict)


def _safe(key: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    if not out:
        raise RecorderError("empty instrument key")
    return out


def _quote_payload(q: Quote) -> dict[str, Any]:
    return {
        "instrument_key": q.instrument_key,
        "exchange_ts": q.exchange_ts,
        "receive_ts": q.receive_ts,
        "bid": q.bid,
        "ask": q.ask,
        "bid_qty": q.bid_qty,
        "ask_qty": q.ask_qty,
        "ltp": q.ltp,
        "oi": q.oi,
    }


class TickRecorder:
    def __init__(self, root: Path, *, gap_threshold_s: Decimal = Decimal(3), flush_every: int = 100) -> None:
        if gap_threshold_s <= 0 or flush_every <= 0:
            raise RecorderError("gap_threshold_s and flush_every must be > 0")
        self._root = root
        self._gap = timedelta(seconds=float(gap_threshold_s))
        self._flush_every = flush_every
        self._files: dict[Path, IO[str]] = {}
        self._pending = 0
        self._last_msg_ts: datetime | None = None
        self._gap_open: datetime | None = None
        self._gap_reason = ""
        self._last_tick: dict[str, tuple[datetime, Decimal | None, Decimal | None, Decimal | None]] = {}
        self.stats = RecorderStats()
        self._closed = False

    # ---- files ----
    def _day_dir(self, d: date) -> Path:
        return self._root / f"date={d.isoformat()}"

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        if self._closed:
            raise RecorderError("recorder is closed")
        try:
            line = encode(payload)
        except JournalError as e:
            raise RecorderError(f"unencodable record: {e}") from e
        try:
            fh = self._files.get(path)
            if fh is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = path.open("a", encoding="utf-8")
                self._files[path] = fh
            fh.write(line + "\n")
            self._pending += 1
            if self._pending >= self._flush_every:
                self.flush()
        except OSError as e:
            raise RecorderError(f"cannot write {path}: {e}") from e
        rel = str(path.relative_to(self._root))
        self.stats.files[rel] = self.stats.files.get(rel, 0) + 1

    def flush(self) -> None:
        try:
            for fh in self._files.values():
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise RecorderError(f"fsync failed: {e}") from e
        self._pending = 0

    # ---- gaps ----
    def _open_gap(self, start: datetime, reason: str) -> None:
        if self._gap_open is None:
            self._gap_open = start
            self._gap_reason = reason

    def _close_gap(self, end: datetime) -> None:
        if self._gap_open is None:
            return
        d = self._gap_open.astimezone(IST).date()
        self._write(self._day_dir(d) / "gaps.jsonl", {"start": self._gap_open, "end": end, "reason": self._gap_reason})
        self.stats.gaps += 1
        self._gap_open = None

    # ---- ingest ----
    def on_message(self, m: FeedMessage) -> None:
        if self._last_msg_ts is not None and m.ts - self._last_msg_ts > self._gap and self._gap_open is None:
            self._open_gap(self._last_msg_ts, f"feed silent > {self._gap.total_seconds()}s")
        if m.kind is MsgKind.DISCONNECT:
            self._open_gap(m.ts, f"disconnect: {m.detail}")
            self._last_msg_ts = m.ts
            return
        if m.kind is MsgKind.RECONNECT:
            self._last_msg_ts = m.ts
            return  # the gap closes on the first data after reconnect
        self._close_gap(m.ts)
        self._last_msg_ts = m.ts
        if m.kind is MsgKind.HEARTBEAT:
            self.stats.heartbeats += 1
            return
        q = m.quote
        if q is None:  # pragma: no cover - guarded in FeedMessage
            raise RecorderError("TICK without quote")
        sig = (q.exchange_ts, q.bid, q.ask, q.ltp)
        last = self._last_tick.get(q.instrument_key)
        if last == sig:
            self.stats.duplicates += 1
            return
        payload = _quote_payload(q)
        if last is not None and q.exchange_ts < last[0]:
            payload["out_of_order"] = True
            self.stats.out_of_order += 1
        else:
            self._last_tick[q.instrument_key] = sig
        d = q.exchange_ts.astimezone(IST).date()
        self._write(self._day_dir(d) / f"instrument={_safe(q.instrument_key)}" / "ticks.jsonl", payload)
        self.stats.ticks_written += 1

    def run(self, feed: Iterable[FeedMessage]) -> RecorderStats:
        for m in feed:
            self.on_message(m)
        return self.stats

    def close(self, *, at: datetime | None = None) -> dict[str, Any]:
        """Close any open gap (at `at`), fsync, close files and write a per-day manifest with SHA-256s."""
        if at is not None:
            self._close_gap(at)
        elif self._gap_open is not None:
            raise RecorderError("an open gap needs an explicit end time at close")
        self.flush()
        for fh in self._files.values():
            fh.close()
        self._closed = True
        manifest: dict[str, Any] = {
            "files": {},
            "stats": {
                "ticks_written": self.stats.ticks_written,
                "duplicates": self.stats.duplicates,
                "out_of_order": self.stats.out_of_order,
                "gaps": self.stats.gaps,
                "heartbeats": self.stats.heartbeats,
            },
        }
        for p in sorted(self._files):
            manifest["files"][str(p.relative_to(self._root))] = {
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "lines": self.stats.files[str(p.relative_to(self._root))],
            }
        try:
            (self._root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        except OSError as e:
            raise RecorderError(f"cannot write manifest: {e}") from e
        return manifest


def read_ticks(path: Path) -> Iterator[tuple[Quote, bool]]:
    """Yield (quote, out_of_order) from a ticks.jsonl file. Malformed lines raise RecorderError."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise RecorderError(f"cannot read {path}: {e}") from e
    for n, line in enumerate(lines, 1):
        try:
            p = decode(line)
            ooo = bool(p.pop("out_of_order", False))
            yield Quote(**p), ooo
        except (JournalError, TypeError, ValueError) as e:
            raise RecorderError(f"{path}:{n}: malformed tick: {e}") from e


def read_gaps(path: Path) -> list[tuple[datetime, datetime, str]]:
    if not path.exists():
        return []
    out: list[tuple[datetime, datetime, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            p = decode(line)
            out.append((p["start"], p["end"], p["reason"]))
        except (JournalError, KeyError) as e:
            raise RecorderError(f"{path}:{n}: malformed gap: {e}") from e
    return out

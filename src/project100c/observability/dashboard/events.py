"""Dashboard events and the in-process event bus (thread-safe, bounded history, blocking subscribe)."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any

from project100c.errors import Project100CError

SIMULATED_LABEL = "SIMULATED"


class DashboardError(Project100CError):
    """Dashboard misuse: bad event, unknown flow edge, invalid kill request."""


class EventKind(StrEnum):
    SESSION = "SESSION"
    TICK = "TICK"
    CHAIN = "CHAIN"
    REGIME = "REGIME"
    STRATEGIES = "STRATEGIES"
    INTENT = "INTENT"
    DECISION = "DECISION"
    ORDER = "ORDER"
    FILL = "FILL"
    POSITION = "POSITION"
    RISK = "RISK"
    KILLS = "KILLS"
    WINDOW = "WINDOW"
    LOG = "LOG"
    DAY_END = "DAY_END"
    ECONOMICS = "ECONOMICS"  # whole-system net-of-everything snapshot (docs/risk/system-economics.md); advisory only
    TONY = "TONY"  # NOW / WHY / NEXT, attention state and Ask-Tony answers (deterministic, tony.py)


@dataclass(frozen=True, slots=True)
class Flow:
    src: str
    dst: str
    kind: (
        str  # tick | clean | regime | budget | intent | approve | reject | order | fill | ack | kill | report | promote
    )


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise DashboardError("naive datetime in a dashboard event")
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, int, bool, float)):
        return v
    raise DashboardError(f"cannot serialise {type(v).__name__} in a dashboard event")


@dataclass(frozen=True, slots=True)
class DashEvent:
    ts: datetime
    kind: EventKind
    data: Mapping[str, Any]
    flows: tuple[Flow, ...] = ()
    seq: int = 0  # assigned by the bus (live) or the replay builder
    simulated: bool = True

    def with_seq(self, seq: int) -> DashEvent:
        return DashEvent(self.ts, self.kind, self.data, self.flows, seq, self.simulated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": _jsonable(self.ts),
            "kind": self.kind.value,
            "simulated": self.simulated,
            "label": SIMULATED_LABEL if self.simulated else "",
            "data": _jsonable(self.data),
            "flows": [[f.src, f.dst, f.kind] for f in self.flows],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"), sort_keys=True)


@dataclass
class EventBus:
    """Publish/subscribe with a bounded history, so late subscribers can catch up via ``after_seq``."""

    history: int = 20000
    _events: deque[DashEvent] = field(init=False)
    _cond: threading.Condition = field(init=False, default_factory=threading.Condition)
    _seq: int = field(init=False, default=0)
    _latest: dict[EventKind, DashEvent] = field(init=False, default_factory=dict)
    closed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._events = deque(maxlen=self.history)

    def publish(self, events: Sequence[DashEvent]) -> list[DashEvent]:
        out: list[DashEvent] = []
        with self._cond:
            for e in events:
                self._seq += 1
                ev = e.with_seq(self._seq)
                self._events.append(ev)
                self._latest[ev.kind] = ev
                out.append(ev)
            self._cond.notify_all()
        return out

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def since(self, after_seq: int, *, timeout: float = 0.0, limit: int = 2000) -> list[DashEvent]:
        """Events with seq > after_seq; blocks up to ``timeout`` seconds if there are none yet."""
        with self._cond:
            if self._seq <= after_seq and timeout > 0 and not self.closed:
                self._cond.wait(timeout)
            return [e for e in self._events if e.seq > after_seq][:limit]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The latest event of each kind (what a freshly opened dashboard needs to paint every panel)."""
        with self._cond:
            return {k.value: e.as_dict() for k, e in sorted(self._latest.items())}

    def tail(self, kind: EventKind, n: int) -> list[DashEvent]:
        """The last ``n`` events of one kind (e.g. the event-log panel on first paint)."""
        with self._cond:
            out = [e for e in reversed(self._events) if e.kind is kind][:n]
        return out[::-1]

    def current_session(self, exclude: frozenset[EventKind] = frozenset()) -> list[DashEvent]:
        """Every event since the latest SESSION event (inclusive), minus ``exclude``: what a dashboard opened
        mid-day needs to draw the day so far (the intraday chart, today's decisions and fills)."""
        with self._cond:
            evs = list(self._events)
        start = next((i for i in range(len(evs) - 1, -1, -1) if evs[i].kind is EventKind.SESSION), None)
        if start is None:
            return []
        return [e for e in evs[start:] if e.kind not in exclude]

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()

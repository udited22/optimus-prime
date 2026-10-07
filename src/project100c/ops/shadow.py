"""Shadow strategies and the replay feed for the host (paper/shadow mode; nothing here places an order).

* `ShadowStrategies` runs the regime classifier (K-11, UNVALIDATED) and every library signal plug-in on each closed
  1-minute bar of the index (with the futures and VIX bars when present), exactly as the paper loop does, and
  records what each plug-in *would* have signalled. It returns no trade intents: every spec is RESEARCH, so no
  strategy may trade, not even on paper, until one passes the validation gates and is promoted
  (docs/research/strategy-hypotheses.md). The
  bridge from a live signal to a sized option order (contract choice from the live chain, the allocator) is not
  built for live quotes; the order path is rehearsed on SYNTHETIC days instead (`paper.loop.PaperLoop`).
* `BarBuilder` turns a stream of quotes (last traded price) into 1-minute bars. Volume is not carried by a
  `Quote`, so bars built from quotes have volume 0: plug-ins and regime features that need futures volume see
  none (a known gap, labelled in the status).
* `ReplayFeed` serves a SYNTHETIC day (seeded, ``synthetic.generate_day``) against any clock: its bars become
  available as their minute closes, and ``poll()`` returns index, futures and VIX quotes along an O-H-L-C path
  inside each minute. It stands in for the market when no broker keys are configured. Every value is SIMULATED.

Market-agnostic: instrument keys are configuration; nothing here names a venue.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from project100c.calendar import ExpiryCalendar
from project100c.kernel.regime_gate import RegimeReading
from project100c.market_types import Bar, Quote
from project100c.regime import EventCalendar, RegimeClassifier, RegimeConfig, RegimeLabel
from project100c.sessions import IST
from project100c.spec.models import StrategySpec
from project100c.strategies.library import PLUGINS, LibraryParams, Session, SignalPlugin
from project100c.synthetic import SyntheticDay

ONE_MIN = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class FeedKeys:
    """The instrument keys of the three series the strategies read (configuration, venue-specific values)."""

    index: str
    vix: str | None = None
    fut: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowSignal:
    at: datetime
    strategy_id: str
    rights: tuple[str, ...]
    reason: str
    regime_tags: tuple[str, ...]
    repeats: int = 1

    def as_json(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "strategy_id": self.strategy_id,
            "rights": list(self.rights),
            "reason": self.reason,
            "regime_tags": list(self.regime_tags),
            "repeats": self.repeats,
            "label": "SHADOW: not traded; every spec is RESEARCH",
        }


class ShadowStrategies:
    """Regime classifier + library plug-ins on closed bars; records signals, submits nothing."""

    def __init__(
        self,
        specs: Sequence[StrategySpec],
        regime: RegimeConfig,
        expiries: ExpiryCalendar,
        *,
        events: EventCalendar | None = None,
        journal: Path | None = None,
        plugins: Mapping[str, type[SignalPlugin]] | None = None,
    ) -> None:
        reg = plugins if plugins is not None else PLUGINS
        self.specs = [s for s in specs if s.id in reg]
        self.params = {s.id: LibraryParams.from_spec(s) for s in self.specs}
        self._cls = {s.id: reg[s.id] for s in self.specs}
        self._regime, self._expiries, self._events = regime, expiries, events
        self.journal = journal
        self.day: date | None = None
        self.bars = 0
        self.signals: list[ShadowSignal] = []
        self.errors: dict[str, str] = {}
        self.regime_tags: tuple[str, ...] = ()
        self.label: RegimeLabel | None = None  # the latest regime label (status and dashboard)
        self.last_bar: Bar | None = None  # the latest closed index bar
        self.last_vix: Decimal | None = None
        self._clf: RegimeClassifier | None = None
        self._plugins: dict[str, SignalPlugin] = {}
        self._sessions: dict[str, Session] = {}
        self._last: dict[str, int] = {}

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.specs)

    def start_day(self, day: date, prev_close: Decimal | None) -> None:
        exps = [e.date for e in self._expiries.expiries_between(day, day + timedelta(days=21))]
        self.day, self.bars, self.signals, self.errors, self.regime_tags = day, 0, [], {}, ()
        self.label, self.last_bar, self.last_vix = None, None, None
        self._clf = RegimeClassifier(self._regime, expiries=self._expiries, events=self._events)
        self._clf.start_session(day, prev_close if prev_close is not None else Decimal(0))
        self._plugins = {sid: cls() for sid, cls in self._cls.items()}
        self._sessions = {sid: Session(day, prev_close) for sid in self._plugins}
        for s in self._sessions.values():
            s.expiry_day = self._expiries.is_expiry_day(day)
            s.expiries = exps
        self._last = {}

    def on_bar(self, idx: Bar, fut: Bar | None = None, vix: Bar | None = None) -> list[ShadowSignal]:
        """One closed index bar (with the same minute's futures and VIX bars when present)."""
        if self._clf is None or self.day != idx.start.astimezone(IST).date():
            raise RuntimeError("start_day() must be called for the bar's day first")
        self.bars += 1
        at = idx.start + ONE_MIN
        label = self._clf.on_bar(
            idx, vix=None if vix is None else vix.close, volume=None if fut is None else fut.volume
        )
        self.regime_tags = tuple(sorted(str(t.value) for t in RegimeReading.from_label(label).tags))
        self.label, self.last_bar = label, idx
        if vix is not None:
            self.last_vix = vix.close
        new: list[ShadowSignal] = []
        for sid, plugin in self._plugins.items():
            s = self._sessions[sid]
            s.idx.append(idx)
            s.fut.append(fut)
            s.vix.append(vix)
            s.label = label
            s.event_day = label.event_day
            if sid in self.errors:
                continue
            try:
                sig = plugin.on_bar(s, self.params[sid].signal)
            except Exception as e:  # a plug-in that needs data the host does not have (e.g. the chain) stands down
                self.errors[sid] = f"{type(e).__name__}: {e}"[:200]
                continue
            if sig is None:
                continue
            rights = tuple(str(r.value) for r in sig.rights)
            last = self._last.get(sid)
            if last is not None and (self.signals[last].rights, self.signals[last].reason) == (rights, sig.reason):
                old = self.signals[last]
                self.signals[last] = ShadowSignal(old.at, sid, rights, sig.reason, old.regime_tags, old.repeats + 1)
                continue
            rec = ShadowSignal(at, sid, rights, sig.reason, self.regime_tags)
            self._last[sid] = len(self.signals)
            self.signals.append(rec)
            new.append(rec)
        if new and self.journal is not None:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as fh:
                for r in new:
                    fh.write(json.dumps(r.as_json(), sort_keys=True) + "\n")
        return new


@dataclass
class BarBuilder:
    """Quotes (last traded price) -> closed 1-minute bars, per instrument. Volume is 0 (not in a Quote)."""

    keys: frozenset[str]
    _open: dict[str, list[Any]] = field(default_factory=dict)  # key -> [start, o, h, l, c]

    def on_quotes(self, quotes: Mapping[str, Quote]) -> list[Bar]:
        closed: list[Bar] = []
        for key, q in quotes.items():
            if key not in self.keys:
                continue
            px = q.ltp if q.ltp is not None else _mid(q)
            if px is None:
                continue
            ts = q.exchange_ts.astimezone(IST)
            start = ts.replace(second=0, microsecond=0)
            cur = self._open.get(key)
            if cur is not None and cur[0] != start:
                if start > cur[0]:
                    closed.append(Bar(key, cur[0], cur[1], cur[2], cur[3], cur[4], 0))
                    cur = None
                else:
                    continue  # an out-of-order quote for a minute already closed
            if cur is None:
                self._open[key] = [start, px, px, px, px]
            else:
                cur[2], cur[3], cur[4] = max(cur[2], px), min(cur[3], px), px
        return closed

    def flush_before(self, now: datetime) -> list[Bar]:
        """Close every bar whose minute ended before ``now`` (a quiet instrument still gets its bar)."""
        out = []
        for key, cur in list(self._open.items()):
            if cur[0] + ONE_MIN <= now:
                out.append(Bar(key, cur[0], cur[1], cur[2], cur[3], cur[4], 0))
                del self._open[key]
        return out


def _mid(q: Quote) -> Decimal | None:
    if q.bid is None or q.ask is None:
        return None
    return (q.bid + q.ask) / 2


@dataclass
class ReplayFeed:
    """A SYNTHETIC day served against a clock: closed bars as their minute ends, and an intrabar quote path."""

    day: SyntheticDay
    clock: Callable[[], datetime]
    keys: FeedKeys
    _served: int = 0

    def __post_init__(self) -> None:
        self._by_start = {
            "index": {b.start: b for b in self.day.index},
            "fut": {b.start: b for b in self.day.fut},
            "vix": {b.start: b for b in self.day.vix},
        }

    @property
    def trading_date(self) -> date:
        return self.day.plan.day

    def healthy(self, now: datetime) -> bool:
        return True

    def closed_bars(self) -> list[tuple[Bar, Bar | None, Bar | None]]:
        """Index bars (with futures and VIX) whose minute has closed and that were not returned before."""
        now = self.clock()
        out = []
        while self._served < len(self.day.index) and self.day.index[self._served].start + ONE_MIN <= now:
            b = self.day.index[self._served]
            out.append((self._as(b, self.keys.index), self._get("fut", b.start), self._get("vix", b.start)))
            self._served += 1
        return out

    def _get(self, series: str, start: datetime) -> Bar | None:
        key = self.keys.fut if series == "fut" else self.keys.vix
        b = self._by_start[series].get(start)
        return None if b is None or key is None else self._as(b, key)

    @staticmethod
    def _as(b: Bar, key: str) -> Bar:
        return Bar(key, b.start, b.open, b.high, b.low, b.close, b.volume, b.oi)

    def poll(self) -> dict[str, Quote]:
        now = self.clock()
        start = now.astimezone(IST).replace(second=0, microsecond=0)
        out: dict[str, Quote] = {}
        for series, key in (("index", self.keys.index), ("fut", self.keys.fut), ("vix", self.keys.vix)):
            b = self._by_start[series].get(start)
            if b is None or key is None:
                continue
            sec = now.astimezone(IST).second
            up = b.close >= b.open
            path = (b.open, b.low if up else b.high, b.high if up else b.low, b.close)
            px = path[min(3, sec // 15)]
            out[key] = Quote(key, now, now, None, None, None, None, px, None, is_option=False)
        return out

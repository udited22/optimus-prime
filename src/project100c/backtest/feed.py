"""Replay feed and point-in-time market view (B-01).

A bar becomes AVAILABLE only after it has closed: ``available_at >= start + interval`` (+ feed latency).
A quote becomes available at or after its exchange timestamp. Constructing an event that is available
earlier raises LookAheadError, and so does any MarketView request for data beyond the simulated clock.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from project100c.errors import BacktestError, LookAheadError
from project100c.market_types import Bar, Quote


@dataclass(frozen=True, slots=True)
class BarEvent:
    bar: Bar
    interval: timedelta
    available_at: datetime

    def __post_init__(self) -> None:
        if self.available_at < self.bar.start + self.interval:
            raise LookAheadError(
                f"bar {self.bar.instrument_key}@{self.bar.start.isoformat()} made available before it closed"
            )

    @property
    def key(self) -> str:
        return self.bar.instrument_key

    @property
    def exchange_start(self) -> datetime:
        return self.bar.start

    @property
    def exchange_end(self) -> datetime:
        return self.bar.start + self.interval


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    quote: Quote
    available_at: datetime

    def __post_init__(self) -> None:
        if self.available_at < self.quote.exchange_ts:
            raise LookAheadError(f"quote {self.quote.instrument_key} made available before its exchange time")

    @property
    def key(self) -> str:
        return self.quote.instrument_key

    @property
    def exchange_start(self) -> datetime:
        return self.quote.exchange_ts

    @property
    def exchange_end(self) -> datetime:
        return self.quote.exchange_ts


Event = BarEvent | QuoteEvent


class ReplayFeed:
    """Events in availability order (ties broken by instrument key, then input order). Deterministic.

    ``decision_keys``: instruments whose bar a strategy decides on (e.g. the NIFTY index). Among events available
    at the same instant, these come AFTER every other instrument, so a decision on the index bar sees the same
    minute's option and futures bars whatever the keys are called. Without it, ties go purely by key, and
    ``"NIFTY-INDEX"`` sorted before the ``"NIFTY|..."`` option keys ('-' < '|'): a strategy deciding on the index
    bar found no option bar for that minute yet. The default (no decision keys) keeps earlier runs reproducible.
    """

    def __init__(self, events: Iterable[Event], *, decision_keys: Iterable[str] = ()) -> None:
        last = frozenset(decision_keys)
        indexed = list(enumerate(events))
        for _, e in indexed:
            if e.available_at.tzinfo is None:
                raise BacktestError("naive available_at")
        indexed.sort(key=lambda ie: (ie[1].available_at, ie[1].key in last, ie[1].key, ie[0]))
        self._events = [e for _, e in indexed]

    @classmethod
    def from_bars(
        cls,
        bars: Iterable[Bar],
        *,
        interval: timedelta,
        feed_latency: timedelta = timedelta(0),
        decision_keys: Iterable[str] = (),
    ) -> ReplayFeed:
        if feed_latency < timedelta(0):
            raise BacktestError("feed_latency must be >= 0")
        return cls(
            (BarEvent(b, interval, b.start + interval + feed_latency) for b in bars), decision_keys=decision_keys
        )

    @classmethod
    def from_quotes(cls, quotes: Iterable[Quote], *, feed_latency: timedelta = timedelta(0)) -> ReplayFeed:
        return cls(QuoteEvent(q, max(q.receive_ts, q.exchange_ts + feed_latency)) for q in quotes)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)


class MarketView:
    """What the strategy may see: only events whose available_at <= now."""

    def __init__(self) -> None:
        self._now: datetime | None = None
        self._bars: dict[str, list[BarEvent]] = defaultdict(list)
        self._quotes: dict[str, Quote] = {}

    @property
    def now(self) -> datetime:
        if self._now is None:
            raise BacktestError("view has no time yet")
        return self._now

    def _advance(self, now: datetime) -> None:
        if self._now is not None and now < self._now:
            raise BacktestError(f"clock went backwards: {now.isoformat()} < {self._now.isoformat()}")
        self._now = now

    def _add(self, e: Event) -> None:
        if e.available_at > self.now:
            raise LookAheadError("event added before it is available")
        if isinstance(e, BarEvent):
            self._bars[e.key].append(e)
        else:
            self._quotes[e.key] = e.quote

    def bars(self, key: str, n: int | None = None) -> Sequence[Bar]:
        evs = self._bars.get(key, [])
        sel = evs if n is None else evs[-n:]
        return tuple(e.bar for e in sel)

    def last_bar(self, key: str) -> Bar | None:
        evs = self._bars.get(key)
        return evs[-1].bar if evs else None

    def last_quote(self, key: str) -> Quote | None:
        return self._quotes.get(key)

    def bars_between(self, key: str, start: datetime, end: datetime) -> Sequence[Bar]:
        """Bars with start in [start, end). Asking for a range that ends after ``now`` is look-ahead."""
        if end > self.now:
            raise LookAheadError(f"requested bars up to {end.isoformat()} at {self.now.isoformat()}")
        return tuple(e.bar for e in self._bars.get(key, []) if start <= e.bar.start < end and e.exchange_end <= end)

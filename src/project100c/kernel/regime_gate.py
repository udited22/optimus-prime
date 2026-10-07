"""The Governor's regime gate (docs/risk/risk-engine.md §9.2 check 17; K-11 + StrategySpec regime policy).

The policy comes from the registered StrategySpec, keyed by strategy id, never from the intent itself (a strategy
cannot vouch for its own regime). The reading comes from the K-11 classifier through the MarketSnapshot.

docs/research/validation.md §13.5: an UNVALIDATED classifier means NO_EDGE for live money. For CANARY / PRODUCTION
intents an
unvalidated reading is replaced by {NO_EDGE}, which every spec prohibits, so live entries are blocked until the
classifier is validated. SHADOW (simulate-only) intents see the labels as they are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from project100c.regime import RegimeLabel
from project100c.spec.models import Lifecycle, Regime, StrategySpec
from project100c.spec.regime_policy import RegimeCheck, RegimePolicy, regime_permits

_LIVE = frozenset({Lifecycle.CANARY, Lifecycle.PRODUCTION})
REGIME_STALE = "REGIME_STALE"


@dataclass(frozen=True, slots=True)
class RegimeReading:
    tags: frozenset[Regime]
    as_of: datetime  # when the label became usable (the classified bar's end)
    classifier: str
    validated: bool

    @classmethod
    def from_label(cls, label: RegimeLabel) -> RegimeReading:
        return cls(label.tags(), label.ts, label.version, label.validated)


class RegimeGate:
    def __init__(self, policies: Iterable[RegimePolicy], *, max_age: timedelta = timedelta(seconds=150)) -> None:
        self._p: Mapping[str, RegimePolicy] = {p.strategy_id: p for p in policies}
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self._max_age = max_age

    @classmethod
    def from_specs(cls, specs: Iterable[StrategySpec], *, max_age: timedelta = timedelta(seconds=150)) -> RegimeGate:
        return cls((RegimePolicy.from_spec(s) for s in specs), max_age=max_age)

    def policy(self, strategy_id: str) -> RegimePolicy | None:
        return self._p.get(strategy_id)

    def check(
        self, strategy_id: str, status: Lifecycle, reading: RegimeReading | None, now: datetime
    ) -> list[tuple[str, str]]:
        """Failing (reason code, note) pairs; empty when the regime permits the entry."""
        pol = self._p.get(strategy_id)
        if pol is None:
            return [(RegimeCheck.REGIME_NOT_ALLOWED.value, f"no regime policy registered for {strategy_id}")]
        if reading is None:
            return [(RegimeCheck.REGIME_UNKNOWN.value, "no regime reading")]
        age = now - reading.as_of
        if age < timedelta(0) or age > self._max_age:
            return [(REGIME_STALE, f"reading from {reading.as_of.isoformat()} is {age.total_seconds():.0f}s old")]
        tags = reading.tags
        note = ""
        if not reading.validated and status in _LIVE:
            tags = frozenset({Regime.NO_EDGE})
            note = (
                f"classifier {reading.classifier} is UNVALIDATED: NO_EDGE for live money "
                "(docs/research/validation.md §13.5); "
            )
        v = regime_permits(pol, tags)
        return [] if v.permitted else [(v.check.value, note + v.detail)]

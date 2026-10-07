"""Regime policy: does a strategy's spec permit trading under the current regime tags?
(docs/architecture/strategyspec.md, docs/risk/risk-engine.md).

Pure functions over spec data. The semantics, in order:

1. no tags at all -> REGIME_UNKNOWN (fail closed)
2. any tag in ``prohibited_regimes`` present -> REGIME_BLOCKED (NO_EDGE and ABNORMAL_MARKET are always prohibited)
3. for each *dimension* (trend, volatility, opening; ``REGIME_DIMENSIONS``) that ``eligible_regimes`` names, the
   current value must be one of the named values; a named dimension with no current value (e.g. the opening not
   decided yet) is not allowed -> REGIME_NOT_ALLOWED. Dimensions the spec does not name are unconstrained (AND across
   dimensions, OR within one). Conditions listed in ``eligible_regimes`` are merely permitted.
4. every condition in ``required_regimes`` must be present -> else REGIME_NOT_ALLOWED
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from project100c.spec.models import (
    REGIME_DIMENSIONS,
    InvalidationKind,
    InvalidationRule,
    Regime,
    StrategySpec,
)


class RegimeCheck(StrEnum):
    OK = "OK"
    REGIME_UNKNOWN = "REGIME_UNKNOWN"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    REGIME_NOT_ALLOWED = "REGIME_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class RegimePolicy:
    strategy_id: str
    eligible: frozenset[Regime]
    prohibited: frozenset[Regime]
    required: frozenset[Regime] = frozenset()

    @classmethod
    def from_spec(cls, spec: StrategySpec) -> RegimePolicy:
        return cls(spec.id, spec.eligible_regimes, spec.prohibited_regimes, spec.required_regimes)


@dataclass(frozen=True, slots=True)
class RegimeVerdict:
    check: RegimeCheck
    detail: str = ""

    @property
    def permitted(self) -> bool:
        return self.check is RegimeCheck.OK


def _names(tags: Iterable[Regime]) -> str:
    return "|".join(sorted(t.value for t in tags))


def regime_permits(policy: RegimePolicy, tags: frozenset[Regime]) -> RegimeVerdict:
    if not tags:
        return RegimeVerdict(RegimeCheck.REGIME_UNKNOWN, "no regime tags")
    blocked = tags & policy.prohibited
    if blocked:
        return RegimeVerdict(RegimeCheck.REGIME_BLOCKED, f"prohibited regime present: {_names(blocked)}")
    for dim, values in REGIME_DIMENSIONS.items():
        allowed = policy.eligible & values
        if not allowed:
            continue
        current = tags & values
        if not current:
            return RegimeVerdict(
                RegimeCheck.REGIME_NOT_ALLOWED, f"{dim} not decided; the spec allows {_names(allowed)}"
            )
        if not current & allowed:
            return RegimeVerdict(
                RegimeCheck.REGIME_NOT_ALLOWED, f"{dim} is {_names(current)}; the spec allows {_names(allowed)}"
            )
    missing = policy.required - tags
    if missing:
        return RegimeVerdict(RegimeCheck.REGIME_NOT_ALLOWED, f"required regime absent: {_names(missing)}")
    return RegimeVerdict(RegimeCheck.OK)


def triggered_regime_invalidations(
    rules: Iterable[InvalidationRule], tags: frozenset[Regime]
) -> tuple[InvalidationRule, ...]:
    """The REGIME_CHANGE rules whose regimes are present now (the other kinds are evaluated by the strategy)."""
    return tuple(r for r in rules if r.kind is InvalidationKind.REGIME_CHANGE and r.regimes & tags)

"""Portfolio allocator v1 (S-05; docs/research/strategy-hypotheses.md "Portfolio allocator v1").

A pure function from (strategy records, NAV, the latest regime reading, the day's risk already used) to an
``AllocationPlan``: for each strategy, eligible or refused (with every reason), and a per-trade risk budget in INR.

Rules, in order, for each strategy:

1. lifecycle: LIVE mode (real money) allows CANARY / PRODUCTION / DEGRADED only; SIMULATE mode (paper, shadow,
   research simulations) allows every status except QUARANTINED / RETIRED;
2. not killed, and under both entry caps: the spec's ``entry.max_entries_per_day`` and the system-wide cap
   (OD-014, limits ``max_trades_per_day``);
3. capital: ``dependencies.min_capital_inr`` must be known (LIVE) and at most the NAV;
4. regime: the spec's regime policy against the reading, through the same ``RegimeGate`` the Governor uses. A
   missing or stale reading is refused; in LIVE mode an UNVALIDATED classifier means NO_EDGE, which every spec
   prohibits, so nothing gets live money until the classifier is validated (docs/research/validation.md §13.5);
5. auto-decrease: each rule that fires halves the budget (they compound); slippage at the kill multiple zeroes it;
6. exclusive groups: within a group of mirror-image strategies only the first eligible one gets a budget.

The day's remaining risk (``daily_risk_frac`` x NAV minus what is already used) is shared equally among the
eligible strategies, and each per-trade budget is capped at ``per_trade_frac`` x NAV. The allocator can only lower
risk: the Governor still checks every intent against its own limits.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import ConfigError
from project100c.kernel.regime_gate import RegimeGate, RegimeReading
from project100c.spec.models import Lifecycle, StrategySpec

_ZERO = Decimal(0)
_HALF = Decimal("0.5")
_PAISA = Decimal("0.01")


class AllocationMode(StrEnum):
    LIVE = "LIVE"  # real money (CANARY / PRODUCTION)
    SIMULATE = "SIMULATE"  # paper, shadow and research simulations: no money at risk


LIVE_STATUSES = frozenset({Lifecycle.CANARY, Lifecycle.PRODUCTION, Lifecycle.DEGRADED})
NEVER = frozenset({Lifecycle.QUARANTINED, Lifecycle.RETIRED})


class Refusal(StrEnum):
    STATUS_NOT_ELIGIBLE = "STATUS_NOT_ELIGIBLE"
    STRATEGY_KILLED = "STRATEGY_KILLED"
    CAPITAL_UNKNOWN = "CAPITAL_UNKNOWN"
    CAPITAL_INELIGIBLE = "CAPITAL_INELIGIBLE"
    REGIME_UNKNOWN = "REGIME_UNKNOWN"
    REGIME_STALE = "REGIME_STALE"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    REGIME_NOT_ALLOWED = "REGIME_NOT_ALLOWED"
    SLIPPAGE_BREACH = "SLIPPAGE_BREACH"
    EXCLUSIVE_GROUP = "EXCLUSIVE_GROUP"
    NO_DAILY_RISK_LEFT = "NO_DAILY_RISK_LEFT"
    MAX_ENTRIES_STRATEGY = "MAX_ENTRIES_STRATEGY"  # the spec's entry.max_entries_per_day (OD-014)
    MAX_ENTRIES_BOOK = "MAX_ENTRIES_BOOK"  # the system-wide cap (limits max_trades_per_day, OD-014)


class AllocatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    adopted_on: date
    status: str = "ASSUMED"
    per_trade_frac: Decimal
    daily_risk_frac: Decimal
    max_concurrent_positions: int = Field(ge=1)
    consecutive_losses_halve: int = Field(ge=1)
    drawdown_halve_frac: Decimal
    slippage_halve_multiple: Decimal
    slippage_zero_multiple: Decimal
    degraded_multiplier: Decimal
    exclusive_groups: tuple[tuple[str, ...], ...] = ()

    @field_validator(
        "per_trade_frac",
        "daily_risk_frac",
        "drawdown_halve_frac",
        "slippage_halve_multiple",
        "slippage_zero_multiple",
        "degraded_multiplier",
        mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        if isinstance(v, (bool, float)) or not isinstance(v, (str, int, Decimal)):
            raise ValueError("must be a decimal string or int (floats forbidden)")
        return Decimal(str(v))

    @model_validator(mode="after")
    def _check(self) -> AllocatorConfig:
        if not _ZERO < self.per_trade_frac <= Decimal("0.02"):
            raise ValueError("per_trade_frac must be in (0, 0.02]: the allocator may not exceed OD-005")
        if not self.per_trade_frac <= self.daily_risk_frac < 1:
            raise ValueError("need per_trade_frac <= daily_risk_frac < 1")
        if not 1 < self.slippage_halve_multiple < self.slippage_zero_multiple:
            raise ValueError("need 1 < slippage_halve_multiple < slippage_zero_multiple")
        if not _ZERO < self.degraded_multiplier <= 1 or not _ZERO < self.drawdown_halve_frac < 1:
            raise ValueError("degraded_multiplier must be in (0, 1] and drawdown_halve_frac in (0, 1)")
        seen: set[str] = set()
        for g in self.exclusive_groups:
            if len(g) < 2 or seen & set(g):
                raise ValueError(f"exclusive group {g} needs >= 2 members and may not overlap another group")
            seen |= set(g)
        return self


def load_allocator_config(path: Path, *, version: str | None = None) -> AllocatorConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("allocator")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: no [[allocator]] blocks")
    try:
        all_ = [AllocatorConfig.model_validate(b) for b in raw]
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    if len({c.version for c in all_}) != len(all_):
        raise ConfigError(f"{path}: duplicate allocator versions")
    if version is not None:
        for c in all_:
            if c.version == version:
                return c
        raise ConfigError(f"{path}: allocator version {version} not found")
    return max(all_, key=lambda c: (c.adopted_on, c.version))


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    """What the allocator knows about one strategy today. ``status`` defaults to the spec's."""

    spec: StrategySpec
    status: Lifecycle | None = None
    killed: bool = False
    consecutive_losses: int = 0
    drawdown_frac: Decimal = _ZERO  # the strategy's own drawdown from its P&L peak, as a fraction of NAV
    slippage_multiple: Decimal | None = None  # realised / assumed slippage; None = not measured yet
    entries_today: int = 0  # ENTRY orders this strategy submitted today

    @property
    def strategy_id(self) -> str:
        return self.spec.id

    @property
    def effective_status(self) -> Lifecycle:
        return self.status or self.spec.status


@dataclass(frozen=True, slots=True)
class Allocation:
    strategy_id: str
    status: Lifecycle
    eligible: bool
    risk_budget_inr: Decimal
    multiplier: Decimal
    refusals: tuple[tuple[Refusal, str], ...] = ()
    decreases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "status": self.status.value,
            "eligible": self.eligible,
            "risk_budget_inr": str(self.risk_budget_inr),
            "multiplier": str(self.multiplier),
            "refusals": [{"code": c.value, "note": n} for c, n in self.refusals],
            "decreases": list(self.decreases),
        }


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    as_of: datetime
    mode: AllocationMode
    nav: Decimal
    config_version: str
    regime_tags: tuple[str, ...]
    classifier: str | None
    classifier_validated: bool | None
    daily_risk_left_inr: Decimal
    allocations: tuple[Allocation, ...]
    labels: tuple[str, ...] = field(
        default=("allocator thresholds ASSUMED", "advisory: the Governor checks every intent"),
    )

    def get(self, strategy_id: str) -> Allocation:
        for a in self.allocations:
            if a.strategy_id == strategy_id:
                return a
        raise KeyError(strategy_id)

    @property
    def eligible(self) -> tuple[str, ...]:
        return tuple(a.strategy_id for a in self.allocations if a.eligible)

    @property
    def total_risk_inr(self) -> Decimal:
        return sum((a.risk_budget_inr for a in self.allocations), _ZERO)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "mode": self.mode.value,
            "nav": str(self.nav),
            "config_version": self.config_version,
            "regime_tags": list(self.regime_tags),
            "classifier": self.classifier,
            "classifier_validated": self.classifier_validated,
            "daily_risk_left_inr": str(self.daily_risk_left_inr),
            "eligible": list(self.eligible),
            "total_risk_inr": str(self.total_risk_inr),
            "allocations": [a.to_dict() for a in self.allocations],
            "labels": list(self.labels),
        }


def _floor(x: Decimal) -> Decimal:
    return x.quantize(_PAISA, rounding=ROUND_DOWN)


def allocate(
    records: Sequence[StrategyRecord],
    *,
    nav: Decimal,
    regime: RegimeReading | None,
    now: datetime,
    cfg: AllocatorConfig,
    mode: AllocationMode = AllocationMode.SIMULATE,
    daily_risk_used: Decimal = _ZERO,
    regime_max_age: timedelta = timedelta(seconds=150),
    book_entries_today: int = 0,
    max_entries_per_day: int | None = None,  # the system-wide cap (limits.max_trades_per_day); None = not checked
) -> AllocationPlan:
    """Records are in priority order (it decides exclusive groups). See the module docstring for the rules."""
    if nav <= 0:
        raise ValueError("nav must be positive")
    ids = [r.strategy_id for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate strategy ids")
    gate = RegimeGate.from_specs((r.spec for r in records), max_age=regime_max_age)
    pre: list[tuple[StrategyRecord, list[tuple[Refusal, str]], Decimal, list[str]]] = []
    for r in records:
        st = r.effective_status
        why: list[tuple[Refusal, str]] = []
        if st in NEVER or (mode is AllocationMode.LIVE and st not in LIVE_STATUSES):
            why.append((Refusal.STATUS_NOT_ELIGIBLE, f"{st.value} cannot get {mode.value} risk"))
        if r.killed:
            why.append((Refusal.STRATEGY_KILLED, "a strategy kill is active"))
        own_cap = r.spec.entry.max_entries_per_day
        if r.entries_today >= own_cap:
            why.append((Refusal.MAX_ENTRIES_STRATEGY, f"{r.entries_today} entries today, spec cap {own_cap}"))
        if max_entries_per_day is not None and book_entries_today >= max_entries_per_day:
            note = f"{book_entries_today} entries today, system cap {max_entries_per_day}"
            why.append((Refusal.MAX_ENTRIES_BOOK, note))
        mc = r.spec.dependencies.min_capital_inr
        if not isinstance(mc, Decimal):
            if mode is AllocationMode.LIVE:
                why.append(
                    (
                        Refusal.CAPITAL_UNKNOWN,
                        "min_capital_inr not computed yet (docs/architecture/strategyspec.md rule 4)",
                    )
                )
        elif mc > nav:
            why.append((Refusal.CAPITAL_INELIGIBLE, f"min_capital_inr {mc} > NAV {nav}"))
        # the gate's LIVE semantics (UNVALIDATED -> NO_EDGE) apply to real money only
        gate_status = Lifecycle.CANARY if mode is AllocationMode.LIVE else Lifecycle.PAPER
        for code, note in gate.check(r.strategy_id, gate_status, regime, now):
            why.append((Refusal(code), note))
        mult, dec = Decimal(1), []
        if st is Lifecycle.DEGRADED:
            mult *= cfg.degraded_multiplier
            dec.append(f"DEGRADED x{cfg.degraded_multiplier}")
        if r.consecutive_losses >= cfg.consecutive_losses_halve:
            mult *= _HALF
            dec.append(f"{r.consecutive_losses} consecutive losses x0.5")
        if r.drawdown_frac >= cfg.drawdown_halve_frac:
            mult *= _HALF
            dec.append(f"drawdown {r.drawdown_frac} of NAV x0.5")
        if r.slippage_multiple is not None:
            if r.slippage_multiple >= cfg.slippage_zero_multiple:
                why.append((Refusal.SLIPPAGE_BREACH, f"slippage {r.slippage_multiple}x assumed"))
            elif r.slippage_multiple >= cfg.slippage_halve_multiple:
                mult *= _HALF
                dec.append(f"slippage {r.slippage_multiple}x assumed x0.5")
        pre.append((r, why, mult, dec))
    # exclusive groups: the first otherwise-eligible member (in priority order) keeps its place
    group_of = {sid: g for g in cfg.exclusive_groups for sid in g}
    taken: dict[tuple[str, ...], str] = {}
    for r, why, _, _ in pre:
        g = group_of.get(r.strategy_id)
        if g is None or why:
            continue
        if g in taken:
            why.append((Refusal.EXCLUSIVE_GROUP, f"{taken[g]} already holds the {'/'.join(g)} slot"))
        else:
            taken[g] = r.strategy_id
    left = max(_ZERO, cfg.daily_risk_frac * nav - daily_risk_used)
    n = sum(1 for _, why, _, _ in pre if not why)
    cap = cfg.per_trade_frac * nav
    share = left / n if n else _ZERO
    out: list[Allocation] = []
    for r, why, mult, dec in pre:
        if not why and left <= 0:
            why.append((Refusal.NO_DAILY_RISK_LEFT, f"{daily_risk_used} of {cfg.daily_risk_frac * nav} used"))
        ok = not why
        budget = _floor(min(cap, share) * mult) if ok else _ZERO
        out.append(Allocation(r.strategy_id, r.effective_status, ok, budget, mult if ok else _ZERO,
                              tuple(why), tuple(dec)))  # fmt: skip
    return AllocationPlan(
        as_of=now,
        mode=mode,
        nav=nav,
        config_version=cfg.version,
        regime_tags=tuple(sorted(t.value for t in regime.tags)) if regime else (),
        classifier=regime.classifier if regime else None,
        classifier_validated=regime.validated if regime else None,
        daily_risk_left_inr=_floor(left),
        allocations=tuple(out),
    )


def records_from_specs(specs: Iterable[StrategySpec]) -> list[StrategyRecord]:
    return [StrategyRecord(s) for s in specs]

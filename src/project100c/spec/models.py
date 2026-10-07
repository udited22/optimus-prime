"""StrategySpec (docs/architecture/strategyspec.md) as strict pydantic models.

Hard rules encoded here (a spec violating them cannot be constructed):
- every tunable param carries a range (docs/architecture/strategyspec.md §7.2 rule 2);
- long options only (OD-006): every leg is a BUY of a NIFTY CE/PE; no SELL legs, no spreads;
- entries end no later than 14:00 IST (OD-008, superseding OD-003's 14:45) and the time exit is no later than
  15:00 IST (OD-002);
- orders are LIMIT / SL-LIMIT only (never MARKET or IOC, docs/architecture/execution-engine.md C7);
- evidence, confidence and min_capital are 'pending'/NONE unless produced by the pipeline, and a lifecycle status
  beyond RESEARCH requires the evidence that status depends on.
Numbers are Decimal; floats are rejected (quote them in JSON, or use the YAML loader in spec.io).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from project100c.errors import SpecValidationError

# Hard limits mirrored from the owner decisions; the Risk Governor enforces them independently at runtime.
ORDER_ACTIVITY_START = time(9, 15)  # OD-002
NO_NEW_ENTRY_CUTOFF = time(14, 0)  # OD-008 (supersedes OD-003 14:45)
HARD_FLAT = time(15, 0)  # OD-002

_FROZEN = ConfigDict(frozen=True, extra="forbid")
PENDING: Literal["pending"] = "pending"


def _to_decimal(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("booleans are not numbers")
    if isinstance(v, float):
        raise ValueError("floats are not allowed; quote the number as a string")
    if isinstance(v, (str, int, Decimal)):
        try:
            d = Decimal(str(v))
        except Exception as e:  # decimal.InvalidOperation
            raise ValueError(f"not a number: {v!r}") from e
        if not d.is_finite():
            raise ValueError("number must be finite")
        return d
    raise ValueError(f"unsupported numeric type {type(v).__name__}")


Dec = Annotated[Decimal, BeforeValidator(_to_decimal)]


class Lifecycle(StrEnum):
    RESEARCH = "RESEARCH"
    BACKTESTED = "BACKTESTED"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    PRODUCTION = "PRODUCTION"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


_FORWARD = [
    Lifecycle.RESEARCH,
    Lifecycle.BACKTESTED,
    Lifecycle.VALIDATED,
    Lifecycle.PAPER,
    Lifecycle.SHADOW,
    Lifecycle.CANARY,
    Lifecycle.PRODUCTION,
]
ALLOWED_TRANSITIONS: dict[Lifecycle, frozenset[Lifecycle]] = {
    **{a: frozenset({b, Lifecycle.RESEARCH, Lifecycle.QUARANTINED, Lifecycle.RETIRED}) for a, b in pairwise(_FORWARD)},
    Lifecycle.PRODUCTION: frozenset({Lifecycle.DEGRADED, Lifecycle.QUARANTINED, Lifecycle.RETIRED}),
    Lifecycle.DEGRADED: frozenset({Lifecycle.PRODUCTION, Lifecycle.QUARANTINED, Lifecycle.RETIRED}),
    Lifecycle.QUARANTINED: frozenset({Lifecycle.RESEARCH, Lifecycle.RETIRED}),
    Lifecycle.RETIRED: frozenset(),
}


def check_transition(current: Lifecycle, target: Lifecycle) -> None:
    """Raise SpecValidationError unless current -> target is a legal lifecycle step (no skipping stages)."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise SpecValidationError(f"ILLEGAL_TRANSITION: {current} -> {target}")


class Regime(StrEnum):
    """Regime tags. Three are *dimensions* with exactly one value at a time (``REGIME_DIMENSIONS``): the trend, the
    volatility state and the opening character; the rest are *conditions* that are either present or not."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    MEAN_REVERTING = "MEAN_REVERTING"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    VOLATILITY_COMPRESSION = "VOLATILITY_COMPRESSION"
    VOLATILITY_NORMAL = "VOLATILITY_NORMAL"
    OPENING_DRIVE = "OPENING_DRIVE"
    OPENING_REVERSION = "OPENING_REVERSION"
    GAP_REGIME = "GAP_REGIME"
    EXPIRY_REGIME = "EXPIRY_REGIME"
    EVENT_REGIME = "EVENT_REGIME"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    ABNORMAL_MARKET = "ABNORMAL_MARKET"
    NO_EDGE = "NO_EDGE"


# The dimensional tags: the classifier emits exactly one tag per dimension (the opening character only once it is
# decided). A spec that names any tag of a dimension in eligible_regimes requires the current value of that dimension
# to be one of the named ones; dimensions it does not name are unconstrained.
REGIME_DIMENSIONS: dict[str, frozenset[Regime]] = {
    "trend": frozenset({Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.MEAN_REVERTING}),
    "volatility": frozenset({Regime.VOLATILITY_COMPRESSION, Regime.VOLATILITY_NORMAL, Regime.VOLATILITY_EXPANSION}),
    "opening": frozenset({Regime.OPENING_DRIVE, Regime.OPENING_REVERSION}),
}
REGIME_CONDITIONS = frozenset(Regime) - frozenset().union(*REGIME_DIMENSIONS.values())

# Regimes in which no strategy may trade (docs/research/validation.md: unvalidated classifier => NO_EDGE => do nothing).
MANDATORY_PROHIBITED = frozenset({Regime.NO_EDGE, Regime.ABNORMAL_MARKET})


class ConfidenceLevel(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Param(BaseModel):
    """A tunable parameter: the value under test and the range used by perturbation tests."""

    model_config = _FROZEN
    value: Dec
    min: Dec
    max: Dec
    step: Dec | None = None

    @model_validator(mode="after")
    def _range(self) -> Param:
        if not self.min < self.max:
            raise ValueError(f"param range must have min < max (got {self.min}..{self.max})")
        if not self.min <= self.value <= self.max:
            raise ValueError(f"param value {self.value} outside its range [{self.min}, {self.max}]")
        if self.step is not None and self.step <= 0:
            raise ValueError("step must be > 0")
        return self


class DataAvailability(StrEnum):
    """Whether the project can actually get a dataset (docs/architecture/historical-data.md).
    UNVERIFIED = not checked yet."""

    AVAILABLE = "AVAILABLE"
    UNCERTAIN = "UNCERTAIN"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


class DataRequirement(BaseModel):
    model_config = _FROZEN
    dataset: str = Field(min_length=1)
    granularity: str = Field(pattern=r"^(tick|\d+(s|m|d))$")
    fields: tuple[str, ...] = ()
    point_in_time: bool = True
    availability: DataAvailability = DataAvailability.UNVERIFIED
    substitute: str | None = None  # what the strategy uses instead if the dataset cannot be had

    @model_validator(mode="after")
    def _substitute(self) -> DataRequirement:
        if self.availability in (DataAvailability.UNCERTAIN, DataAvailability.UNAVAILABLE) and not self.substitute:
            raise ValueError(f"{self.dataset}: availability {self.availability} needs a named substitute")
        return self


class InvalidationKind(StrEnum):
    REGIME_CHANGE = "REGIME_CHANGE"  # a listed regime tag appears
    PRICE_LEVEL = "PRICE_LEVEL"  # the underlying crosses a level that kills the thesis (e.g. back inside the range)
    TIME = "TIME"  # the move did not come within the expected time
    VOLATILITY = "VOLATILITY"  # IV / VIX does what the thesis says it must not
    DATA = "DATA"  # a required input goes missing or stale


class InvalidationAction(StrEnum):
    EXIT_POSITION = "EXIT_POSITION"  # sell to close now (a strategy exit, never a short)
    CANCEL_ENTRY = "CANCEL_ENTRY"  # cancel a working entry; no new entry for this setup
    STAND_DOWN_DAY = "STAND_DOWN_DAY"  # no further entries today


class InvalidationRule(BaseModel):
    """A condition that says the thesis is wrong *now*, before the stop is hit (docs/architecture/strategyspec.md)."""

    model_config = _FROZEN
    kind: InvalidationKind
    description: str = Field(min_length=10)
    action: InvalidationAction
    regimes: frozenset[Regime] = frozenset()  # for REGIME_CHANGE: any of these tags triggers the rule

    @model_validator(mode="after")
    def _regimes(self) -> InvalidationRule:
        if self.kind is InvalidationKind.REGIME_CHANGE and not self.regimes:
            raise ValueError("a REGIME_CHANGE invalidation rule must list the regimes that trigger it")
        if self.kind is not InvalidationKind.REGIME_CHANGE and self.regimes:
            raise ValueError("only REGIME_CHANGE invalidation rules list regimes")
        return self


class Signal(BaseModel):
    model_config = _FROZEN
    formula: str = Field(min_length=1)
    params: dict[str, Param] = Field(default_factory=dict)


class LegRight(StrEnum):
    CE = "CE"
    PE = "PE"
    SIGNAL = "SIGNAL"  # CE or PE chosen by the signal direction (e.g. breakout up -> CE, down -> PE)


class Leg(BaseModel):
    """One option leg. OD-006: only BUY legs exist; the literal makes a SELL leg unrepresentable."""

    model_config = _FROZEN
    side: Literal["BUY"]
    right: LegRight
    underlying: Literal["NIFTY"] = "NIFTY"
    instrument: Literal["INDEX_OPTION"] = "INDEX_OPTION"

    @field_validator("side", mode="before")
    @classmethod
    def _long_only(cls, v: Any) -> Any:
        if isinstance(v, str) and v.upper() != "BUY":
            raise ValueError("MANDATE_LONG_ONLY: only BUY legs are allowed (OD-006: no option selling, no spreads)")
        return v


class Selection(BaseModel):
    model_config = _FROZEN
    expiry: str = Field(min_length=1)
    strike: str = Field(min_length=1)
    allow_zero_dte: bool = False


class EntryOrder(BaseModel):
    model_config = _FROZEN
    type: Literal["LIMIT"]
    price_rule: str = Field(min_length=1)
    ttl_seconds: int = Field(ge=1, le=60)
    max_chase_ticks: int = Field(ge=0, le=5)


class Entry(BaseModel):
    model_config = _FROZEN
    trigger: str = Field(min_length=1)
    window_start: time
    window_end: time
    order: EntryOrder
    # the spec's own cap on new entries per day (OD-014: the system-wide cap in limits.toml still applies on top)
    max_entries_per_day: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _window(self) -> Entry:
        if self.window_start < ORDER_ACTIVITY_START:
            raise ValueError(
                f"entry window starts {self.window_start} before order activity start {ORDER_ACTIVITY_START}"
            )
        if self.window_end > NO_NEW_ENTRY_CUTOFF:
            raise ValueError(
                f"ENTRY_AFTER_CUTOFF: entry window ends {self.window_end} after {NO_NEW_ENTRY_CUTOFF} (OD-008)"
            )
        if not self.window_start < self.window_end:
            raise ValueError("empty entry window")
        return self


class StopMethod(StrEnum):
    PREMIUM_PCT = "PREMIUM_PCT"
    PREMIUM_POINTS = "PREMIUM_POINTS"
    UNDERLYING_LEVEL = "UNDERLYING_LEVEL"


class Stop(BaseModel):
    """Every position has a price stop; a time exit alone does not bound the loss for the Governor."""

    model_config = _FROZEN
    method: StopMethod
    value: Dec = Field(gt=0)
    order: Literal["SL_LIMIT"]
    limit_offset_ticks: int = Field(ge=1, le=20)

    @model_validator(mode="after")
    def _pct(self) -> Stop:
        if self.method is StopMethod.PREMIUM_PCT and self.value >= 100:
            raise ValueError("a premium stop of >= 100% is not a stop")
        return self


class Exit(BaseModel):
    model_config = _FROZEN
    stop: Stop
    profit_taking: str = Field(min_length=1)
    time_exit: time
    max_holding_minutes: int = Field(ge=1, le=345)

    @field_validator("time_exit")
    @classmethod
    def _flat(cls, v: time) -> time:
        if v > HARD_FLAT:
            raise ValueError(f"TIME_EXIT_AFTER_HARD_FLAT: {v} > {HARD_FLAT} (OD-002)")
        return v


class Sizing(BaseModel):
    model_config = _FROZEN
    method: str = Field(min_length=1)
    max_lots: int = Field(ge=1, le=1)  # canary and v1: one lot (docs/risk/risk-engine.md)
    risk_at_stop_formula: str = Field(min_length=1)


class SlippageSource(StrEnum):
    MEASURED = "measured"
    ASSUMED = "assumed"


class ExpectedSlippage(BaseModel):
    model_config = _FROZEN
    entry_ticks: int = Field(ge=0)
    exit_ticks: int = Field(ge=0)
    stop_ticks: int = Field(ge=0)
    source: SlippageSource


class CostAssumptions(BaseModel):
    model_config = _FROZEN
    cost_model_version: str = Field(pattern=r"^CM-\d{4}-\d{2}-\d{2}$")
    brokerage_plan: str = Field(min_length=1)


class Dependencies(BaseModel):
    model_config = _FROZEN
    weekly_expiry: bool
    # computed by the pipeline (docs/architecture/strategyspec.md rule 4); never authored
    min_capital_inr: Dec | Literal["pending"] = PENDING


class EvidenceRef(BaseModel):
    """Pointer to a pipeline artifact; produced_by must be a pipeline/validation identity."""

    model_config = _FROZEN
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    produced_by: str = Field(min_length=1)
    run_ids: tuple[str, ...] = ()


EvidenceSlot = EvidenceRef | Literal["pending"]


class Evidence(BaseModel):
    model_config = _FROZEN
    backtest_dataset: EvidenceSlot = PENDING
    in_sample: EvidenceSlot = PENDING
    out_of_sample: EvidenceSlot = PENDING
    walk_forward: EvidenceSlot = PENDING
    holdout: EvidenceSlot = PENDING
    stress: EvidenceSlot = PENDING
    paper: EvidenceSlot = PENDING
    shadow: EvidenceSlot = PENDING
    live: EvidenceSlot = PENDING

    def is_pending(self) -> bool:
        return all(getattr(self, f) == PENDING for f in type(self).model_fields)

    def missing(self, fields: tuple[str, ...]) -> list[str]:
        return [f for f in fields if getattr(self, f) == PENDING]


# Evidence a status requires (cumulative).
_BACKTESTED = ("backtest_dataset", "in_sample")
_VALIDATED = (*_BACKTESTED, "out_of_sample", "walk_forward", "holdout", "stress")
_PAPER = _VALIDATED
_SHADOW = (*_VALIDATED, "paper")
_CANARY = (*_SHADOW, "shadow")
_PRODUCTION = (*_CANARY, "live")
REQUIRED_EVIDENCE: dict[Lifecycle, tuple[str, ...]] = {
    Lifecycle.RESEARCH: (),
    Lifecycle.BACKTESTED: _BACKTESTED,
    Lifecycle.VALIDATED: _VALIDATED,
    Lifecycle.PAPER: _PAPER,
    Lifecycle.SHADOW: _SHADOW,
    Lifecycle.CANARY: _CANARY,
    Lifecycle.PRODUCTION: _PRODUCTION,
    Lifecycle.DEGRADED: _PRODUCTION,
    Lifecycle.QUARANTINED: (),
    Lifecycle.RETIRED: (),
}
_NEEDS_CONFIDENCE = frozenset(
    {Lifecycle.VALIDATED, Lifecycle.PAPER, Lifecycle.SHADOW, Lifecycle.CANARY, Lifecycle.PRODUCTION, Lifecycle.DEGRADED}
)


class Confidence(BaseModel):
    model_config = _FROZEN
    level: ConfidenceLevel = ConfidenceLevel.NONE
    rationale: str = "no evidence yet"


class Lineage(BaseModel):
    model_config = _FROZEN
    code_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    data_manifest: str | None = None
    experiment_ids: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()


# H17 scalping (docs/research/strategy-hypotheses.md): capped at 1 entry a day until the cost-drag study
# (docs/research/cost-drag-study.md) shows
# its costs are justified at the current NAV. Lifting it is a code change with the owner's approval, never a spec edit.
# Event-day certification (OD-014 default, 2-Oct-2026; validation/event_cert.py). A strategy may enter on an
# EVENT_REGIME day only with event_certified: true, which needs this record: the id of a saved gate result in which
# the V1-V18 gates gave VALIDATED (REAL data only) on the strategy's historical event-day trades.
EVENT_CERT_SUBSET = "HISTORICAL_EVENT_DAYS"


class EventCertification(BaseModel):
    model_config = _FROZEN

    gate_result_id: str = Field(pattern=r"^VR-[0-9a-f]{12}$")  # configs/validation/event_certifications/<id>.json
    gate_config_version: str = Field(min_length=1)
    subset: Literal["HISTORICAL_EVENT_DAYS"]
    data_label: Literal["REAL"]
    verdict: Literal["VALIDATED"]
    event_days: int = Field(ge=1)
    certified_on: date


SCALPING_ID_PREFIX = "S-SCALP-"
SCALPING_MAX_ENTRIES_PER_DAY = 1


class StrategySpec(BaseModel):
    model_config = _FROZEN

    id: str = Field(pattern=r"^[SF]-[A-Z0-9]+-\d{3}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Lifecycle = Lifecycle.RESEARCH
    owner_agent: str = Field(min_length=1)
    created: date
    hypothesis: str = Field(min_length=20)
    economic_rationale: str = Field(min_length=20)
    required_data: tuple[DataRequirement, ...] = Field(min_length=1)
    signal: Signal
    legs: tuple[Leg, ...] = Field(min_length=1, max_length=2)
    # Regime policy (docs/architecture/strategyspec.md §7.x). eligible: for each *dimension* it names
    # (REGIME_DIMENSIONS), the current value
    # must be one of the named ones; conditions listed here are merely permitted. prohibited: any present tag blocks.
    # required: conditions that must all be present (e.g. EXPIRY_REGIME for an expiry-day strategy).
    eligible_regimes: frozenset[Regime] = Field(min_length=1)
    prohibited_regimes: frozenset[Regime]
    required_regimes: frozenset[Regime] = frozenset()
    invalidation_rules: tuple[InvalidationRule, ...] = Field(min_length=1)
    selection: Selection
    entry: Entry
    exit: Exit
    sizing: Sizing
    expected_frequency: str = Field(min_length=1)
    expected_slippage: ExpectedSlippage
    cost_assumptions: CostAssumptions
    capacity: str = Field(min_length=1)
    failure_modes: tuple[str, ...] = Field(min_length=1)
    # pre-registered before any data is examined (docs/research/validation.md §13.3): what result would show the
    # hypothesis is false
    falsification_criteria: tuple[str, ...] = Field(min_length=1)
    # how the edge (if any) depends on costs: e.g. the break-even move in premium points at the assumed slippage
    cost_sensitivity: str = Field(min_length=20)
    dependencies: Dependencies
    evidence: Evidence = Field(default_factory=Evidence)
    confidence: Confidence = Field(default_factory=Confidence)
    retirement_criteria: tuple[str, ...] = Field(min_length=1)
    lineage: Lineage = Field(default_factory=Lineage)
    # May this strategy enter on an EVENT_REGIME day? Only with a passing event-day gate result on record.
    event_certified: bool = False
    event_certification: EventCertification | None = None

    @model_validator(mode="after")
    def _rules(self) -> StrategySpec:
        overlap = self.eligible_regimes & self.prohibited_regimes
        if overlap:
            raise ValueError(f"regimes both eligible and prohibited: {sorted(overlap)}")
        missing_mandatory = MANDATORY_PROHIBITED - self.prohibited_regimes
        if missing_mandatory:
            raise ValueError(f"prohibited_regimes must include {sorted(missing_mandatory)}")
        bad_req = self.required_regimes - REGIME_CONDITIONS
        if bad_req:
            raise ValueError(f"required_regimes must be conditions, not dimension values: {sorted(bad_req)}")
        req_blocked = self.required_regimes & self.prohibited_regimes
        if req_blocked:
            raise ValueError(f"regimes both required and prohibited: {sorted(req_blocked)}")
        if len(self.legs) == 2:
            # Two BUY legs (e.g. a long straddle/strangle) are allowed; duplicate identical legs are not.
            rights = {leg.right for leg in self.legs}
            if rights != {LegRight.CE, LegRight.PE}:
                raise ValueError("a two-leg spec must buy one CE and one PE")
        if self.exit.time_exit <= self.entry.window_start:
            raise ValueError("time_exit must be after the entry window start")
        if self.id.startswith(SCALPING_ID_PREFIX) and self.entry.max_entries_per_day > SCALPING_MAX_ENTRIES_PER_DAY:
            raise ValueError(
                f"{self.id}: scalping (H17) is capped at {SCALPING_MAX_ENTRIES_PER_DAY} entry a day until the "
                "cost-drag study shows its costs are justified at the current NAV (docs/research/cost-drag-study.md)"
            )
        if self.event_certified and self.event_certification is None:
            raise ValueError("event_certified: true needs an event_certification record (a passing gate result id)")
        if self.event_certification is not None and not self.event_certified:
            raise ValueError("an event_certification record without event_certified: true is inconsistent")
        need = REQUIRED_EVIDENCE[self.status]
        missing = self.evidence.missing(need)
        if missing:
            raise ValueError(f"EVIDENCE_MISSING: status {self.status} requires evidence {missing}")
        if self.status in _NEEDS_CONFIDENCE and self.confidence.level is ConfidenceLevel.NONE:
            raise ValueError(f"status {self.status} requires a confidence level set by Validation")
        return self

    def spec_hash(self) -> str:
        """SHA-256 of the canonical JSON form; TradeIntents echo this (docs/architecture/execution-engine.md §10.2)."""
        return hashlib.sha256(canonical_json(self).encode()).hexdigest()


def canonical_json(spec: StrategySpec) -> str:
    return json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

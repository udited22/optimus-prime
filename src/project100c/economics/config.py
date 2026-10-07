"""Versioned whole-system economics configuration (configs/economics/economics.toml -> frozen pydantic models).

Rules (as for the cost model): amounts are Decimals parsed from strings (floats rejected), unknown keys are
rejected, and every unverified number carries an explicit ``status`` label that reports must show.
The cost-justification check is advisory by construction: ``advisory_only = false`` is refused at load time.
"""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import ConfigError

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _dec(v: Any) -> Decimal:
    if isinstance(v, bool) or isinstance(v, float):
        raise ValueError("amounts must be quoted strings or ints, not floats/bools")
    if not isinstance(v, (str, int, Decimal)):
        raise ValueError(f"unsupported numeric type {type(v).__name__}")
    d = Decimal(str(v))
    if not d.is_finite():
        raise ValueError("amount must be finite")
    return d


class Status(StrEnum):
    VERIFIED = "VERIFIED"
    ASSUMED = "ASSUMED"
    UNVERIFIED = "UNVERIFIED"


class Billing(StrEnum):
    MONTHLY = "MONTHLY"
    PER_N_DAYS = "PER_N_DAYS"


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"


class FxConfig(BaseModel):
    model_config = _FROZEN
    usd_inr: Decimal
    status: Status
    as_of: date
    note: str = ""

    @field_validator("usd_inr", mode="before")
    @classmethod
    def _p(cls, v: Any) -> Decimal:
        return _dec(v)

    @model_validator(mode="after")
    def _check(self) -> FxConfig:
        if self.usd_inr <= 0:
            raise ValueError("usd_inr must be > 0")
        return self


class FixedCostLine(BaseModel):
    """One fixed running cost. ``amount`` is before GST, in ``currency``, per billing period."""

    model_config = _FROZEN
    line_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    amount: Decimal
    currency: Currency
    billing: Billing
    period_days: int | None = None
    gst_rate: Decimal
    enabled: bool
    status: Status
    sources: tuple[str, ...] = ()
    range_low: Decimal | None = None
    range_high: Decimal | None = None
    brokerage_plan_if_enabled: str | None = None
    note: str = ""
    optimisation: str = Field(min_length=1)

    @field_validator("amount", "gst_rate", "range_low", "range_high", mode="before")
    @classmethod
    def _p(cls, v: Any) -> Decimal | None:
        return None if v is None else _dec(v)

    @model_validator(mode="after")
    def _check(self) -> FixedCostLine:
        if self.amount < 0:
            raise ValueError(f"{self.line_id}: amount must be >= 0")
        if not (Decimal(0) <= self.gst_rate < Decimal(1)):
            raise ValueError(f"{self.line_id}: gst_rate must be in [0, 1)")
        if self.billing is Billing.PER_N_DAYS and (self.period_days is None or self.period_days <= 0):
            raise ValueError(f"{self.line_id}: PER_N_DAYS billing needs period_days > 0")
        if self.billing is Billing.MONTHLY and self.period_days is not None:
            raise ValueError(f"{self.line_id}: MONTHLY billing must not set period_days")
        if (self.range_low is None) != (self.range_high is None):
            raise ValueError(f"{self.line_id}: range_low and range_high go together")
        if self.range_low is not None and self.range_high is not None:
            if not (Decimal(0) <= self.range_low <= self.amount <= self.range_high):
                raise ValueError(f"{self.line_id}: need 0 <= range_low <= amount <= range_high")
        if self.status is Status.VERIFIED and not self.sources:
            raise ValueError(f"{self.line_id}: a VERIFIED line must cite sources")
        return self


class TaxCarryForward(BaseModel):
    model_config = _FROZEN
    status: Status
    note: str = Field(min_length=1)


class TaxAudit(BaseModel):
    model_config = _FROZEN
    status: Status
    turnover_method: str
    turnover_limit_digital: Decimal
    turnover_limit_default: Decimal
    note: str = Field(min_length=1)

    @field_validator("turnover_limit_digital", "turnover_limit_default", mode="before")
    @classmethod
    def _p(cls, v: Any) -> Decimal:
        return _dec(v)

    @model_validator(mode="after")
    def _check(self) -> TaxAudit:
        if self.turnover_method != "SUM_ABS_TRADE_PNL":
            raise ValueError("only turnover_method = SUM_ABS_TRADE_PNL is implemented")
        if not (0 < self.turnover_limit_default <= self.turnover_limit_digital):
            raise ValueError("need 0 < turnover_limit_default <= turnover_limit_digital")
        return self


class TaxConfig(BaseModel):
    model_config = _FROZEN
    status: Status
    treatment: str
    slab_rate: Decimal
    cess_rate: Decimal
    fiscal_year_start_month: int = Field(ge=1, le=12)
    expenses_deductible: bool
    note: str = Field(min_length=1)
    basis: str = Field(min_length=1)  # why this figure is used (OD-017: ASSUMED, owner handles tax at filing)
    loss_carry_forward: TaxCarryForward
    audit: TaxAudit

    @field_validator("slab_rate", "cess_rate", mode="before")
    @classmethod
    def _p(cls, v: Any) -> Decimal:
        return _dec(v)

    @model_validator(mode="after")
    def _check(self) -> TaxConfig:
        if self.treatment != "NON_SPECULATIVE_BUSINESS_INCOME":
            raise ValueError("only NON_SPECULATIVE_BUSINESS_INCOME is implemented")
        if not (Decimal(0) <= self.slab_rate < Decimal(1)) or not (Decimal(0) <= self.cess_rate < Decimal(1)):
            raise ValueError("slab_rate and cess_rate must be in [0, 1)")
        if self.status is not Status.ASSUMED or "ASSUMED" not in self.basis:
            raise ValueError("tax stays ASSUMED and its basis must say so (OD-017)")
        return self

    @property
    def effective_rate(self) -> Decimal:
        """Slab rate including cess (0.30 * 1.04 = 0.312 by default)."""
        return self.slab_rate * (1 + self.cess_rate)


class JustificationRule(StrEnum):
    ALL_MONTHS_BELOW = "ALL_MONTHS_BELOW"


class CostJustificationConfig(BaseModel):
    model_config = _FROZEN
    min_net_return_over_costs: Decimal
    min_net_return_status: Status
    window_months: int = Field(ge=1, le=24)
    rule: JustificationRule
    fixed_cost_nav_threshold: Decimal
    trading_charge_drag_threshold: Decimal
    advisory_only: bool

    @field_validator(
        "min_net_return_over_costs", "fixed_cost_nav_threshold", "trading_charge_drag_threshold", mode="before"
    )
    @classmethod
    def _p(cls, v: Any) -> Decimal:
        return _dec(v)

    @model_validator(mode="after")
    def _check(self) -> CostJustificationConfig:
        if self.advisory_only is not True:
            raise ValueError(
                "cost_justification.advisory_only must be true: the check is advisory and never stops trading (OD-012)"
            )
        if not (Decimal("-1") < self.min_net_return_over_costs < Decimal(1)):
            raise ValueError("min_net_return_over_costs is a monthly fraction in (-1, 1)")
        if not (Decimal(0) < self.fixed_cost_nav_threshold < Decimal(1)):
            raise ValueError("fixed_cost_nav_threshold must be in (0, 1)")
        if not (Decimal(0) < self.trading_charge_drag_threshold <= Decimal(1)):
            raise ValueError("trading_charge_drag_threshold must be in (0, 1]")
        return self


class EconomicsConfig(BaseModel):
    model_config = _FROZEN
    config_version: str = Field(min_length=1)
    effective_from: date
    currency: Currency
    days_per_month: Decimal
    fx: FxConfig
    fixed_cost: tuple[FixedCostLine, ...]
    tax: TaxConfig
    cost_justification: CostJustificationConfig

    @field_validator("days_per_month", mode="before")
    @classmethod
    def _p(cls, v: Any) -> Decimal:
        return _dec(v)

    @model_validator(mode="after")
    def _check(self) -> EconomicsConfig:
        if self.currency is not Currency.INR:
            raise ValueError("reporting currency must be INR")
        if not (Decimal(28) <= self.days_per_month <= Decimal(31)):
            raise ValueError("days_per_month must be in [28, 31]")
        ids = [f.line_id for f in self.fixed_cost]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate fixed_cost line_id")
        if not self.fixed_cost:
            raise ValueError("no fixed_cost lines")
        return self

    def line(self, line_id: str) -> FixedCostLine:
        for f in self.fixed_cost:
            if f.line_id == line_id:
                return f
        raise ConfigError(f"unknown fixed_cost line {line_id!r}")


def load_economics_config(path: Path) -> EconomicsConfig:
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read economics config {path}: {e}") from e
    try:
        return EconomicsConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid economics config {path}: {e}") from e

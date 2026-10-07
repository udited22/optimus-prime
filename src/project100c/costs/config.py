"""Loading and validating the versioned cost configuration (TOML -> frozen pydantic models).

Design rules:
* Rates are Decimals parsed from strings; floats in the config are rejected (no binary rounding error).
* Schedules must not overlap and must be sorted; an open-ended schedule may only be the last one.
* Entries marked ``verified = false`` are refused unless the caller explicitly passes ``allow_unverified=True``.
"""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import ConfigError, NoScheduleForDateError, UnverifiedConfigError

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=False)


def _decimal_from_str(v: Any) -> Decimal:
    if isinstance(v, float):
        raise ValueError("floats are not allowed for rates/amounts; quote the number as a string")
    if isinstance(v, bool):
        raise ValueError("booleans are not numbers")
    if isinstance(v, (str, int, Decimal)):
        d = Decimal(str(v))
        if not d.is_finite():
            raise ValueError("rate must be finite")
        return d
    raise ValueError(f"unsupported numeric type {type(v).__name__}")


class ChargeSchedule(BaseModel):
    """Statutory + exchange charges valid over [effective_from, effective_to]."""

    model_config = _FROZEN

    version: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    stt_sell_rate: Decimal
    stt_exercise_rate: Decimal
    exchange_txn_rate: Decimal
    sebi_fee_per_crore: Decimal
    stamp_buy_rate: Decimal
    gst_rate: Decimal
    verified: bool
    verified_on: date | None = None
    notes: str = ""
    sources: tuple[str, ...] = ()

    @field_validator(
        "stt_sell_rate",
        "stt_exercise_rate",
        "exchange_txn_rate",
        "sebi_fee_per_crore",
        "stamp_buy_rate",
        "gst_rate",
        mode="before",
    )
    @classmethod
    def _parse_decimal(cls, v: Any) -> Decimal:
        return _decimal_from_str(v)

    @model_validator(mode="after")
    def _check(self) -> ChargeSchedule:
        for name in ("stt_sell_rate", "stt_exercise_rate", "exchange_txn_rate", "stamp_buy_rate", "gst_rate"):
            val: Decimal = getattr(self, name)
            if not (Decimal(0) <= val < Decimal(1)):
                raise ValueError(f"{name}={val} must be in [0, 1)")
        if self.sebi_fee_per_crore < 0:
            raise ValueError("sebi_fee_per_crore must be >= 0")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to before effective_from")
        return self

    @property
    def sebi_fee_rate(self) -> Decimal:
        return self.sebi_fee_per_crore / Decimal(10_000_000)

    def covers(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d <= self.effective_to)


class ChargeBook(BaseModel):
    model_config = _FROZEN

    book_id: str
    currency: str
    config_version: str
    schedule: tuple[ChargeSchedule, ...]

    @model_validator(mode="after")
    def _no_overlap(self) -> ChargeBook:
        if not self.schedule:
            raise ValueError("charge book has no schedules")
        scheds = self.schedule
        for a, b in pairwise(scheds):
            if a.effective_to is None:
                raise ValueError(f"open-ended schedule {a.version} is not the last one")
            if b.effective_from <= a.effective_to:
                raise ValueError(f"schedules {a.version} and {b.version} overlap or are unsorted")
        versions = [s.version for s in scheds]
        if len(set(versions)) != len(versions):
            raise ValueError("duplicate schedule versions")
        return self

    def schedule_for(self, trade_date: date, *, allow_unverified: bool = False) -> ChargeSchedule:
        for s in self.schedule:
            if s.covers(trade_date):
                if not s.verified and not allow_unverified:
                    raise UnverifiedConfigError(
                        f"schedule {s.version} covering {trade_date} is UNVERIFIED; "
                        "pass allow_unverified=True to use it explicitly"
                    )
                return s
        raise NoScheduleForDateError(f"no charge schedule in {self.book_id} covers {trade_date}")


class PlanKind(StrEnum):
    FLAT_PER_ORDER = "flat_per_order"
    ZERO = "zero"
    PCT_WITH_CAP = "pct_with_cap"


class BrokeragePlan(BaseModel):
    model_config = _FROZEN

    plan_id: str = Field(min_length=1)
    kind: PlanKind
    amount: Decimal | None = None  # flat_per_order
    rate: Decimal | None = None  # pct_with_cap
    cap: Decimal | None = None  # pct_with_cap
    exercise_brokerage: Decimal | None = None
    verified: bool
    verified_on: date | None = None
    sources: tuple[str, ...] = ()
    notes: str = ""

    @field_validator("amount", "rate", "cap", "exercise_brokerage", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Decimal | None:
        return None if v is None else _decimal_from_str(v)

    @model_validator(mode="after")
    def _check(self) -> BrokeragePlan:
        if self.kind is PlanKind.FLAT_PER_ORDER:
            if self.amount is None or self.amount < 0:
                raise ValueError(f"{self.plan_id}: flat_per_order needs amount >= 0")
            if self.rate is not None or self.cap is not None:
                raise ValueError(f"{self.plan_id}: flat_per_order must not set rate/cap")
        elif self.kind is PlanKind.ZERO:
            if any(x is not None for x in (self.amount, self.rate, self.cap)):
                raise ValueError(f"{self.plan_id}: zero plan must not set amount/rate/cap")
        else:
            if self.rate is None or self.cap is None or self.rate < 0 or self.cap < 0:
                raise ValueError(f"{self.plan_id}: pct_with_cap needs rate >= 0 and cap >= 0")
            if self.amount is not None:
                raise ValueError(f"{self.plan_id}: pct_with_cap must not set amount")
        return self

    def per_order(self, turnover: Decimal) -> Decimal:
        if self.kind is PlanKind.ZERO:
            return Decimal(0)
        if self.kind is PlanKind.FLAT_PER_ORDER:
            assert self.amount is not None
            return self.amount
        assert self.rate is not None and self.cap is not None
        return min(self.rate * turnover, self.cap)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e


def load_charge_book(path: Path) -> ChargeBook:
    raw = _read_toml(path)
    try:
        return ChargeBook.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid charge book {path}: {e}") from e


def load_brokerage_plans(path: Path) -> dict[str, BrokeragePlan]:
    raw = _read_toml(path)
    plans_raw = raw.get("plan")
    if not isinstance(plans_raw, list) or not plans_raw:
        raise ConfigError(f"{path}: no [[plan]] entries")
    plans: dict[str, BrokeragePlan] = {}
    for p in plans_raw:
        try:
            plan = BrokeragePlan.model_validate(p)
        except ValidationError as e:
            raise ConfigError(f"invalid brokerage plan in {path}: {e}") from e
        if plan.plan_id in plans:
            raise ConfigError(f"duplicate plan_id {plan.plan_id} in {path}")
        plans[plan.plan_id] = plan
    default = raw.get("default_live_plan")
    if default is not None and default not in plans:
        raise ConfigError(f"default_live_plan {default!r} is not a defined plan")
    return plans


def default_live_plan_id(path: Path) -> str:
    raw = _read_toml(path)
    default = raw.get("default_live_plan")
    if not isinstance(default, str):
        raise ConfigError(f"{path}: default_live_plan is not set")
    return default

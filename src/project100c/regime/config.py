"""Versioned regime-classifier config (configs/regime/classifier.toml) and the scheduled-event calendar
(configs/regime/events.toml). Validated strictly; numbers are quoted Decimals in TOML."""

from __future__ import annotations

import datetime as _dt
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project100c.errors import ConfigError

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class TrendConfig(BaseModel):
    model_config = _FROZEN
    adx_period: int = Field(ge=2, le=100)
    adx_enter: Decimal
    adx_exit: Decimal
    slope_bars: int = Field(ge=5, le=240)
    slope_enter_atr: Decimal
    slope_exit_atr: Decimal
    vwap_enter_sigma: Decimal
    vwap_exit_sigma: Decimal
    confirm_bars: int = Field(ge=1, le=60)

    @model_validator(mode="after")
    def _bands(self) -> TrendConfig:
        for enter, exit_, name in (
            (self.adx_enter, self.adx_exit, "adx"),
            (self.slope_enter_atr, self.slope_exit_atr, "slope"),
            (self.vwap_enter_sigma, self.vwap_exit_sigma, "vwap"),
        ):
            if not Decimal(0) < exit_ < enter:
                raise ValueError(f"{name}: hysteresis needs 0 < exit < enter (got exit {exit_}, enter {enter})")
        return self


class VolatilityConfig(BaseModel):
    model_config = _FROZEN
    rv_bars: int = Field(ge=5, le=240)
    rv_compression: Decimal
    rv_expansion: Decimal
    vix_compression: Decimal
    vix_expansion: Decimal
    vix_change_bars: int = Field(ge=1, le=240)
    vix_jump_pct: Decimal = Field(gt=0)
    range_bars: int = Field(ge=5, le=240)
    range_pct_compression: Decimal
    range_pct_expansion: Decimal
    range_min_history: int = Field(ge=1)
    band_pct: Decimal = Field(ge=0, lt=50)
    confirm_bars: int = Field(ge=1, le=60)

    @model_validator(mode="after")
    def _order(self) -> VolatilityConfig:
        if not Decimal(0) < self.rv_compression < self.rv_expansion:
            raise ValueError("need 0 < rv_compression < rv_expansion")
        if not Decimal(0) < self.vix_compression < self.vix_expansion:
            raise ValueError("need 0 < vix_compression < vix_expansion")
        if not Decimal(0) <= self.range_pct_compression < self.range_pct_expansion <= Decimal(100):
            raise ValueError("need 0 <= range_pct_compression < range_pct_expansion <= 100")
        return self


class GapConfig(BaseModel):
    model_config = _FROZEN
    flat_pct: Decimal = Field(gt=0)
    large_pct: Decimal

    @model_validator(mode="after")
    def _order(self) -> GapConfig:
        if not self.flat_pct < self.large_pct:
            raise ValueError("need flat_pct < large_pct")
        return self


class OpeningConfig(BaseModel):
    model_config = _FROZEN
    decide_at_minutes: int = Field(ge=5, le=120)
    drive_close_frac: Decimal = Field(gt=Decimal("0.5"), lt=1)
    drive_max_open_crosses: int = Field(ge=0)
    reversion_min_crosses: int = Field(ge=1)
    reversion_mid_frac: Decimal = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def _order(self) -> OpeningConfig:
        if not self.drive_max_open_crosses < self.reversion_min_crosses:
            raise ValueError("need drive_max_open_crosses < reversion_min_crosses")
        return self


class AbnormalConfig(BaseModel):
    model_config = _FROZEN
    index_move_pct: Decimal = Field(gt=0)
    one_bar_move_pct: Decimal = Field(gt=0)
    vix_level: Decimal = Field(gt=0)


class RegimeConfig(BaseModel):
    model_config = _FROZEN
    version: str = Field(pattern=r"^RC-\d{4}-\d{2}-\d{2}\.\d+$")
    adopted_on: date
    status: Literal["UNVALIDATED", "VALIDATED"]
    bar_minutes: int = Field(ge=1, le=15)
    warmup_bars: int = Field(ge=1, le=120)
    trend: TrendConfig
    volatility: VolatilityConfig
    gap: GapConfig
    opening: OpeningConfig
    abnormal: AbnormalConfig

    @property
    def validated(self) -> bool:
        return self.status == "VALIDATED"


class ScheduledEvent(BaseModel):
    model_config = _FROZEN
    date: date  # the Indian session the event moves (EVENT_REGIME all day)
    kind: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_date: _dt.date | None = None  # the event's own date when it differs (a US release moves the next session)
    provisional: bool = False  # the session was found outside the holiday book's coverage (fixed holidays only)


class EventCalendar(BaseModel):
    """Scheduled events (RBI MPC, Union Budget, election results ...). A listed date sets EVENT_REGIME all day."""

    model_config = _FROZEN
    version: str = Field(min_length=1)
    status: Literal["UNVERIFIED", "VERIFIED"]
    events: tuple[ScheduledEvent, ...] = ()

    def on(self, d: date) -> tuple[ScheduledEvent, ...]:
        return tuple(e for e in self.events if e.date == d)


def _toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def load_regime_config(path: Path, *, version: str | None = None) -> RegimeConfig:
    raw = _toml(path).get("classifier")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: no [[classifier]] blocks")
    try:
        all_ = [RegimeConfig.model_validate(b) for b in raw]
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    if len({c.version for c in all_}) != len(all_):
        raise ConfigError(f"{path}: duplicate classifier versions")
    if version is not None:
        for c in all_:
            if c.version == version:
                return c
        raise ConfigError(f"{path}: classifier version {version} not found")
    return max(all_, key=lambda c: (c.adopted_on, c.version))


def load_event_calendar(path: Path) -> EventCalendar:
    try:
        return EventCalendar.model_validate(_toml(path))
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e

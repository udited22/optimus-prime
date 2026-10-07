"""US market-data configuration (``configs/data/us_market.toml``) and the NYSE session book
(``configs/calendar/nyse_sessions.toml``). Non-secret; numbers are quoted strings, never floats."""

from __future__ import annotations

import tomllib
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import CalendarCoverageError, ConfigError


class UsMarketConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    yahoo_chart_base: str
    cboe_base: str
    constituents_url: str
    alpaca_data_base: str
    allowed_hosts: tuple[str, ...]
    requests_per_second: int = Field(gt=0, le=5)
    requests_per_day: int = Field(gt=0, le=100_000)
    alpaca_requests_per_second: int = Field(gt=0, le=3)  # Basic plan: 200 calls/minute
    http_timeout_seconds: Decimal
    retry_server_max_attempts: int = Field(ge=1, le=10)
    retry_rate_limit_max_attempts: int = Field(ge=1, le=20)
    backoff_base_seconds: Decimal
    backoff_cap_seconds: Decimal
    minute_lookback_days: int = Field(ge=1, le=29)
    minute_window_days: int = Field(ge=1, le=7)
    max_missing_fraction: Decimal
    etfs: tuple[str, ...]
    indices: tuple[str, ...]
    reference_symbol: str
    cboe_series: tuple[str, ...]

    @field_validator(
        "http_timeout_seconds", "backoff_base_seconds", "backoff_cap_seconds", "max_missing_fraction", mode="before"
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        if isinstance(v, float | bool):
            raise ValueError("quote numbers as strings (no floats)")
        d = Decimal(str(v))
        if d <= 0:
            raise ValueError("must be > 0")
        return d

    @model_validator(mode="after")
    def _https(self) -> UsMarketConfig:
        for url in (self.yahoo_chart_base, self.cboe_base, self.constituents_url, self.alpaca_data_base):
            if not url.startswith("https://"):
                raise ValueError(f"{url}: only https is allowed")
            if url.split("/")[2] not in self.allowed_hosts:
                raise ValueError(f"{url}: host not in allowed_hosts")
        if self.reference_symbol not in self.etfs:
            raise ValueError("reference_symbol must be one of etfs")
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_cap_seconds < backoff_base_seconds")
        return self


def _load(path: Path, what: str) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"{what} not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{what} {path}: invalid TOML: {e}") from e


def load_us_config(path: Path) -> UsMarketConfig:
    try:
        return UsMarketConfig.model_validate(_load(path, "US market config"))
    except ValidationError as e:
        raise ConfigError(f"US market config {path}: {e}") from e


class NyseSessions(BaseModel):
    """Holidays and early closes for ``covered_years`` only. Nothing outside them is assumed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    timezone: str
    covered_years: tuple[int, ...] = Field(min_length=1)
    open: time
    close: time
    early_close: time
    holidays: tuple[date, ...]
    early_closes: tuple[date, ...]

    @field_validator("open", "close", "early_close", mode="before")
    @classmethod
    def _time(cls, v: Any) -> time:
        return time.fromisoformat(str(v))

    @model_validator(mode="after")
    def _check(self) -> NyseSessions:
        ZoneInfo(self.timezone)
        for d in (*self.holidays, *self.early_closes):
            if d.year not in self.covered_years:
                raise ValueError(f"{d} outside covered_years")
            if d.weekday() >= 5:
                raise ValueError(f"{d} is a weekend day")
        if set(self.holidays) & set(self.early_closes):
            raise ValueError("a day cannot be both a holiday and an early close")
        if not self.open < self.early_close < self.close:
            raise ValueError("expected open < early_close < close")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def covers(self, d: date) -> bool:
        return d.year in self.covered_years

    def is_trading_day(self, d: date) -> bool:
        if not self.covers(d):
            raise CalendarCoverageError(f"NYSE calendar {self.version} does not cover {d}")
        return d.weekday() < 5 and d not in self.holidays

    def session(self, d: date) -> tuple[datetime, datetime]:
        """Regular session [open, close) of a trading day as aware datetimes in the exchange zone."""
        if not self.is_trading_day(d):
            raise CalendarCoverageError(f"{d} is not an NYSE trading day")
        end = self.early_close if d in self.early_closes else self.close
        return datetime.combine(d, self.open, self.tz), datetime.combine(d, end, self.tz)


def load_nyse_sessions(path: Path) -> NyseSessions:
    try:
        return NyseSessions.model_validate(_load(path, "NYSE sessions"))
    except ValidationError as e:
        raise ConfigError(f"NYSE sessions {path}: {e}") from e

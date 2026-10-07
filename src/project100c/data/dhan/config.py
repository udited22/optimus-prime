"""Non-secret Dhan Data API configuration (configs/data/dhan.toml)."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import ConfigError


class DhanConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    api_base: str
    instrument_list_url: str
    allowed_hosts: tuple[str, ...]
    requests_per_second: int = Field(gt=0, le=5)  # vendor limit 5/s (S29)
    requests_per_day: int = Field(gt=0, le=100_000)  # vendor limit 100k/day (S29)
    http_timeout_seconds: Decimal
    retry_rate_limit_max_attempts: int = Field(ge=1, le=20)
    retry_server_max_attempts: int = Field(ge=1, le=10)
    backoff_base_seconds: Decimal
    backoff_cap_seconds: Decimal
    rolling_option_max_window_days: int = Field(gt=0, le=30)
    intraday_max_window_days: int = Field(gt=0, le=90)
    nifty_index_security_id: str
    nifty_strike_step: Decimal
    # SENSEX (BSE), added 3-Oct-2026: index id 51 on IDX_I; options on BSE_FNO take the INDEX id as securityId
    sensex_index_security_id: str
    sensex_strike_step: Decimal

    @field_validator(
        "http_timeout_seconds",
        "backoff_base_seconds",
        "backoff_cap_seconds",
        "nifty_strike_step",
        "sensex_strike_step",
        mode="before",
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
    def _https(self) -> DhanConfig:
        for url in (self.api_base, self.instrument_list_url):
            if not url.startswith("https://"):
                raise ValueError(f"{url}: only https is allowed")
            host = url.split("/")[2]
            if host not in self.allowed_hosts:
                raise ValueError(f"{url}: host {host} not in allowed_hosts")
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_cap_seconds < backoff_base_seconds")
        return self


def load_dhan_config(path: Path) -> DhanConfig:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"Dhan config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML {path}: {e}") from e
    try:
        return DhanConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid Dhan config {path}: {e}") from e

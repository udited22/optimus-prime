"""Non-secret Binance public-data configuration (configs/data/binance.toml). No key exists or is needed."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import ConfigError


class BinanceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    archive_base: str
    listing_base: str
    rest_base: str
    allowed_hosts: tuple[str, ...]
    requests_per_second: int = Field(gt=0, le=10)
    requests_per_day: int = Field(gt=0, le=200_000)
    http_timeout_seconds: Decimal
    retry_server_max_attempts: int = Field(ge=1, le=10)
    retry_rate_limit_max_attempts: int = Field(ge=1, le=20)
    backoff_base_seconds: Decimal
    backoff_cap_seconds: Decimal

    @field_validator("http_timeout_seconds", "backoff_base_seconds", "backoff_cap_seconds", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        if isinstance(v, float | bool):
            raise ValueError("quote numbers as strings (no floats)")
        d = Decimal(str(v))
        if d <= 0:
            raise ValueError("must be > 0")
        return d

    @model_validator(mode="after")
    def _https(self) -> BinanceConfig:
        for url in (self.archive_base, self.listing_base, self.rest_base):
            if not url.startswith("https://"):
                raise ValueError(f"{url}: only https is allowed")
            host = url.split("/")[2]
            if host not in self.allowed_hosts:
                raise ValueError(f"{url}: host {host} not in allowed_hosts")
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_cap_seconds < backoff_base_seconds")
        return self


def load_binance_config(path: Path) -> BinanceConfig:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"Binance config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML {path}: {e}") from e
    try:
        return BinanceConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid Binance config {path}: {e}") from e

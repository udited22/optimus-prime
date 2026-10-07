"""The untouched holdout partition (docs/research/validation.md §13.1): ``configs/validation/holdout.toml``.

``Holdout.contains(day)`` is the only question research code may ask; anything it returns True for must be dropped
before tuning, selecting a regime map or shortlisting. The final evaluation reads the holdout once (V16)."""

from __future__ import annotations

import hashlib
import tomllib
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project100c.errors import ConfigError


class Holdout(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^HD-\d{4}-\d{2}-\d{2}\.\d+$")
    adopted_on: date
    status: Literal["PROPOSED", "APPROVED"]
    recent_from: date
    recent_to: date
    earliest: date
    earlier_week_share: Decimal = Field(ge=0, lt=1)
    selection_salt: str = Field(min_length=1)
    prior_looks: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _order(self) -> Holdout:
        if not self.earliest < self.recent_from <= self.recent_to:
            raise ValueError("need earliest < recent_from <= recent_to")
        return self

    @property
    def earlier_weeks(self) -> frozenset[tuple[int, int]]:
        """The ISO (year, week) pairs drawn into the holdout before ``recent_from``."""
        return _earlier_weeks(self.selection_salt, self.earliest, self.recent_from, self.earlier_week_share)

    def contains(self, d: date) -> bool:
        if self.recent_from <= d <= self.recent_to:
            return True
        if d < self.earliest or d >= self.recent_from:
            return False
        y, w, _ = d.isocalendar()
        return (y, w) in self.earlier_weeks

    def research_days(self, days: list[date]) -> list[date]:
        return [d for d in days if not self.contains(d)]


@lru_cache(maxsize=16)
def _earlier_weeks(salt: str, earliest: date, recent_from: date, share: Decimal) -> frozenset[tuple[int, int]]:
    weeks: list[tuple[int, int]] = []
    d = earliest
    while d < recent_from:
        y, w, _ = d.isocalendar()
        if not weeks or weeks[-1] != (y, w):
            weeks.append((y, w))
        d += timedelta(days=1)

    def rank(yw: tuple[int, int]) -> str:
        return hashlib.sha256(f"{salt}|{yw[0]}-W{yw[1]:02d}".encode()).hexdigest()

    k = int((share * len(weeks)).to_integral_value())
    return frozenset(sorted(weeks, key=rank)[:k])


def load_holdout(path: Path, *, version: str | None = None) -> Holdout:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("holdout")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: no [[holdout]] blocks")
    try:
        all_ = [Holdout.model_validate(b) for b in raw]
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    if version is not None:
        for h in all_:
            if h.version == version:
                return h
        raise ConfigError(f"{path}: holdout version {version} not found")
    return max(all_, key=lambda h: (h.adopted_on, h.version))

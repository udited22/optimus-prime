"""Point-in-time NIFTY lot-size history (D-03/D-04 extension, 2021 onward).

Lot revisions apply per *contract*: existing weekly/monthly contracts normally keep the old lot until expiry, and
pre-existing long-dated contracts switch on a stated end-of-day. The lot therefore depends on the contract's expiry,
its cycle (weekly vs monthly) and the trade date. Resolution rules are documented in
``configs/instruments/nifty_lot_sizes.toml``. Anything outside the documented range raises ``LotSizeHistoryError``;
nothing is extrapolated.
"""

from __future__ import annotations

import tomllib
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project100c.calendar.model import ExpiryCalendar, ExpiryKind
from project100c.errors import ConfigError, LotSizeHistoryError

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class LotRevision(BaseModel):
    model_config = _FROZEN
    id: str = Field(min_length=1)
    old_lot: int = Field(gt=0)
    new_lot: int = Field(gt=0)
    effective_trade_date: date
    last_old_weekly: date
    first_new_weekly: date
    last_old_monthly: date
    first_new_monthly: date
    carryover_cycles: tuple[ExpiryKind, ...] = ()
    carryover_min_expiry: date | None = None
    carryover_until_eod: date | None = None
    verified: bool
    sources: tuple[str, ...] = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> LotRevision:
        if self.old_lot == self.new_lot:
            raise ValueError(f"{self.id}: old_lot == new_lot")
        if not (self.last_old_weekly < self.first_new_weekly and self.last_old_monthly < self.first_new_monthly):
            raise ValueError(f"{self.id}: last-old expiry must precede first-new expiry")
        if self.verified and len(self.sources) < 2:
            raise ValueError(f"{self.id}: verified=true requires >= 2 sources")
        has = [self.carryover_min_expiry is not None, self.carryover_until_eod is not None, bool(self.carryover_cycles)]
        if any(has) and not all(has):
            raise ValueError(f"{self.id}: carryover needs cycles, min_expiry and until_eod together")
        return self

    def last_old(self, kind: ExpiryKind) -> date:
        return self.last_old_weekly if kind is ExpiryKind.WEEKLY else self.last_old_monthly

    def first_new(self, kind: ExpiryKind) -> date:
        return self.first_new_weekly if kind is ExpiryKind.WEEKLY else self.first_new_monthly

    def carried_over(self, expiry: date, kind: ExpiryKind, trade_date: date) -> bool:
        return (
            kind in self.carryover_cycles
            and self.carryover_min_expiry is not None
            and self.carryover_until_eod is not None
            and expiry >= self.carryover_min_expiry
            and trade_date <= self.carryover_until_eod
        )


class LotSizeHistory(BaseModel):
    model_config = _FROZEN
    underlying: str
    version: str = Field(min_length=1)
    coverage_from_expiry: date
    known_through_trade_date: date
    base_lot: int = Field(gt=0)
    base_sources: tuple[str, ...] = Field(min_length=1)
    revisions: tuple[LotRevision, ...]

    @model_validator(mode="after")
    def _chain(self) -> LotSizeHistory:
        lot = self.base_lot
        for r in self.revisions:
            if r.old_lot != lot:
                raise ValueError(f"{r.id}: old_lot {r.old_lot} does not continue the chain (expected {lot})")
            lot = r.new_lot
        for a, b in pairwise(self.revisions):
            if not a.effective_trade_date < b.effective_trade_date:
                raise ValueError(f"revisions {a.id} -> {b.id} are not in date order")
        return self

    @property
    def current_lot(self) -> int:
        return self.revisions[-1].new_lot if self.revisions else self.base_lot

    def lot_size(self, expiry: date, kind: ExpiryKind, trade_date: date, *, allow_unverified: bool = False) -> int:
        """Market lot of the ``kind`` contract expiring ``expiry`` as traded on ``trade_date``."""
        if expiry < self.coverage_from_expiry:
            raise LotSizeHistoryError(
                f"{self.underlying} {expiry}: before lot history coverage {self.coverage_from_expiry}"
            )
        if trade_date > expiry:
            raise LotSizeHistoryError(f"trade date {trade_date} is after expiry {expiry}")
        if trade_date > self.known_through_trade_date:
            raise LotSizeHistoryError(
                f"trade date {trade_date} is after {self.known_through_trade_date}: a later revision may exist "
                "(use the point-in-time instrument master)"
            )
        lot = self.base_lot
        for r in self.revisions:
            if expiry <= r.last_old(kind) or r.carried_over(expiry, kind, trade_date):
                continue  # this contract keeps the pre-revision lot
            if expiry < r.first_new(kind):
                raise LotSizeHistoryError(f"{r.id}: no {kind.value} contract expected between last-old and first-new")
            if trade_date < r.effective_trade_date:
                raise LotSizeHistoryError(
                    f"{r.id}: a {kind.value} contract expiring {expiry} was not listed before {r.effective_trade_date}"
                )
            if not r.verified and not allow_unverified:
                raise LotSizeHistoryError(f"{r.id} is UNVERIFIED; pass allow_unverified=True to use it")
            lot = r.new_lot
        return lot

    def lot_size_for(self, expiries: ExpiryCalendar, expiry: date, trade_date: date) -> int:
        """Convenience: classify ``expiry`` as MONTHLY (last expiry of its month) or WEEKLY via the expiry calendar."""
        m = expiries.monthly(expiry.year, expiry.month, provisional=True)
        kind = ExpiryKind.MONTHLY if m.date == expiry else ExpiryKind.WEEKLY
        return self.lot_size(expiry, kind, trade_date)


def load_lot_history(path: Path) -> LotSizeHistory:
    try:
        with path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    try:
        raw["revisions"] = raw.pop("revision", [])
        return LotSizeHistory.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid lot-size history {path}: {e}") from e

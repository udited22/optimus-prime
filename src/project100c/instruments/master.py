"""Point-in-time instrument master. Nothing about contracts is hard-coded: expiries, lot sizes, tick sizes,
strike grids and weekly/monthly classification are all derived from the loaded file."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from project100c.core_types import OptionRight
from project100c.errors import InstrumentMasterError, LotSizeAmbiguityError, StaleInstrumentMasterError

__all__ = ["Contract", "ExpiryClass", "InstrumentKind", "InstrumentMaster", "OptionRight"]


class InstrumentKind(StrEnum):
    OPTION = "OPTION"
    FUTURE = "FUTURE"


class ExpiryClass(StrEnum):
    MONTHLY = "MONTHLY"  # a future expires on this date
    WEEKLY = "WEEKLY"  # options-only expiry inside the futures horizon
    LONG_DATED = "LONG_DATED"  # options-only expiry beyond the last listed future


@dataclass(frozen=True, slots=True)
class Contract:
    source: str
    instrument_key: str
    exchange_token: str
    underlying: str
    kind: InstrumentKind
    right: OptionRight | None
    expiry: date
    strike: Decimal | None
    lot_size: int
    tick_size: Decimal  # rupees
    freeze_qty: int | None
    trading_symbol: str
    source_weekly_flag: bool | None = None

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise InstrumentMasterError(f"{self.instrument_key}: lot_size must be > 0")
        if self.tick_size <= 0:
            raise InstrumentMasterError(f"{self.instrument_key}: tick_size must be > 0")
        if self.kind is InstrumentKind.OPTION:
            if self.right is None or self.strike is None or self.strike <= 0:
                raise InstrumentMasterError(f"{self.instrument_key}: option needs right and strike > 0")
        elif self.right is not None or (self.strike not in (None, Decimal(0))):
            raise InstrumentMasterError(f"{self.instrument_key}: future must not have right/strike")
        if self.freeze_qty is not None and self.freeze_qty <= 0:
            raise InstrumentMasterError(f"{self.instrument_key}: freeze_qty must be > 0")


class InstrumentMaster:
    """Immutable set of F&O contracts as of a timestamp, from one source file."""

    def __init__(
        self,
        contracts: Iterable[Contract],
        *,
        as_of: datetime,
        source: str,
        source_sha256: str,
        allow_expired: bool = False,
    ) -> None:
        if as_of.tzinfo is None:
            raise InstrumentMasterError("as_of must be timezone-aware")
        cs = tuple(contracts)
        if not cs:
            raise InstrumentMasterError(f"{source}: no contracts")
        keys = Counter(c.instrument_key for c in cs)
        dups = [k for k, n in keys.items() if n > 1]
        if dups:
            raise InstrumentMasterError(f"{source}: duplicate instrument keys {dups[:5]}")
        as_of_date = as_of.date()
        expired = [c for c in cs if c.expiry < as_of_date]
        if expired and not allow_expired:
            raise StaleInstrumentMasterError(
                f"{source}: {len(expired)} contracts expired before as-of {as_of_date} "
                f"(e.g. {expired[0].trading_symbol}); file is stale"
            )
        self._contracts = cs
        self.as_of = as_of
        self.source = source
        self.source_sha256 = source_sha256

    # ---- basic access ----
    @property
    def contracts(self) -> tuple[Contract, ...]:
        return self._contracts

    def __len__(self) -> int:
        return len(self._contracts)

    def by_key(self, instrument_key: str) -> Contract:
        for c in self._contracts:
            if c.instrument_key == instrument_key:
                return c
        raise InstrumentMasterError(f"unknown instrument {instrument_key}")

    def underlyings(self) -> frozenset[str]:
        return frozenset(c.underlying for c in self._contracts)

    def _for(self, underlying: str) -> tuple[Contract, ...]:
        cs = tuple(c for c in self._contracts if c.underlying == underlying)
        if not cs:
            raise InstrumentMasterError(f"no contracts for underlying {underlying!r} in {self.source}")
        return cs

    # ---- derived facts ----
    def option_expiries(self, underlying: str) -> tuple[date, ...]:
        return tuple(sorted({c.expiry for c in self._for(underlying) if c.kind is InstrumentKind.OPTION}))

    def future_expiries(self, underlying: str) -> tuple[date, ...]:
        return tuple(sorted({c.expiry for c in self._for(underlying) if c.kind is InstrumentKind.FUTURE}))

    def classify_expiry(self, underlying: str, expiry: date) -> ExpiryClass:
        opts = self.option_expiries(underlying)
        if expiry not in opts:
            raise InstrumentMasterError(f"{underlying} has no option expiry on {expiry}")
        futs = self.future_expiries(underlying)
        if not futs:
            raise InstrumentMasterError(f"{underlying}: no futures listed; cannot classify expiries")
        if expiry in futs:
            return ExpiryClass.MONTHLY
        if expiry <= max(futs):
            return ExpiryClass.WEEKLY
        return ExpiryClass.LONG_DATED

    def weekly_expiries(self, underlying: str) -> tuple[date, ...]:
        return tuple(
            e for e in self.option_expiries(underlying) if self.classify_expiry(underlying, e) is ExpiryClass.WEEKLY
        )

    def nearest_option_expiry(self, underlying: str, on: date, *, min_days: int = 0) -> date:
        for e in self.option_expiries(underlying):
            if (e - on).days >= min_days:
                return e
        raise InstrumentMasterError(f"{underlying}: no option expiry >= {min_days} days after {on}")

    def lot_size(self, underlying: str, expiry: date | None = None) -> int:
        cs = self._for(underlying)
        if expiry is not None:
            cs = tuple(c for c in cs if c.expiry == expiry)
            if not cs:
                raise InstrumentMasterError(f"{underlying}: no contracts expiring {expiry}")
        sizes = {c.lot_size for c in cs}
        if len(sizes) != 1:
            raise LotSizeAmbiguityError(
                f"{underlying}{'' if expiry is None else ' ' + expiry.isoformat()}: lot sizes {sorted(sizes)}"
            )
        return sizes.pop()

    def lot_sizes_by_expiry(self, underlying: str) -> dict[date, frozenset[int]]:
        out: dict[date, set[int]] = {}
        for c in self._for(underlying):
            out.setdefault(c.expiry, set()).add(c.lot_size)
        return {k: frozenset(v) for k, v in sorted(out.items())}

    def tick_size(self, underlying: str, kind: InstrumentKind) -> Decimal:
        ticks = {c.tick_size for c in self._for(underlying) if c.kind is kind}
        if len(ticks) != 1:
            raise InstrumentMasterError(f"{underlying} {kind}: tick sizes {sorted(ticks)}; not unique")
        return ticks.pop()

    def strikes(self, underlying: str, expiry: date, right: OptionRight) -> tuple[Decimal, ...]:
        return tuple(
            sorted(
                c.strike
                for c in self._for(underlying)
                if c.kind is InstrumentKind.OPTION and c.expiry == expiry and c.right is right and c.strike is not None
            )
        )

    def min_strike_step(self, underlying: str, expiry: date) -> Decimal:
        ks = sorted(
            {c.strike for c in self._for(underlying) if c.expiry == expiry and c.strike is not None and c.strike > 0}
        )
        if len(ks) < 2:
            raise InstrumentMasterError(f"{underlying} {expiry}: fewer than 2 strikes")
        return min(b - a for a, b in pairwise(ks))

    def option(self, underlying: str, expiry: date, strike: Decimal, right: OptionRight) -> Contract:
        hits = [
            c
            for c in self._for(underlying)
            if c.kind is InstrumentKind.OPTION and c.expiry == expiry and c.strike == strike and c.right is right
        ]
        if len(hits) != 1:
            raise InstrumentMasterError(f"{underlying} {expiry} {strike} {right}: {len(hits)} matches")
        return hits[0]

    def weekly_flag_disagreements(self, underlying: str) -> tuple[date, ...]:
        """Expiries where the source's own weekly flag disagrees with our derived classification."""
        bad: set[date] = set()
        for c in self._for(underlying):
            if c.kind is InstrumentKind.OPTION and c.source_weekly_flag is not None:
                derived_weekly = self.classify_expiry(underlying, c.expiry) is ExpiryClass.WEEKLY
                if derived_weekly != c.source_weekly_flag:
                    bad.add(c.expiry)
        return tuple(sorted(bad))

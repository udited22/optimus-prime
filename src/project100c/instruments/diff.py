"""Instrument-master diff detector (backlog D-04): lot/tick/freeze changes, expiry changes, missing next expiry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from project100c.core_types import OptionRight
from project100c.instruments.master import Contract, InstrumentKind, InstrumentMaster


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"  # entries must be disabled until a human/validated process acknowledges it


class ChangeKind(StrEnum):
    LOT_SIZE_CHANGED = "LOT_SIZE_CHANGED"
    LOT_SIZE_SET_CHANGED = "LOT_SIZE_SET_CHANGED"
    TICK_SIZE_CHANGED = "TICK_SIZE_CHANGED"
    FREEZE_QTY_CHANGED = "FREEZE_QTY_CHANGED"
    EXPIRY_ADDED = "EXPIRY_ADDED"
    EXPIRY_REMOVED_AFTER_EXPIRING = "EXPIRY_REMOVED_AFTER_EXPIRING"
    EXPIRY_REMOVED_EARLY = "EXPIRY_REMOVED_EARLY"
    NEXT_EXPIRY_MISSING = "NEXT_EXPIRY_MISSING"
    UNDERLYING_MISSING = "UNDERLYING_MISSING"
    AS_OF_NOT_ADVANCING = "AS_OF_NOT_ADVANCING"


@dataclass(frozen=True, slots=True)
class MasterChange:
    kind: ChangeKind
    severity: Severity
    underlying: str
    detail: str


Identity = tuple[InstrumentKind, date, Decimal | None, OptionRight | None]


def _ident(c: Contract) -> Identity:
    return (c.kind, c.expiry, c.strike, c.right)


def diff_masters(old: InstrumentMaster, new: InstrumentMaster, underlying: str) -> list[MasterChange]:
    changes: list[MasterChange] = []
    if new.as_of <= old.as_of:
        changes.append(
            MasterChange(
                ChangeKind.AS_OF_NOT_ADVANCING,
                Severity.BLOCKING,
                underlying,
                f"new as_of {new.as_of} is not after old {old.as_of}",
            )
        )
    if underlying not in new.underlyings():
        changes.append(MasterChange(ChangeKind.UNDERLYING_MISSING, Severity.BLOCKING, underlying, "absent in new"))
        return changes
    o = {_ident(c): c for c in old.contracts if c.underlying == underlying}
    n = {_ident(c): c for c in new.contracts if c.underlying == underlying}
    lot_changes = tick_changes = freeze_changes = 0
    examples: dict[str, str] = {}
    for k in sorted(set(o) & set(n), key=str):
        a, b = o[k], n[k]
        if a.lot_size != b.lot_size:
            lot_changes += 1
            examples.setdefault("lot", f"{b.trading_symbol}: {a.lot_size}->{b.lot_size}")
        if a.tick_size != b.tick_size:
            tick_changes += 1
            examples.setdefault("tick", f"{b.trading_symbol}: {a.tick_size}->{b.tick_size}")
        if a.freeze_qty != b.freeze_qty:
            freeze_changes += 1
            examples.setdefault("freeze", f"{b.trading_symbol}: {a.freeze_qty}->{b.freeze_qty}")
    if lot_changes:
        changes.append(
            MasterChange(
                ChangeKind.LOT_SIZE_CHANGED,
                Severity.BLOCKING,
                underlying,
                f"{lot_changes} contracts, e.g. {examples['lot']}",
            )
        )
    old_sizes = {c.lot_size for c in o.values()}
    new_sizes = {c.lot_size for c in n.values()}
    if old_sizes != new_sizes:
        changes.append(
            MasterChange(
                ChangeKind.LOT_SIZE_SET_CHANGED,
                Severity.BLOCKING,
                underlying,
                f"lot sizes {sorted(old_sizes)} -> {sorted(new_sizes)}",
            )
        )
    if tick_changes:
        changes.append(
            MasterChange(
                ChangeKind.TICK_SIZE_CHANGED,
                Severity.BLOCKING,
                underlying,
                f"{tick_changes} contracts, e.g. {examples['tick']}",
            )
        )
    if freeze_changes:
        changes.append(
            MasterChange(
                ChangeKind.FREEZE_QTY_CHANGED,
                Severity.WARNING,
                underlying,
                f"{freeze_changes} contracts, e.g. {examples['freeze']}",
            )
        )
    old_exp = {c.expiry for c in o.values() if c.kind is InstrumentKind.OPTION}
    new_exp = {c.expiry for c in n.values() if c.kind is InstrumentKind.OPTION}
    today = new.as_of.date()
    for e in sorted(new_exp - old_exp):
        changes.append(MasterChange(ChangeKind.EXPIRY_ADDED, Severity.INFO, underlying, e.isoformat()))
    for e in sorted(old_exp - new_exp):
        if e < today:
            changes.append(
                MasterChange(ChangeKind.EXPIRY_REMOVED_AFTER_EXPIRING, Severity.INFO, underlying, e.isoformat())
            )
        else:
            changes.append(
                MasterChange(
                    ChangeKind.EXPIRY_REMOVED_EARLY,
                    Severity.BLOCKING,
                    underlying,
                    f"{e.isoformat()} vanished before expiring (as of {today})",
                )
            )
    missing = next_expiry_check(new, underlying)
    if missing is not None:
        changes.append(missing)
    return changes


def next_expiry_check(master: InstrumentMaster, underlying: str, *, max_gap_days: int = 8) -> MasterChange | None:
    """BLOCKING if no option expiry falls within ``max_gap_days`` of the as-of date (a weekly is expected)."""
    today = master.as_of.date()
    upcoming = [e for e in master.option_expiries(underlying) if e >= today]
    if not upcoming or (upcoming[0] - today).days > max_gap_days:
        nxt = upcoming[0].isoformat() if upcoming else "none"
        return MasterChange(
            ChangeKind.NEXT_EXPIRY_MISSING,
            Severity.BLOCKING,
            underlying,
            f"nearest option expiry {nxt} is > {max_gap_days} days after {today}",
        )
    return None

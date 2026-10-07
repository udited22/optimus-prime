"""The trade record the validation toolkit works on, rule eras (docs/research/validation.md §13.1) and an adapter from
library runs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from project100c.backtest.engine import RunResult
from project100c.core_types import OrderSide


class Era(StrEnum):
    MULTI_WEEKLY = "MULTI_WEEKLY"  # before 20-Nov-2024: several weekly expiries, old lot sizes
    SINGLE_WEEKLY_THU = "SINGLE_WEEKLY_THU"  # 20-Nov-2024 to 31-Aug-2025: one weekly index, Thursday expiry
    TUESDAY = "TUESDAY"  # from 1-Sep-2025: Tuesday expiry, lot 65, STT 0.15%, 15:40 close (the most recent era)


def era_of(d: date) -> Era:
    if d < date(2024, 11, 20):
        return Era.MULTI_WEEKLY
    if d <= date(2025, 8, 31):
        return Era.SINGLE_WEEKLY_THU
    return Era.TUESDAY


RECENT_ERA = Era.TUESDAY


@dataclass(frozen=True, slots=True)
class Trade:
    """One closed trade, all money in INR, net = after charges and the slippage already in the fills."""

    day: date
    entry_ts: datetime
    exit_ts: datetime
    net_pnl: Decimal
    charges: Decimal
    slippage: Decimal  # the half-spread paid on both sides (what a 2x slippage stress doubles)
    risk_at_stop: Decimal
    qty: int
    regimes: frozenset[str] = frozenset()
    event_day: bool = False
    exit_reason: str = ""
    oos: bool = True  # out-of-sample (walk-forward test window); in-sample trades are excluded from V5-V15

    @property
    def gross_pnl(self) -> Decimal:
        return self.net_pnl + self.charges

    @property
    def r_multiple(self) -> Decimal:
        return self.net_pnl / self.risk_at_stop if self.risk_at_stop > 0 else Decimal(0)

    @property
    def era(self) -> Era:
        return era_of(self.day)


def trades_from_library_run(
    records: Sequence[Mapping[str, Any]], result: RunResult, *, oos: bool = True
) -> list[Trade]:
    """Closed trades of a ``LongOptionStrategy`` run (its ``.trades`` records plus the engine's fills)."""
    return [t for _, t in paired_trades_from_library_run(records, result, oos=oos)]


def paired_trades_from_library_run(
    records: Sequence[Mapping[str, Any]], result: RunResult, *, oos: bool = True
) -> list[tuple[Mapping[str, Any], Trade]]:
    """As ``trades_from_library_run``, each trade with the record it came from (its signal time, facts, tags), so
    two trades on one day are never matched to the same record."""
    by_key: dict[str, list[Any]] = {}
    for f in sorted(result.fills, key=lambda x: x.ts):
        by_key.setdefault(f.instrument_key, []).append(f)
    out: list[tuple[Mapping[str, Any], Trade]] = []
    used: set[str] = set()
    for t in records:
        if t.get("outcome") != "CLOSED":
            continue
        cash, charges, slip, qty = Decimal(0), Decimal(0), Decimal(0), 0
        first: datetime | None = None
        last: datetime | None = None
        for leg in t["legs_detail"]:
            for f in by_key.get(leg["key"], []):
                if f.order_id in used or f.ts < t["at"]:
                    continue
                used.add(f.order_id)
                sign = -1 if f.side is OrderSide.BUY else 1
                cash += sign * f.price * f.qty - f.charges
                charges += f.charges
                slip += f.half_spread * f.qty
                if f.side is OrderSide.BUY:
                    qty += f.qty
                    first = f.ts if first is None else min(first, f.ts)
                else:
                    last = f.ts if last is None else max(last, f.ts)
        if first is None or last is None:
            continue
        tags = frozenset(str(x) for x in t.get("regime_tags", ()))
        out.append((t, Trade(t["day"], first, last, cash, charges, slip, Decimal(t["risk_at_stop"]), qty, tags,
                             "EVENT_REGIME" in tags, str(t.get("exit_reason") or ""), oos)))  # fmt: skip
    return out


def total(trades: Iterable[Trade]) -> Decimal:
    return sum((t.net_pnl for t in trades), Decimal(0))

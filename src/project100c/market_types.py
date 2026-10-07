"""Market-data records. Prices are Decimal (never float); timestamps must be timezone-aware."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from project100c.errors import DataQualityInputError


def _aware(ts: datetime, name: str) -> None:
    if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise DataQualityInputError(f"{name} must be a timezone-aware datetime")


def _price(v: Decimal | None, name: str, *, optional: bool) -> None:
    if v is None:
        if not optional:
            raise DataQualityInputError(f"{name} is required")
        return
    if not isinstance(v, Decimal):
        raise DataQualityInputError(f"{name} must be Decimal, got {type(v).__name__}")


def _qty(v: int | None, name: str) -> None:
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, int):
        raise DataQualityInputError(f"{name} must be int")


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book snapshot. A missing side is None (one-sided market); zero is a *defect*, not 'missing'."""

    instrument_key: str
    exchange_ts: datetime
    receive_ts: datetime
    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int | None
    ask_qty: int | None
    ltp: Decimal | None
    oi: int | None
    is_option: bool = True

    def __post_init__(self) -> None:
        if not self.instrument_key:
            raise DataQualityInputError("instrument_key required")
        _aware(self.exchange_ts, "exchange_ts")
        _aware(self.receive_ts, "receive_ts")
        for n in ("bid", "ask", "ltp"):
            _price(getattr(self, n), n, optional=True)
        for n in ("bid_qty", "ask_qty", "oi"):
            _qty(getattr(self, n), n)


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV bar identified by its START time."""

    instrument_key: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    oi: int | None = None

    def __post_init__(self) -> None:
        _aware(self.start, "start")
        for n in ("open", "high", "low", "close"):
            _price(getattr(self, n), n, optional=False)
        _qty(self.volume, "volume")
        _qty(self.oi, "oi")

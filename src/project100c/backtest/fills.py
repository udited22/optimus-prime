"""Fill models (B-02). Deliberately CONSERVATIVE; every rule errs against the strategy.

Bar model (OHLCV, no quotes):
* An order may only match bars that START at or after it reached the exchange (``active_at``): intra-bar
  timing is unknown, so the bar in which an order arrives is never used.
* LIMIT BUY fills only on a strict trade-THROUGH (``low < limit``); LIMIT SELL only if ``high > limit``.
  Touching the limit is not enough (queue position unknown). Fill price = the limit (no price improvement).
* SL-LIMIT SELL (a protective stop on a long) triggers when ``low <= trigger``. If the bar OPENS below the
  limit (a gap through the whole band) it is triggered but NOT filled; it then rests as a sell limit and
  needs a later trade-through (``high > limit``). Otherwise it fills at the limit (the worst price allowed).
  SL-LIMIT BUY mirrors this (trigger on ``high >= trigger``; a gap open above the limit leaves it unfilled).
* Quantity is capped at ``participation`` x bar volume, rounded down to whole lots; zero volume -> no fill.

Synthetic spread (optional, ASSUMED; ``backtest/spreads.py``): with a half-spread provider the bar must clear
the limit by the half-spread ``h`` of that bar: BUY needs ``low + h < limit``, SELL needs ``high - h > limit``,
and a triggered stop SELL whose bar opens below ``limit + h`` counts as gapped (BUY stop: ``open - h > limit``).
Without a provider ``h = 0`` (the pure trade-through rule above).

Quote model (top of book): a marketable limit fills at the touch (ask for BUY, bid for SELL) for up to the
displayed size, in whole lots; one-sided or missing books do not fill. SL triggers use LTP or the touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from project100c.backtest.types import SimOrder, SimOrderType
from project100c.core_types import OrderSide
from project100c.errors import BacktestError
from project100c.market_types import Bar, Quote


@dataclass(frozen=True, slots=True)
class LatencyModel:
    order_to_exchange: timedelta = timedelta(milliseconds=300)
    report_to_strategy: timedelta = timedelta(milliseconds=300)

    def __post_init__(self) -> None:
        if self.order_to_exchange < timedelta(0) or self.report_to_strategy < timedelta(0):
            raise BacktestError("latencies must be >= 0")


@dataclass(frozen=True, slots=True)
class Match:
    triggered: bool  # the SL condition was met on this data point (sticky on the order)
    qty: int  # 0 = no fill
    price: Decimal
    ts: datetime
    half_spread: Decimal = Decimal(0)  # the synthetic half-spread the bar had to clear (0 = none)


class HalfSpreadProvider(Protocol):
    def half_spread(self, instrument_key: str, bar: Bar) -> Decimal: ...

    def describe(self) -> dict[str, object]: ...


def _lots(q: Decimal | int, lot: int) -> int:
    return int((Decimal(q) / lot).to_integral_value(rounding=ROUND_FLOOR)) * lot


class BarFillModel:
    def __init__(
        self,
        *,
        interval: timedelta,
        participation: Decimal = Decimal("0.10"),
        spread: HalfSpreadProvider | None = None,
    ) -> None:
        if not (Decimal(0) < participation <= Decimal(1)):
            raise BacktestError("participation must be in (0, 1]")
        self.interval = interval
        self.participation = participation
        self.spread = spread

    def describe(self) -> dict[str, object]:
        d: dict[str, object] = {
            "model": "bar",
            "interval_s": int(self.interval.total_seconds()),
            "participation": self.participation,
        }
        d.update(self.spread.describe() if self.spread is not None else {"spread_model": None})
        return d

    def match(self, order: SimOrder, bar: Bar) -> Match | None:
        """None = bar not eligible for this order (it started before the order was live)."""
        if bar.start < order.active_at:
            return None
        s = order.spec
        end = bar.start + self.interval
        cap = min(order.remaining, _lots(Decimal(bar.volume) * self.participation, s.lot_size))
        lim = s.limit_price
        h = self.spread.half_spread(s.instrument_key, bar) if self.spread is not None else Decimal(0)
        if h < 0:
            raise BacktestError(f"negative half-spread {h} for {s.instrument_key}")
        triggered = order.triggered
        if s.order_type is SimOrderType.SL_LIMIT and not triggered:
            trig = s.trigger_price
            assert trig is not None  # validated in PlaceOrder
            if s.side is OrderSide.SELL and bar.low <= trig:
                if bar.open - h < lim:  # gapped through the band (after the spread): no fill on this bar
                    return Match(True, 0, lim, end, h)
                return Match(True, max(cap, 0), lim, end, h)
            if s.side is OrderSide.BUY and bar.high >= trig:
                if bar.open + h > lim:
                    return Match(True, 0, lim, end, h)
                return Match(True, max(cap, 0), lim, end, h)
            return Match(False, 0, lim, end, h)
        # plain limit, or an already-triggered stop resting as a limit: strict trade-through by more than h
        through = bar.low + h < lim if s.side is OrderSide.BUY else bar.high - h > lim
        return Match(triggered, cap if through and cap > 0 else 0, lim, end, h)


class QuoteFillModel:
    def match(self, order: SimOrder, q: Quote) -> Match | None:
        if q.exchange_ts < order.active_at:
            return None
        s = order.spec
        lim = s.limit_price
        triggered = order.triggered
        if s.order_type is SimOrderType.SL_LIMIT and not triggered:
            trig = s.trigger_price
            assert trig is not None
            if s.side is OrderSide.SELL:
                triggered = (q.ltp is not None and q.ltp <= trig) or (q.bid is not None and q.bid <= trig)
            else:
                triggered = (q.ltp is not None and q.ltp >= trig) or (q.ask is not None and q.ask >= trig)
            if not triggered:
                return Match(False, 0, lim, q.exchange_ts)
        if s.side is OrderSide.BUY:
            if q.ask is None or q.ask_qty is None or q.ask > lim:
                return Match(triggered, 0, lim, q.exchange_ts)
            qty = min(order.remaining, _lots(q.ask_qty, s.lot_size))
            return Match(triggered, qty, q.ask, q.exchange_ts)
        if q.bid is None or q.bid_qty is None or q.bid < lim:
            return Match(triggered, 0, lim, q.exchange_ts)
        qty = min(order.remaining, _lots(q.bid_qty, s.lot_size))
        return Match(triggered, qty, q.bid, q.exchange_ts)

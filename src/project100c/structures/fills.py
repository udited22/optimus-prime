"""Bid-ask spread for the offline structure simulator (OD-019 research; needed for gate V4).

The offline simulator (``scripts/sell_sim.py``) fills a leg at the NEXT minute's open. Without a spread, a buy fills
at that open and a sell at that open: optimistic, so V4 ("realistic fills") failed by construction in SR-2026-10-03.1.
This module adds the SAME synthetic half-spread the bar-based backtest engine uses (``backtest/spreads.py``,
``configs/backtest/synthetic_spreads.toml``, status ASSUMED, nothing measured):

    buy  fill = reference + half_spread + slippage_ticks x tick
    sell fill = max(reference - half_spread - slippage_ticks x tick, tick)

with the half-spread estimated from the leg's premium (the bar HIGH where the caller has it: the conservative
choice, as in the engine), the time of day, the trading days to expiry and the distance from the ATM strike in whole
strike steps. Results already reported without the spread (SR-2026-10-03.1) are NOT re-scored with it; it applies
to runs that declare it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from project100c.backtest.spreads import SyntheticSpreadModel
from project100c.calendar import TradingCalendar


class OptionHalfSpread:
    """Half-spread in rupees per unit for one option leg at one minute."""

    def __init__(self, model: SyntheticSpreadModel, calendar: TradingCalendar, strike_step: float) -> None:
        if strike_step <= 0:
            raise ValueError("strike_step must be > 0")
        self.model, self.cal, self.step = model, calendar, strike_step
        self._dte: dict[tuple[date, date], int] = {}

    @property
    def version(self) -> str:
        return self.model.version

    @property
    def status(self) -> str:
        return self.model.status

    def dte(self, today: date, expiry: date) -> int:
        k = (today, expiry)
        if k not in self._dte:
            if today > expiry:
                raise ValueError(f"{today} is after the expiry {expiry}")
            self._dte[k] = len(self.cal.trading_days(today, expiry)) - 1
        return self._dte[k]

    def __call__(self, premium: float, at: datetime, expiry: date, strike: float, spot: float) -> float:
        p = Decimal(str(round(max(premium, 0.05), 2)))
        steps = int((Decimal(str(abs(strike - spot))) / Decimal(str(self.step))).to_integral_value(ROUND_HALF_UP))
        est = self.model.estimate(p, at, self.dte(at.date(), expiry), steps)
        return float(est.half_spread)

    def describe(self) -> dict[str, str]:
        return {"spread_model": self.version, "spread_status": self.status}


def fill_price(reference: float, buy: bool, ticks: int, tick: float, half_spread: float = 0.0) -> float:
    """Fill against a reference price: a buy pays the half-spread and the slippage ticks, a sell gives them up
    (never below one tick)."""
    if half_spread < 0 or ticks < 0:
        raise ValueError("half_spread and ticks must be >= 0")
    adj = half_spread + ticks * tick
    return reference + adj if buy else max(reference - adj, tick)

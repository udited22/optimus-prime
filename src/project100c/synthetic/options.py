"""SYNTHETIC NIFTY option chain bars priced off a synthetic index day (for strategy-mechanics tests only).

Each 1-minute bar of each strike is priced with Black-Scholes on the index bar's open / high / low / close, with
India VIX as the at-the-money volatility plus a simple ASSUMED smile. Prices are not market prices: there is no
skew dynamics, no IV crush, no microstructure. Volume is a constant SYNTHETIC figure so the bar fill model's
participation cap does not bind. Contracts are keyed ``SYNTH|NIFTY<DDMONYY><strike><CE|PE>``; the key sorts before
the index/futures keys, so in a ReplayFeed a minute's option bars are visible before that minute's index bar.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from project100c.backtest.types import OptionContract
from project100c.core_types import OptionRight
from project100c.market_types import Bar
from project100c.sessions.model import IST
from project100c.synthetic.intraday import SyntheticDay

STRIKE_STEP = 50
LOT_SIZE = 65  # NIFTY lot since 28-Oct-2025 EOD (configs/instruments/nifty_lot_sizes.toml)
_TICK = Decimal("0.05")
_RATE = 0.065  # ASSUMED risk-free rate
_MIN_PER_YEAR = 365 * 24 * 60
OPTION_VOLUME = 20_000  # SYNTHETIC contracts per minute


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, call: bool) -> float:
    t = max(t_years, 1.0 / _MIN_PER_YEAR)
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (_RATE + 0.5 * vol * vol) * t) / sd
    d2 = d1 - sd
    if call:
        return spot * _ncdf(d1) - strike * math.exp(-_RATE * t) * _ncdf(d2)
    return strike * math.exp(-_RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def _q(x: float) -> Decimal:
    v = (Decimal(repr(max(x, 0.0))) / _TICK).quantize(Decimal(1), rounding=ROUND_HALF_UP) * _TICK
    return max(_TICK, v)


def option_key(expiry: date, strike: int, right: OptionRight) -> str:
    return f"SYNTH|NIFTY{expiry:%d%b%y}{strike}{right.value}".upper()


@dataclass(frozen=True, slots=True)
class SyntheticChain:
    contracts: dict[str, OptionContract]
    bars: tuple[Bar, ...]
    notes: tuple[str, ...] = (
        "SYNTHETIC: Black-Scholes on synthetic index bars, India VIX as ATM vol plus an ASSUMED smile; not market data",
    )


def _iv(vix: float, spot: float, strike: int, smile: float) -> float:
    return vix / 100 * (1 + smile * abs(strike - spot) / spot)


def generate_chain(
    day: SyntheticDay,
    expiries: Sequence[date],
    *,
    strikes_each_side: int = 12,
    step: int = STRIKE_STEP,
    lot_size: int = LOT_SIZE,
    smile: float = 1.8,
) -> SyntheticChain:
    """Bars for ATM +/- ``strikes_each_side`` strikes around the day's opening price, for each live expiry."""
    if not day.index:
        return SyntheticChain({}, ())
    d = day.plan.day
    live = [e for e in expiries if e >= d]
    open_px = float(day.index[0].open)
    atm = round(open_px / step) * step
    strikes = [atm + k * step for k in range(-strikes_each_side, strikes_each_side + 1)]
    vix_at = {b.start: float(b.close) for b in day.vix}
    contracts: dict[str, OptionContract] = {}
    bars: list[Bar] = []
    for e in live:
        close = datetime.combine(e, time(15, 30), IST)
        for k in strikes:
            for right in (OptionRight.CE, OptionRight.PE):
                key = option_key(e, k, right)
                contracts[key] = OptionContract(key, "NIFTY", e, Decimal(k), right, lot_size)
                call = right is OptionRight.CE
                for b in day.index:
                    end = b.start + timedelta(minutes=1)
                    t0 = (close - b.start).total_seconds() / 60 / _MIN_PER_YEAR
                    t1 = (close - end).total_seconds() / 60 / _MIN_PER_YEAR
                    vol = _iv(vix_at.get(b.start, 13.0), float(b.close), k, smile)
                    o = bs_price(float(b.open), k, t0, vol, call)
                    c = bs_price(float(b.close), k, t1, vol, call)
                    x_hi = bs_price(float(b.high), k, t1, vol, call)
                    x_lo = bs_price(float(b.low), k, t1, vol, call)
                    hi, lo = max(o, c, x_hi, x_lo), min(o, c, x_hi, x_lo)
                    bars.append(Bar(key, b.start, _q(o), _q(hi), _q(lo), _q(c), OPTION_VOLUME))
    return SyntheticChain(contracts, tuple(bars))


def merge_chains(chains: Iterable[SyntheticChain]) -> SyntheticChain:
    contracts: dict[str, OptionContract] = {}
    bars: list[Bar] = []
    for ch in chains:
        contracts.update(ch.contracts)
        bars.extend(ch.bars)
    return SyntheticChain(contracts, tuple(bars))

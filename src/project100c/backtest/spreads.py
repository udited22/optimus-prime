"""Synthetic bid-ask spread model for bar-based fills (backlog B-02). STATUS: ASSUMED.

OHLCV bars carry trade prices, not quotes, so a bar that trades through a limit by a few paise says little about
whether a resting order would really have been hit: the prints may have been on the other side of the book. Until our
own tick recorder (D-10) measures real NIFTY option spreads, this module charges a deliberately wide, parameterised,
*assumed* half-spread (``configs/backtest/synthetic_spreads.toml``) that a bar must clear beyond the limit before the
fill model accepts a fill. Every number is a placeholder; the loader only accepts ``status = "ASSUMED"`` and the
version plus label are written into each run's ledger.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.backtest.types import OptionContract
from project100c.calendar.model import TradingCalendar
from project100c.errors import ConfigError, SpreadModelError
from project100c.market_types import Bar
from project100c.sessions.model import IST

SPREAD_STATUS_ASSUMED = "ASSUMED"
_FROZEN = ConfigDict(frozen=True, extra="forbid")
_SESSION_OPEN, _SESSION_CLOSE = time(9, 15), time(15, 30)


def _dec(v: Any) -> Decimal:
    if isinstance(v, float):
        raise ValueError("use a string for decimals, not a float")
    return Decimal(str(v))


class _Mult(BaseModel):
    model_config = _FROZEN
    multiplier: Decimal

    @field_validator("multiplier", mode="before")
    @classmethod
    def _m(cls, v: Any) -> Decimal:
        d = _dec(v)
        if d < 1:
            raise ValueError("multipliers must be >= 1 (a band may widen, never narrow, the base spread)")
        return d


class TimeBand(_Mult):
    start: time
    end: time


class DteBand(_Mult):
    max_days: int | None = Field(default=None, ge=0)


class MoneynessBand(_Mult):
    max_steps: int | None = Field(default=None, ge=0)


def _open_ended_ascending(bands: tuple[DteBand, ...] | tuple[MoneynessBand, ...], attr: str) -> None:
    bounds = [getattr(b, attr) for b in bands]
    if not bounds or bounds[-1] is not None or any(b is None for b in bounds[:-1]):
        raise ValueError(f"{attr} bands: every band but the last needs {attr}; the last must be open-ended")
    fixed = [int(b) for b in bounds[:-1]]
    if fixed != sorted(set(fixed)):
        raise ValueError(f"{attr} bands must be strictly ascending")


class SyntheticSpreadParams(BaseModel):
    model_config = _FROZEN
    version: str = Field(min_length=1)
    status: Literal["ASSUMED"]  # no calibration path exists yet (D-10); anything else is refused
    basis: str = Field(min_length=1)
    tick_size: Decimal
    min_spread_ticks: int = Field(ge=1, le=100)
    premium_pct: Decimal
    max_spread_pct: Decimal
    time_of_day: tuple[TimeBand, ...]
    dte: tuple[DteBand, ...]
    moneyness: tuple[MoneynessBand, ...]

    @field_validator("tick_size", "premium_pct", "max_spread_pct", mode="before")
    @classmethod
    def _d(cls, v: Any) -> Decimal:
        return _dec(v)

    @model_validator(mode="after")
    def _check(self) -> SyntheticSpreadParams:
        if self.tick_size <= 0:
            raise ValueError("tick_size must be > 0")
        if not (Decimal(0) < self.premium_pct <= self.max_spread_pct <= Decimal(1)):
            raise ValueError("need 0 < premium_pct <= max_spread_pct <= 1")
        bands = self.time_of_day
        if not bands or bands[0].start != _SESSION_OPEN or bands[-1].end < _SESSION_CLOSE:
            raise ValueError("time_of_day bands must start at 09:15 and reach at least 15:30")
        for i, b in enumerate(bands):
            if not b.start < b.end:
                raise ValueError(f"empty time band {b.start}-{b.end}")
            if i and bands[i - 1].end != b.start:
                raise ValueError(f"time bands must be contiguous (gap/overlap at {b.start})")
        _open_ended_ascending(self.dte, "max_days")
        _open_ended_ascending(self.moneyness, "max_steps")
        return self


@dataclass(frozen=True, slots=True)
class SpreadEstimate:
    spread: Decimal  # full bid-ask spread, whole ticks
    half_spread: Decimal  # ceil(spread_ticks / 2) ticks
    base: Decimal
    m_time: Decimal
    m_dte: Decimal
    m_moneyness: Decimal
    capped: bool
    version: str
    status: str = SPREAD_STATUS_ASSUMED


class SyntheticSpreadModel:
    def __init__(self, params: SyntheticSpreadParams) -> None:
        self.params = params

    @property
    def version(self) -> str:
        return self.params.version

    @property
    def status(self) -> str:
        return self.params.status

    def _time_mult(self, at: datetime) -> Decimal:
        t = at.astimezone(IST).time()
        for b in self.params.time_of_day:
            if b.start <= t < b.end:
                return b.multiplier
        last = self.params.time_of_day[-1].end
        raise SpreadModelError(f"{at.isoformat()} is outside every time-of-day band (09:15-{last:%H:%M} IST)")

    def estimate(self, premium: Decimal, at: datetime, dte_days: int, steps_from_atm: int) -> SpreadEstimate:
        p = self.params
        if premium <= 0:
            raise SpreadModelError(f"premium must be > 0 (got {premium})")
        if dte_days < 0 or steps_from_atm < 0:
            raise SpreadModelError("dte_days and steps_from_atm must be >= 0")
        m_t = self._time_mult(at)
        m_d = next(b.multiplier for b in p.dte if b.max_days is None or dte_days <= b.max_days)
        m_m = next(b.multiplier for b in p.moneyness if b.max_steps is None or steps_from_atm <= b.max_steps)
        floor = p.min_spread_ticks * p.tick_size
        base = max(floor, p.premium_pct * premium)
        raw = base * m_t * m_d * m_m
        cap = max(floor, p.max_spread_pct * premium)
        capped = raw > cap
        ticks = int((min(raw, cap) / p.tick_size).to_integral_value(rounding=ROUND_CEILING))
        ticks = max(ticks, p.min_spread_ticks)
        half_ticks = -(-ticks // 2)
        return SpreadEstimate(
            ticks * p.tick_size, half_ticks * p.tick_size, base, m_t, m_d, m_m, capped, p.version, p.status
        )

    def describe(self) -> dict[str, object]:
        return {"spread_model": self.params.version, "spread_status": self.params.status}


SpotLookup = Callable[[str, datetime], Decimal | None]


class SyntheticSpreadProvider:
    """Adapts the model to the bar fill model: key + bar -> half-spread, using contract terms, the underlying spot
    at the bar and the trading calendar. The premium used is the bar HIGH (a higher premium means a wider
    percentage spread: the conservative choice). Missing context raises; nothing is defaulted."""

    def __init__(
        self,
        model: SyntheticSpreadModel,
        contracts: Mapping[str, OptionContract],
        spot: SpotLookup,
        calendar: TradingCalendar,
        strike_step: Decimal,
    ) -> None:
        if strike_step <= 0:
            raise SpreadModelError("strike_step must be > 0")
        self._model = model
        self._contracts = contracts
        self._spot = spot
        self._cal = calendar
        self._step = strike_step
        self._dte: dict[tuple[date, date], int] = {}
        self.estimates = 0
        self.capped = 0

    def dte(self, trade_date: date, expiry: date) -> int:
        k = (trade_date, expiry)
        if k not in self._dte:
            if trade_date > expiry:
                raise SpreadModelError(f"bar on {trade_date} is after the contract expiry {expiry}")
            self._dte[k] = len(self._cal.trading_days(trade_date, expiry)) - 1
        return self._dte[k]

    def estimate(self, instrument_key: str, bar: Bar) -> SpreadEstimate:
        c = self._contracts.get(instrument_key)
        if c is None:
            raise SpreadModelError(f"no contract terms for {instrument_key}")
        spot = self._spot(instrument_key, bar.start)
        if spot is None:
            raise SpreadModelError(f"no underlying spot for {instrument_key} at {bar.start.isoformat()}")
        steps = int((abs(c.strike - spot) / self._step).to_integral_value(rounding=ROUND_HALF_UP))
        est = self._model.estimate(bar.high, bar.start, self.dte(bar.start.astimezone(IST).date(), c.expiry), steps)
        self.estimates += 1
        self.capped += est.capped
        return est

    def half_spread(self, instrument_key: str, bar: Bar) -> Decimal:
        return self.estimate(instrument_key, bar).half_spread

    def describe(self) -> dict[str, object]:
        return self._model.describe()


def load_spread_params(path: Path) -> SyntheticSpreadParams:
    try:
        with path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    try:
        return SyntheticSpreadParams.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid spread model {path}: {e}") from e

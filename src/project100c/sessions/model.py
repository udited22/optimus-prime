"""Session model.

Two independent, versioned notions:

* **ExchangeSession**: what NSE runs (F&O normal market 09:15-15:40 from 3-Aug-2026, 15:30 before; cash 09:15-15:30).
* **TradingWindow**: what *we* allow (OD-002: order activity only 09:15-15:00, hard flat 15:00; OD-008: no new
  entries at/after 14:00, superseding OD-003's 14:45; OD-007: broker Exit-All if not flat at 15:00). The window
  must sit inside the F&O normal market on every date it is applied to; otherwise a SessionError is raised
  (never silently clipped).

Holidays and special sessions are not modelled yet (backlog D-03), so every time query requires the caller to
state that the date has been confirmed as a trading day by the calendar service.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime, time
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project100c.errors import ConfigError, SessionError

IST = ZoneInfo("Asia/Kolkata")
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ExchangeSession(BaseModel):
    model_config = _FROZEN

    version: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    normal_open: time
    normal_close: time
    trade_modification_end: time | None = None
    close_price_vwap_start: time | None = None
    close_price_vwap_end: time | None = None
    closing_auction_start: time | None = None
    closing_auction_end: time | None = None
    closing_auction_verified: bool | None = None
    verified: bool
    sources: tuple[str, ...] = ()
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> ExchangeSession:
        if not self.normal_open < self.normal_close:
            raise ValueError(f"{self.version}: normal_open must precede normal_close")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(f"{self.version}: effective_to before effective_from")
        if (self.closing_auction_start is None) != (self.closing_auction_end is None):
            raise ValueError(f"{self.version}: closing auction needs both start and end")
        return self

    def covers(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d <= self.effective_to)


def _validate_versions(items: tuple[ExchangeSession, ...], label: str) -> None:
    if not items:
        raise ValueError(f"no {label} sessions")
    for a, b in pairwise(items):
        if a.effective_to is None:
            raise ValueError(f"{label}: open-ended {a.version} is not last")
        if b.effective_from <= a.effective_to:
            raise ValueError(f"{label}: {a.version} and {b.version} overlap or are unsorted")


class ExchangeSessions(BaseModel):
    model_config = _FROZEN

    config_version: str
    fo: tuple[ExchangeSession, ...]
    cash: tuple[ExchangeSession, ...]

    @model_validator(mode="after")
    def _check(self) -> ExchangeSessions:
        _validate_versions(self.fo, "fo")
        _validate_versions(self.cash, "cash")
        return self

    @staticmethod
    def _pick(items: tuple[ExchangeSession, ...], d: date, label: str) -> ExchangeSession:
        for s in items:
            if s.covers(d):
                if not s.verified:
                    raise SessionError(f"{label} session {s.version} for {d} is UNVERIFIED")
                return s
        raise SessionError(f"no {label} session version covers {d}")

    def fo_for(self, d: date) -> ExchangeSession:
        return self._pick(self.fo, d, "fo")

    def cash_for(self, d: date) -> ExchangeSession:
        return self._pick(self.cash, d, "cash")


class ResidualPositionPolicy(StrEnum):
    """What happens if a position is still open at hard_flat (15:00)."""

    BROKER_EXIT_ALL = (
        "BROKER_EXIT_ALL"  # OD-007 default: trigger the broker's Exit-All + urgent alert; failure -> halt + recon kill
    )
    HALT_AND_ALERT = "HALT_AND_ALERT"  # fallback: stop ALL automated order activity, P1 alert, owner exits manually
    EXIT_ONLY_EXTENSION = "EXIT_ONLY_EXTENSION"  # automated exit-only orders until residual_extension_end (needs an OD)


class TradingWindow(BaseModel):
    model_config = _FROZEN

    version: str = Field(min_length=1)
    adopted_on: date
    order_activity_start: time
    order_activity_end: time
    entry_start: time
    entry_cutoff: time
    flatten_start: time
    hard_flat: time
    residual_position_policy: ResidualPositionPolicy = ResidualPositionPolicy.HALT_AND_ALERT
    residual_extension_end: time | None = None
    residual_policy_owner_decision: str | None = None
    owner_decisions: tuple[str, ...] = ()
    pending_signoff: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _ordering(self) -> TradingWindow:
        seq = [
            ("order_activity_start", self.order_activity_start),
            ("entry_start", self.entry_start),
            ("entry_cutoff", self.entry_cutoff),
            ("flatten_start", self.flatten_start),
            ("hard_flat", self.hard_flat),
        ]
        for (na, a), (nb, b) in pairwise(seq):
            if not a <= b:
                raise ValueError(f"{self.version}: {na} ({a}) must be <= {nb} ({b})")
        if self.entry_start >= self.entry_cutoff:
            raise ValueError(f"{self.version}: empty entry window")
        if self.hard_flat > self.order_activity_end:
            raise ValueError(f"{self.version}: hard_flat after order_activity_end leaves no time to exit")
        if self.flatten_start >= self.order_activity_end:
            raise ValueError(f"{self.version}: flatten_start must be before order_activity_end")
        if self.residual_position_policy is ResidualPositionPolicy.BROKER_EXIT_ALL:
            # Exit-All at hard_flat is order activity at/after 15:00: only valid with its owner decision (OD-007).
            if not self.residual_policy_owner_decision:
                raise ValueError(f"{self.version}: BROKER_EXIT_ALL requires residual_policy_owner_decision (OD-007)")
            if self.residual_extension_end is not None:
                raise ValueError(f"{self.version}: residual_extension_end is not used by BROKER_EXIT_ALL")
        elif self.residual_position_policy is ResidualPositionPolicy.EXIT_ONLY_EXTENSION:
            # OD-002 forbids order activity after 15:00; an extension is only valid with its own owner decision.
            if not self.residual_policy_owner_decision:
                raise ValueError(
                    f"{self.version}: EXIT_ONLY_EXTENSION requires residual_policy_owner_decision (OD-002)"
                )
            if self.residual_extension_end is None or self.residual_extension_end <= self.hard_flat:
                raise ValueError(f"{self.version}: EXIT_ONLY_EXTENSION needs residual_extension_end after hard_flat")
        elif self.residual_extension_end is not None:
            raise ValueError(
                f"{self.version}: residual_extension_end set but policy is {self.residual_position_policy}"
            )
        allowed = {
            "residual_position_policy",
            "entry_start",
            "entry_cutoff",
            "flatten_start",
            "hard_flat",
            "order_activity_start",
            "order_activity_end",
        }
        unknown = set(self.pending_signoff) - allowed
        if unknown:
            raise ValueError(f"{self.version}: pending_signoff has unknown fields {sorted(unknown)}")
        return self


class WindowPhase(StrEnum):
    """Phase of our trading window at an instant (assumes a confirmed trading day)."""

    BEFORE_WINDOW = "BEFORE_WINDOW"  # no order activity at all
    OPENING_NO_ENTRY = "OPENING_NO_ENTRY"  # activity allowed (exits/cancels), no new entries
    ENTRY_ALLOWED = "ENTRY_ALLOWED"
    EXIT_ONLY = "EXIT_ONLY"  # after entry cutoff, before forced flatten
    FLATTENING = "FLATTENING"  # forced flatten in progress
    CLOSED = "CLOSED"  # at/after order_activity_end: NO order activity, even if not flat


class SessionCalendar:
    def __init__(self, exchange: ExchangeSessions, window: TradingWindow) -> None:
        self._exchange = exchange
        self._window = window

    @property
    def window(self) -> TradingWindow:
        return self._window

    @property
    def exchange(self) -> ExchangeSessions:
        return self._exchange

    @staticmethod
    def _to_ist(ts: datetime) -> datetime:
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise SessionError("naive datetime: timestamps must be timezone-aware")
        return ts.astimezone(IST)

    def validate_for_date(self, d: date) -> None:
        """Raise SessionError unless our window sits inside the F&O normal market on ``d``."""
        fo = self._exchange.fo_for(d)
        w = self._window
        if w.order_activity_start < fo.normal_open:
            raise SessionError(f"{w.version}: order activity starts before F&O open on {d} ({fo.version})")
        if w.order_activity_end > fo.normal_close:
            raise SessionError(f"{w.version}: order activity ends after F&O close on {d} ({fo.version})")

    def phase(self, ts: datetime, *, trading_day_confirmed: bool) -> WindowPhase:
        if not trading_day_confirmed:
            raise SessionError("holiday calendar not consulted: caller must confirm the trading day (D-03)")
        local = self._to_ist(ts)
        self.validate_for_date(local.date())
        t = local.time()
        w = self._window
        if t < w.order_activity_start:
            return WindowPhase.BEFORE_WINDOW
        if t >= w.order_activity_end:
            return WindowPhase.CLOSED
        if t < w.entry_start:
            return WindowPhase.OPENING_NO_ENTRY
        if t < w.entry_cutoff:
            return WindowPhase.ENTRY_ALLOWED
        if t < w.flatten_start:
            return WindowPhase.EXIT_ONLY
        return WindowPhase.FLATTENING

    def order_activity_allowed(self, ts: datetime, *, trading_day_confirmed: bool) -> bool:
        p = self.phase(ts, trading_day_confirmed=trading_day_confirmed)
        return p not in (WindowPhase.BEFORE_WINDOW, WindowPhase.CLOSED)

    def entry_allowed(self, ts: datetime, *, trading_day_confirmed: bool) -> bool:
        return self.phase(ts, trading_day_confirmed=trading_day_confirmed) is WindowPhase.ENTRY_ALLOWED

    def must_be_flat(self, ts: datetime, *, trading_day_confirmed: bool) -> bool:
        if not trading_day_confirmed:
            raise SessionError("holiday calendar not consulted: caller must confirm the trading day (D-03)")
        return self._to_ist(ts).time() >= self._window.hard_flat

    def exchange_fo_close(self, d: date) -> time:
        return self._exchange.fo_for(d).normal_close

    def exchange_cash_close(self, d: date) -> time:
        return self._exchange.cash_for(d).normal_close


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e


def load_exchange_sessions(path: Path) -> ExchangeSessions:
    try:
        return ExchangeSessions.model_validate(_read_toml(path))
    except ValidationError as e:
        raise ConfigError(f"invalid exchange sessions {path}: {e}") from e


def load_trading_windows(path: Path, version: str | None = None) -> TradingWindow:
    """Load one window version (default: the file's ``current``). Unknown version -> ConfigError."""
    raw = _read_toml(path)
    windows = raw.get("window")
    if not isinstance(windows, list) or not windows:
        raise ConfigError(f"{path}: no [[window]] entries")
    wanted = version if version is not None else raw.get("current")
    if not isinstance(wanted, str):
        raise ConfigError(f"{path}: no version requested and 'current' not set")
    parsed: dict[str, TradingWindow] = {}
    for w in windows:
        try:
            tw = TradingWindow.model_validate(w)
        except ValidationError as e:
            raise ConfigError(f"invalid trading window in {path}: {e}") from e
        if tw.version in parsed:
            raise ConfigError(f"duplicate window version {tw.version}")
        parsed[tw.version] = tw
    if wanted not in parsed:
        raise ConfigError(f"trading window version {wanted!r} not found in {path}")
    return parsed[wanted]

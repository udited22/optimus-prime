"""NSE F&O trading calendar (holidays + special sessions) and NIFTY expiry rules (backlog D-03).

Principles:
- A date is only called a trading day if its year is *covered* by the holiday book. Outside coverage the calendar
  raises CalendarCoverageError; it never assumes "no holidays".
- Each holiday records its sources and whether it is verified (>= 2 independent public sources agree).
  Unverified entries are usable only if the caller opts in (allow_unverified=True).
- Special sessions (Muhurat, Saturday budget/DR sessions) are NOT regular trading days for this system.
- Expiry = the rule's nominal weekday; if that is not a trading day, the previous trading day (NSE practice).
  Rules are versioned by the nominal expiry date: Thursday up to 31-Aug-2025, Tuesday from 1-Sep-2025
  (NSE/FAOP/68747). The Monday change of NSE/FAOP/66938 was deferred (NSE/FAOP/67338) and never took effect.
- For years outside coverage, `provisional=True` may be requested: only fixed-date national holidays are applied
  and the result is flagged provisional (used for long-dated contracts; the instrument master is authoritative).
"""

from __future__ import annotations

import calendar as _cal
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project100c.errors import CalendarCoverageError, CalendarError, ConfigError

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_Date = date


class DayKind(StrEnum):
    TRADING_HOLIDAY = "TRADING_HOLIDAY"  # F&O closed
    SPECIAL_SESSION = "SPECIAL_SESSION"  # e.g. Muhurat / Saturday session: not a regular day for us
    SETTLEMENT_HOLIDAY = "SETTLEMENT_HOLIDAY"  # F&O trades normally; informational


class CalendarDay(BaseModel):
    model_config = _FROZEN
    date: date
    kind: DayKind
    description: str = Field(min_length=1)
    sources: tuple[str, ...] = Field(min_length=1)
    verified: bool
    note: str = ""

    @model_validator(mode="after")
    def _verified_needs_two_sources(self) -> CalendarDay:
        if self.verified and len(self.sources) < 2:
            raise ValueError(f"{self.date}: verified=true requires >= 2 independent sources")
        return self


class HolidayBook(BaseModel):
    model_config = _FROZEN
    version: str = Field(min_length=1)
    covered_years: tuple[int, ...] = Field(min_length=1)
    fixed_date_holidays: tuple[tuple[int, int], ...]  # (month, day), used only in provisional mode
    days: tuple[CalendarDay, ...]

    @model_validator(mode="after")
    def _consistency(self) -> HolidayBook:
        seen: set[date] = set()
        for d in self.days:
            if d.date in seen:
                raise ValueError(f"duplicate calendar entry {d.date}")
            seen.add(d.date)
            if d.date.year not in self.covered_years:
                raise ValueError(f"{d.date} is outside covered_years {self.covered_years}")
            if d.kind is DayKind.TRADING_HOLIDAY and d.date.weekday() >= 5:
                raise ValueError(f"{d.date}: weekend holidays need not be listed (they are non-trading anyway)")
        return self


class ExpiryRule(BaseModel):
    model_config = _FROZEN
    version: str
    effective_from: date  # applies to nominal expiry dates in [effective_from, effective_to]
    effective_to: date | None
    weekday: int = Field(ge=0, le=4)  # Mon=0 .. Fri=4
    source: str = Field(min_length=1)


class ExpiryRules(BaseModel):
    model_config = _FROZEN
    underlying: str
    rules: tuple[ExpiryRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _contiguous(self) -> ExpiryRules:
        rs = self.rules
        for a, b in pairwise(rs):
            if a.effective_to is None or b.effective_from != a.effective_to + timedelta(days=1):
                raise ValueError(f"expiry rules {a.version} -> {b.version} are not contiguous")
        return self

    def rule_for(self, nominal: date) -> ExpiryRule:
        for r in self.rules:
            if r.effective_from <= nominal and (r.effective_to is None or nominal <= r.effective_to):
                return r
        raise CalendarError(f"no {self.underlying} expiry rule covers {nominal}")


class ExpiryKind(StrEnum):
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"  # last expiry of the month (also carries futures, quarterly, half-yearly)


@dataclass(frozen=True, slots=True)
class Expiry:
    date: _Date
    nominal: _Date  # the rule weekday before any holiday shift
    kind: ExpiryKind
    rule_version: str
    shifted: bool
    provisional: bool


class TradingCalendar:
    def __init__(self, book: HolidayBook, *, allow_unverified: bool = False) -> None:
        self._book = book
        self._days = {d.date: d for d in book.days}
        self._fixed = frozenset(book.fixed_date_holidays)
        self._allow_unverified = allow_unverified

    @property
    def version(self) -> str:
        return self._book.version

    def covers(self, d: date) -> bool:
        return d.year in self._book.covered_years

    def entry(self, d: date) -> CalendarDay | None:
        return self._days.get(d)

    def is_trading_day(self, d: date, *, provisional: bool = False) -> bool:
        """Regular F&O trading day? Raises CalendarCoverageError outside coverage unless provisional=True."""
        if d.weekday() >= 5:
            return False
        if not self.covers(d):
            if not provisional:
                raise CalendarCoverageError(f"{d}: year {d.year} not covered by holiday book {self.version}")
            return (d.month, d.day) not in self._fixed
        e = self._days.get(d)
        if e is None or e.kind is DayKind.SETTLEMENT_HOLIDAY:
            return True
        if not e.verified and not self._allow_unverified:
            raise CalendarError(
                f"{d} ({e.description}) is UNVERIFIED in {self.version}; construct with allow_unverified=True to use"
            )
        return False

    def previous_trading_day(self, d: date, *, provisional: bool = False) -> date:
        x = d - timedelta(days=1)
        while not self.is_trading_day(x, provisional=provisional):
            x -= timedelta(days=1)
        return x

    def next_trading_day(self, d: date, *, provisional: bool = False) -> date:
        x = d + timedelta(days=1)
        while not self.is_trading_day(x, provisional=provisional):
            x += timedelta(days=1)
        return x

    def trading_days(self, start: date, end: date) -> list[date]:
        if end < start:
            raise CalendarError("end before start")
        out: list[date] = []
        x = start
        while x <= end:
            if self.is_trading_day(x):
                out.append(x)
            x += timedelta(days=1)
        return out


class ExpiryCalendar:
    def __init__(self, cal: TradingCalendar, rules: ExpiryRules) -> None:
        self._cal = cal
        self._rules = rules

    def _resolve(self, nominal: date, kind: ExpiryKind, rule: ExpiryRule, provisional: bool) -> Expiry:
        prov = provisional and not self._cal.covers(nominal)
        d = nominal
        if not self._cal.is_trading_day(d, provisional=provisional):
            d = self._cal.previous_trading_day(d, provisional=provisional)
        return Expiry(
            date=d, nominal=nominal, kind=kind, rule_version=rule.version, shifted=d != nominal, provisional=prov
        )

    def monthly(self, year: int, month: int, *, provisional: bool = False) -> Expiry:
        last = date(year, month, _cal.monthrange(year, month)[1])
        # the rule is chosen by the candidate nominal date; try the rule in force at month end
        rule = self._rules.rule_for(last)
        nominal = last - timedelta(days=(last.weekday() - rule.weekday) % 7)
        rule = self._rules.rule_for(nominal)
        nominal = last - timedelta(days=(last.weekday() - rule.weekday) % 7)
        return self._resolve(nominal, ExpiryKind.MONTHLY, rule, provisional)

    def weekly_for_week(self, any_day: date, *, provisional: bool = False) -> Expiry:
        """The expiry belonging to the Mon-Sun week that contains ``any_day``."""
        monday = any_day - timedelta(days=any_day.weekday())
        # rule by the nominal date; evaluate for each candidate weekday
        for r in self._rules.rules:
            nominal = monday + timedelta(days=r.weekday)
            if self._rules.rule_for(nominal) is r:
                kind = (
                    ExpiryKind.MONTHLY
                    if self.monthly(nominal.year, nominal.month, provisional=provisional).nominal == nominal
                    else ExpiryKind.WEEKLY
                )
                return self._resolve(nominal, kind, r, provisional)
        raise CalendarError(f"no expiry rule applies to the week of {monday}")

    def expiries_between(self, start: date, end: date, *, provisional: bool = False) -> list[Expiry]:
        """All weekly+monthly expiries whose (shifted) date lies in [start, end]."""
        if end < start:
            raise CalendarError("end before start")
        out: list[Expiry] = []
        seen: set[date] = set()
        week = start - timedelta(days=start.weekday() + 7)  # include a week before: shifts can move back
        while week <= end + timedelta(days=7):
            e = self.weekly_for_week(week, provisional=provisional)
            if start <= e.date <= end and e.date not in seen:
                out.append(e)
                seen.add(e.date)
            week += timedelta(days=7)
        return sorted(out, key=lambda e: e.date)

    def is_expiry_day(self, d: date) -> bool:
        return any(e.date == d for e in self.expiries_between(d, d))


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e


def load_holiday_book(path: Path) -> HolidayBook:
    raw = _read(path)
    try:
        raw["fixed_date_holidays"] = [tuple(x) for x in raw.get("fixed_date_holidays", [])]
        raw["days"] = raw.pop("day", [])
        return HolidayBook.model_validate(raw)
    except (ValidationError, TypeError) as e:
        raise ConfigError(f"invalid holiday book {path}: {e}") from e


def load_expiry_rules(path: Path) -> ExpiryRules:
    raw = _read(path)
    try:
        raw["rules"] = raw.pop("rule", [])
        return ExpiryRules.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid expiry rules {path}: {e}") from e

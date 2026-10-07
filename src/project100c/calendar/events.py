"""Versioned event calendar (``configs/calendar/events.yaml``; schema ``schemas/events.schema.json``).

Each entry has a date, type, status (HELD / SCHEDULED), title, the official source URL, a ``verified`` flag and an
``impact``.

* ``verified: true`` needs an official source domain: RBI, the Union Budget site, PIB, NSE, BSE, the Election
  Commission, the Federal Reserve or the US BLS.
* Only verified entries reach the strategies and the classifier (``EventBook.to_calendar``). An unverified entry
  is kept for the record and ignored.

``impact`` says which Indian session the event moves:

* ``same_session`` (default): the event's own date, e.g. an RBI MPC decision at 10:00 IST.
* ``next_session``: US events announced after the NSE close (15:30 IST).
  * An FOMC statement at 2:00 pm ET is 23:30 IST (EDT) or 00:30 IST (EST).
  * A US CPI release at 8:30 am ET is 18:00 IST (EDT) or 19:00 IST (EST).
  * The Indian market first trades on it the next session, so EVENT_REGIME applies to the first NSE trading day
    strictly after the US date.
  * ``to_calendar`` needs the trading calendar to find that day.
  * For a year the holiday book does not cover, the day is computed in provisional mode (fixed-date holidays
    only) and the event is marked ``provisional``. Re-check it when that year's holiday book lands.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import CalendarCoverageError, ConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only (regime imports calendar at runtime)
    from project100c.calendar.model import TradingCalendar
    from project100c.regime.config import EventCalendar

SCHEMA_VERSION = 1
OFFICIAL_DOMAINS = (
    "rbi.org.in",
    "indiabudget.gov.in",
    "pib.gov.in",
    "nseindia.com",
    "bseindia.com",
    "eci.gov.in",
    "results.eci.gov.in",
    "federalreserve.gov",
    "bls.gov",
)
# US releases land after the NSE close, so they must move the next Indian session (see the module docstring).
_AFTER_INDIAN_CLOSE = frozenset({"US_FOMC_DECISION", "US_CPI_RELEASE"})


class EventType(StrEnum):
    RBI_MPC_DECISION = "RBI_MPC_DECISION"
    UNION_BUDGET = "UNION_BUDGET"
    ELECTION_RESULT = "ELECTION_RESULT"
    SPECIAL_SESSION = "SPECIAL_SESSION"
    US_FOMC_DECISION = "US_FOMC_DECISION"  # FOMC statement, 2:00 pm ET on the meeting's last day
    US_CPI_RELEASE = "US_CPI_RELEASE"  # BLS Consumer Price Index release, 8:30 am ET
    OTHER = "OTHER"


class EventImpact(StrEnum):
    SAME_SESSION = "same_session"
    NEXT_SESSION = "next_session"


class EventStatus(StrEnum):
    HELD = "HELD"
    SCHEDULED = "SCHEDULED"


def _official(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


class EventEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    type: EventType
    status: EventStatus
    title: str = Field(min_length=3)
    source_url: str = Field(min_length=12)
    verified: bool
    impact: EventImpact = EventImpact.SAME_SESSION

    @field_validator("source_url")
    @classmethod
    def _https(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme != "https" or not p.hostname:
            raise ValueError(f"source_url must be an https URL, got {v!r}")
        return v

    @model_validator(mode="after")
    def _verified_needs_official(self) -> EventEntry:
        if self.verified and not _official(self.source_url):
            raise ValueError(f"{self.date} {self.type}: verified needs an official source, not {self.source_url}")
        if self.type.value in _AFTER_INDIAN_CLOSE and self.impact is not EventImpact.NEXT_SESSION:
            raise ValueError(f"{self.date} {self.type}: lands after the NSE close, so impact must be next_session")
        return self


class EventBook(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    version: str = Field(min_length=1)
    adopted_on: date
    checked_on: date
    events: tuple[EventEntry, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> EventBook:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version {self.schema_version} != {SCHEMA_VERSION}")
        keys = [(e.date, e.type) for e in self.events]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate (date, type) entries")
        if [e.date for e in self.events] != sorted(e.date for e in self.events):
            raise ValueError("events must be in date order")
        for e in self.events:
            if e.status is EventStatus.HELD and e.date > self.checked_on:
                raise ValueError(f"{e.date}: HELD but after checked_on {self.checked_on}")
        return self

    @property
    def verified(self) -> tuple[EventEntry, ...]:
        return tuple(e for e in self.events if e.verified)

    def on(self, d: date, *, verified_only: bool = True) -> tuple[EventEntry, ...]:
        return tuple(e for e in (self.verified if verified_only else self.events) if e.date == d)

    def session_for(self, e: EventEntry, cal: TradingCalendar | None) -> tuple[date, bool]:
        """The Indian session ``e`` moves and whether that date is provisional (outside the holiday book)."""
        if e.impact is EventImpact.SAME_SESSION:
            return e.date, False
        if cal is None:
            raise ConfigError(f"{e.date} {e.type}: a next_session event needs the trading calendar")
        try:
            return cal.next_trading_day(e.date), False
        except CalendarCoverageError:  # beyond the holiday book: fixed-date holidays only, flagged provisional
            return cal.next_trading_day(e.date, provisional=True), True

    def to_calendar(self, trading_calendar: TradingCalendar | None = None) -> EventCalendar:
        """The verified entries as the regime classifier's ``EventCalendar`` (unverified ones are dropped).

        Each event is placed on the Indian session it moves (``impact``). A ``next_session`` event needs
        ``trading_calendar``.
        """
        from project100c.regime.config import EventCalendar, ScheduledEvent

        out = []
        for e in self.verified:
            d, prov = self.session_for(e, trading_calendar)
            out.append(ScheduledEvent(date=d, kind=e.type.value, source=e.source_url,
                                      source_date=e.date if d != e.date else None,
                                      provisional=prov))  # fmt: skip
        return EventCalendar(version=self.version, status="VERIFIED", events=tuple(sorted(out, key=lambda x: x.date)))


def load_event_book(path: Path) -> EventBook:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: not a mapping")
    try:
        return EventBook.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e


def event_book_schema() -> dict[str, Any]:
    return EventBook.model_json_schema()

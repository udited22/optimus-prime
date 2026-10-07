"""The versioned event calendar (configs/calendar/events.yaml): schema, provenance, verified-only consumption."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from project100c.calendar import TradingCalendar, load_holiday_book
from project100c.calendar.events import (
    OFFICIAL_DOMAINS,
    EventBook,
    EventImpact,
    EventStatus,
    EventType,
    event_book_schema,
    load_event_book,
)
from project100c.errors import ConfigError
from project100c.regime import RegimeClassifier, classify_session, load_regime_config
from project100c.spec.models import Regime
from project100c.synthetic import DayPlan, Segment, generate_day
from tests.data.dhan_fakes import CONFIGS, REPO

BOOK = CONFIGS / "calendar" / "events.yaml"
CAL = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
FED = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS = "https://www.bls.gov/schedule/news_release/cpi.htm"


def raw() -> dict[str, Any]:
    d = yaml.safe_load(BOOK.read_text(encoding="utf-8"))
    assert isinstance(d, dict)
    return d


def write(tmp_path: Path, d: dict[str, Any]) -> Path:
    p = tmp_path / "events.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    return p


def test_the_shipped_book_is_versioned_and_every_entry_has_an_official_source() -> None:
    b = load_event_book(BOOK)
    assert b.version.startswith("EV-") and b.schema_version == 1
    assert b.events
    for e in b.events:
        assert e.source_url.startswith("https://")
        if e.verified:
            assert any(d in e.source_url for d in OFFICIAL_DOMAINS)
    types = {e.type for e in b.events}
    assert {EventType.RBI_MPC_DECISION, EventType.UNION_BUDGET} <= types


def test_the_checked_in_schema_matches_the_model() -> None:
    on_disk = json.loads((REPO / "schemas" / "events.schema.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(event_book_schema(), sort_keys=True))


def test_known_official_dates() -> None:
    b = load_event_book(BOOK)
    assert [e.type for e in b.on(date(2026, 10, 7))] == [EventType.RBI_MPC_DECISION]  # RBI PR 2025-2026/2306
    assert b.on(date(2026, 10, 7))[0].status is EventStatus.SCHEDULED
    assert [e.type for e in b.on(date(2026, 2, 1))] == [EventType.UNION_BUDGET]
    assert b.on(date(2025, 8, 6)) and not b.on(date(2025, 8, 7))  # rescheduled (RBI PR 2025-2026/497)


def test_unverified_entries_are_kept_but_never_reach_the_strategies(tmp_path: Path) -> None:
    d = raw()
    d["events"].append({"date": date(2028, 3, 1), "type": "OTHER", "status": "SCHEDULED", "title": "rumoured event",
                        "source_url": "https://example.com/rumour", "verified": False})  # fmt: skip
    b = load_event_book(write(tmp_path, d))
    assert b.on(date(2028, 3, 1)) == () and len(b.on(date(2028, 3, 1), verified_only=False)) == 1
    assert date(2028, 3, 1) not in {e.date for e in b.to_calendar(CAL).events}


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"source_url": "https://example.com/x", "verified": True}, "official source"),
        ({"source_url": "http://www.rbi.org.in/x"}, "https"),
        ({"type": "WHATEVER"}, "type"),
        ({"date": date(2030, 1, 1), "status": "HELD"}, "HELD but after"),
    ],
)
def test_bad_entries_are_rejected(tmp_path: Path, patch: dict[str, Any], match: str) -> None:
    d = raw()
    d["events"][-1] = d["events"][-1] | patch
    with pytest.raises(ConfigError, match=match):
        load_event_book(write(tmp_path, d))


def test_duplicates_order_and_schema_version_are_enforced(tmp_path: Path) -> None:
    d = raw()
    d["events"].append(dict(d["events"][-1]))
    with pytest.raises(ConfigError, match="duplicate"):
        load_event_book(write(tmp_path, d))
    d = raw()
    d["events"] = list(reversed(d["events"]))
    with pytest.raises(ConfigError, match="date order"):
        load_event_book(write(tmp_path, d))
    with pytest.raises(ConfigError, match="schema_version"):
        load_event_book(write(tmp_path, raw() | {"schema_version": 2}))
    p = tmp_path / "bad.yaml"
    p.write_text("- just a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_event_book(p)
    with pytest.raises(ValueError):
        EventBook.model_validate(raw() | {"unknown": 1})


def test_a_verified_event_day_sets_the_event_regime() -> None:
    cal = load_event_book(BOOK).to_calendar(CAL)
    cfg = load_regime_config(CONFIGS / "regime" / "classifier.toml")
    for day, want in ((date(2026, 10, 7), True), (date(2026, 10, 8), False)):
        d = generate_day(DayPlan(day, 3, segments=(Segment(60, 0, 10),)))
        labs = classify_session(RegimeClassifier(cfg, events=cal), day, list(d.index), prev_close=None)
        assert (Regime.EVENT_REGIME in labs[-1].tags()) is want


# ---------------------------------------------------------------------------------------- US events (next_session)
US_DATES = {
    # (US date, type): the Indian session it moves; 2026 checked against the holiday book, 2027 provisional
    (date(2026, 10, 14), EventType.US_CPI_RELEASE): date(2026, 10, 15),
    (date(2026, 10, 28), EventType.US_FOMC_DECISION): date(2026, 10, 29),
    (date(2026, 11, 10), EventType.US_CPI_RELEASE): date(2026, 11, 11),
    (date(2026, 12, 9), EventType.US_FOMC_DECISION): date(2026, 12, 10),
    (date(2026, 12, 10), EventType.US_CPI_RELEASE): date(2026, 12, 11),
    (date(2027, 1, 27), EventType.US_FOMC_DECISION): date(2027, 1, 28),
    (date(2027, 3, 17), EventType.US_FOMC_DECISION): date(2027, 3, 18),
    (date(2027, 4, 28), EventType.US_FOMC_DECISION): date(2027, 4, 29),
    (date(2027, 6, 9), EventType.US_FOMC_DECISION): date(2027, 6, 10),
    (date(2027, 7, 28), EventType.US_FOMC_DECISION): date(2027, 7, 29),
    (date(2027, 9, 15), EventType.US_FOMC_DECISION): date(2027, 9, 16),
    (date(2027, 10, 27), EventType.US_FOMC_DECISION): date(2027, 10, 28),
    (date(2027, 12, 8), EventType.US_FOMC_DECISION): date(2027, 12, 9),
}


def test_us_fomc_and_cpi_dates_are_on_file_from_the_official_pages() -> None:
    b = load_event_book(BOOK)
    us = {(e.date, e.type): e for e in b.events if e.type in (EventType.US_FOMC_DECISION, EventType.US_CPI_RELEASE)}
    assert set(us) == set(US_DATES)
    for (_, t), e in us.items():
        assert e.verified and e.impact is EventImpact.NEXT_SESSION and e.status is EventStatus.SCHEDULED
        assert e.source_url == (FED if t is EventType.US_FOMC_DECISION else BLS)
    assert not any(t is EventType.US_CPI_RELEASE and d.year == 2027 for d, t in us)  # BLS 2027 not yet published


def test_us_events_set_the_event_regime_on_the_next_indian_session() -> None:
    ev = load_event_book(BOOK).to_calendar(CAL)
    got: dict[tuple[date | None, str], tuple[date, bool]] = {
        (e.source_date, e.kind): (e.date, e.provisional) for e in ev.events if e.kind.startswith("US_")
    }
    for (d, t), session in US_DATES.items():
        assert got[(d, t.value)] == (session, d.year == 2027), (d, t)
        if d.year == 2026:
            assert CAL.is_trading_day(session) and session > d
    assert not ev.on(date(2026, 10, 14)) and ev.on(date(2026, 10, 15))  # the US date itself is not an event day
    rbi = [e for e in ev.events if e.kind == "RBI_MPC_DECISION"]
    assert all(e.source_date is None and not e.provisional for e in rbi)  # same_session: unchanged
    assert [e.date for e in ev.events] == sorted(e.date for e in ev.events)


def test_next_session_events_need_the_trading_calendar_and_skip_holidays(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs the trading calendar"):
        load_event_book(BOOK).to_calendar()
    d = raw()
    d["events"] = [{"date": date(2026, 10, 1), "type": "US_CPI_RELEASE", "status": "SCHEDULED",
                    "title": "test: Thursday before Gandhi Jayanti", "source_url": BLS, "verified": True,
                    "impact": "next_session"}]  # fmt: skip
    ev = load_event_book(write(tmp_path, d)).to_calendar(CAL)
    assert [e.date for e in ev.events] == [date(2026, 10, 5)]  # Fri 2-Oct is a holiday, then the weekend


def test_us_releases_must_be_next_session(tmp_path: Path) -> None:
    d = raw()
    us = next(i for i, e in enumerate(d["events"]) if e["type"] == "US_FOMC_DECISION")
    d["events"][us] = d["events"][us] | {"impact": "same_session"}
    with pytest.raises(ConfigError, match="impact must be next_session"):
        load_event_book(write(tmp_path, d))

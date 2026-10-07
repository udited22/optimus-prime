"""Deterministic, versioned regime classifier (backlog K-11). UNVALIDATED: see docs/architecture/market-data.md §5.x and
docs/research/validation.md §13.5."""

from project100c.regime.classifier import (
    GapType,
    OpenCharacter,
    RegimeClassifier,
    RegimeLabel,
    SessionRegime,
    Trend,
    VolState,
    classify_session,
)
from project100c.regime.config import (
    EventCalendar,
    RegimeConfig,
    ScheduledEvent,
    load_event_calendar,
    load_regime_config,
)

__all__ = [
    "EventCalendar",
    "GapType",
    "OpenCharacter",
    "RegimeClassifier",
    "RegimeConfig",
    "RegimeLabel",
    "ScheduledEvent",
    "SessionRegime",
    "Trend",
    "VolState",
    "classify_session",
    "load_event_calendar",
    "load_regime_config",
]

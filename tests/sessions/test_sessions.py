"""Exchange session vs our trading window (OD-002 / OD-003) as separate versioned config."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from project100c.errors import ConfigError, SessionError
from project100c.sessions import (
    IST,
    ResidualPositionPolicy,
    SessionCalendar,
    WindowPhase,
    load_exchange_sessions,
    load_trading_windows,
)


@pytest.fixture(scope="module")
def cal(configs_dir: Path) -> SessionCalendar:
    ex = load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml")
    tw = load_trading_windows(configs_dir / "sessions" / "trading_window.toml")
    return SessionCalendar(ex, tw)


def ist(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


def test_exchange_fo_close_changes_on_3_aug_2026(cal: SessionCalendar) -> None:
    assert cal.exchange_fo_close(date(2026, 7, 31)) == time(15, 30)
    assert cal.exchange_fo_close(date(2026, 8, 3)) == time(15, 40)
    assert cal.exchange_fo_close(date(2026, 9, 30)) == time(15, 40)


def test_exchange_cash_close_is_1530(cal: SessionCalendar) -> None:
    assert cal.exchange_cash_close(date(2026, 7, 31)) == time(15, 30)
    assert cal.exchange_cash_close(date(2026, 9, 30)) == time(15, 30)


def test_cash_cas_window_flagged_unverified(configs_dir: Path) -> None:
    ex = load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml")
    cm = ex.cash_for(date(2026, 9, 30))
    assert cm.closing_auction_start == time(15, 15) and cm.closing_auction_end == time(15, 35)
    assert cm.closing_auction_verified is False


def test_trading_window_values_match_owner_decisions(cal: SessionCalendar) -> None:
    w = cal.window
    assert (w.order_activity_start, w.order_activity_end) == (time(9, 15), time(15, 0))  # OD-002
    assert w.hard_flat == time(15, 0)  # OD-002
    assert w.version == "TW-2026-10-01.2"
    assert w.entry_start == time(9, 20)  # OD-009 (confirmed)
    assert w.entry_cutoff == time(14, 0)  # OD-008 (supersedes OD-003's 14:45)
    assert w.flatten_start == time(14, 50)  # OD-008 (owner-confirmed)
    assert set(w.owner_decisions) == {"OD-002", "OD-007", "OD-008", "OD-009"}
    assert "entry_cutoff" not in w.pending_signoff and "flatten_start" not in w.pending_signoff
    assert w.pending_signoff == ()  # nothing in the trading window awaits sign-off any more
    assert w.residual_position_policy is ResidualPositionPolicy.BROKER_EXIT_ALL  # OD-007
    assert w.residual_policy_owner_decision == "OD-007" and w.residual_extension_end is None


def test_window_is_independent_of_exchange_close(cal: SessionCalendar) -> None:
    # Before and after the 15:30 -> 15:40 change our window still ends at 15:00.
    for d in (date(2026, 7, 31), date(2026, 9, 29)):
        assert cal.order_activity_allowed(ist(d.year, d.month, d.day, 14, 59, 59), trading_day_confirmed=True)
        assert not cal.order_activity_allowed(ist(d.year, d.month, d.day, 15, 0, 0), trading_day_confirmed=True)
        # Exchange is still open at 15:20 but we are CLOSED.
        assert cal.phase(ist(d.year, d.month, d.day, 15, 20), trading_day_confirmed=True) is WindowPhase.CLOSED


@pytest.mark.parametrize(
    ("hh", "mm", "ss", "phase"),
    [
        (9, 14, 59, WindowPhase.BEFORE_WINDOW),
        (9, 15, 0, WindowPhase.OPENING_NO_ENTRY),
        (9, 19, 59, WindowPhase.OPENING_NO_ENTRY),
        (9, 20, 0, WindowPhase.ENTRY_ALLOWED),
        (13, 59, 59, WindowPhase.ENTRY_ALLOWED),
        (14, 0, 0, WindowPhase.EXIT_ONLY),
        (14, 45, 0, WindowPhase.EXIT_ONLY),
        (14, 49, 59, WindowPhase.EXIT_ONLY),
        (14, 50, 0, WindowPhase.FLATTENING),
        (14, 59, 59, WindowPhase.FLATTENING),
        (15, 0, 0, WindowPhase.CLOSED),
        (15, 39, 59, WindowPhase.CLOSED),
        (16, 0, 0, WindowPhase.CLOSED),
    ],
)
def test_phase_boundaries(cal: SessionCalendar, hh: int, mm: int, ss: int, phase: WindowPhase) -> None:
    assert cal.phase(ist(2026, 9, 29, hh, mm, ss), trading_day_confirmed=True) is phase


def test_entry_cutoff_1400(cal: SessionCalendar) -> None:
    assert cal.entry_allowed(ist(2026, 9, 29, 13, 59, 59), trading_day_confirmed=True)
    assert not cal.entry_allowed(ist(2026, 9, 29, 14, 0), trading_day_confirmed=True)
    assert not cal.entry_allowed(ist(2026, 9, 29, 14, 30), trading_day_confirmed=True)


def test_superseded_window_version_still_loadable(configs_dir: Path) -> None:
    old = load_trading_windows(configs_dir / "sessions" / "trading_window.toml", version="TW-2026-09-30")
    assert old.entry_cutoff == time(14, 45)
    assert old.residual_position_policy is ResidualPositionPolicy.HALT_AND_ALERT


def test_must_be_flat_at_1500(cal: SessionCalendar) -> None:
    assert not cal.must_be_flat(ist(2026, 9, 29, 14, 59, 59), trading_day_confirmed=True)
    assert cal.must_be_flat(ist(2026, 9, 29, 15, 0), trading_day_confirmed=True)


def test_utc_timestamps_are_converted(cal: SessionCalendar) -> None:
    # 09:30 UTC = 15:00 IST -> CLOSED; 09:29:59 UTC = 14:59:59 IST -> FLATTENING
    utc = UTC
    assert cal.phase(datetime(2026, 9, 29, 9, 30, tzinfo=utc), trading_day_confirmed=True) is WindowPhase.CLOSED
    assert cal.phase(datetime(2026, 9, 29, 9, 29, 59, tzinfo=utc), trading_day_confirmed=True) is WindowPhase.FLATTENING


def test_naive_datetime_rejected(cal: SessionCalendar) -> None:
    with pytest.raises(SessionError):
        cal.phase(datetime(2026, 9, 29, 10, 0), trading_day_confirmed=True)


def test_unconfirmed_trading_day_rejected(cal: SessionCalendar) -> None:
    with pytest.raises(SessionError):
        cal.phase(ist(2026, 9, 29, 10, 0), trading_day_confirmed=False)
    with pytest.raises(SessionError):
        cal.must_be_flat(ist(2026, 9, 29, 10, 0), trading_day_confirmed=False)


def test_date_before_coverage_rejected(cal: SessionCalendar) -> None:
    with pytest.raises(SessionError):
        cal.phase(ist(2020, 12, 31, 10, 0), trading_day_confirmed=True)


# ---- config validation ----
WINDOW_TMPL = """
current = "T"
[[window]]
version = "T"
adopted_on = 2026-09-30
order_activity_start = "{oas}"
order_activity_end = "{oae}"
entry_start = "09:20:00"
entry_cutoff = "{cut}"
flatten_start = "{fl}"
hard_flat = "{hf}"
"""


def _w(tmp_path: Path, **kw: str) -> Path:
    vals = dict(oas="09:15:00", oae="15:00:00", cut="14:45:00", fl="14:50:00", hf="15:00:00") | kw
    p = tmp_path / "w.toml"
    p.write_text(WINDOW_TMPL.format(**vals))
    return p


@pytest.mark.parametrize(
    "kw",
    [
        dict(cut="14:55:00"),  # entry cutoff after flatten start
        dict(hf="15:05:00"),  # hard flat after order activity end
        dict(fl="15:00:00"),  # flatten starts when activity already ended
        dict(cut="09:10:00"),  # empty entry window
    ],
)
def test_inconsistent_window_rejected(tmp_path: Path, kw: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_trading_windows(_w(tmp_path, **kw))


def test_window_outside_exchange_session_raises(tmp_path: Path, configs_dir: Path) -> None:
    ex = load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml")
    late = load_trading_windows(_w(tmp_path, oae="15:35:00", hf="15:35:00", fl="15:20:00"))
    cal = SessionCalendar(ex, late)
    # Fine after 3-Aug-2026 (close 15:40) ...
    cal.validate_for_date(date(2026, 9, 29))
    # ... but not before, when F&O closed at 15:30: never silently clipped.
    with pytest.raises(SessionError):
        cal.phase(ist(2026, 7, 31, 10, 0), trading_day_confirmed=True)


def test_unknown_window_version(configs_dir: Path) -> None:
    with pytest.raises(ConfigError):
        load_trading_windows(configs_dir / "sessions" / "trading_window.toml", version="TW-nope")


def test_overlapping_exchange_versions_rejected(tmp_path: Path) -> None:
    s = """config_version="t"
[[fo]]
version="A"
effective_from=2026-01-01
effective_to=2026-09-01
normal_open="09:15:00"
normal_close="15:30:00"
verified=true
[[fo]]
version="B"
effective_from=2026-08-01
normal_open="09:15:00"
normal_close="15:40:00"
verified=true
[[cash]]
version="C"
effective_from=2026-01-01
normal_open="09:15:00"
normal_close="15:30:00"
verified=true
"""
    p = tmp_path / "e.toml"
    p.write_text(s)
    with pytest.raises(ConfigError):
        load_exchange_sessions(p)


@given(st.integers(min_value=0, max_value=24 * 3600 - 1))
def test_property_no_activity_outside_0915_1500(cal: SessionCalendar, secs: int) -> None:
    ts = datetime(2026, 9, 29, tzinfo=IST) + timedelta(seconds=secs)
    allowed = cal.order_activity_allowed(ts, trading_day_confirmed=True)
    assert allowed == (time(9, 15) <= ts.time() < time(15, 0))
    if cal.entry_allowed(ts, trading_day_confirmed=True):
        assert time(9, 20) <= ts.time() < time(14, 0)


def _window_dict(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": "TW-X",
        "adopted_on": "2026-09-30",
        "order_activity_start": "09:15:00",
        "order_activity_end": "15:00:00",
        "entry_start": "09:20:00",
        "entry_cutoff": "14:45:00",
        "flatten_start": "14:50:00",
        "hard_flat": "15:00:00",
    }
    base.update(over)
    return base


def test_residual_extension_requires_owner_decision() -> None:
    from pydantic import ValidationError

    from project100c.sessions import TradingWindow

    with pytest.raises(ValidationError, match="requires residual_policy_owner_decision"):
        TradingWindow.model_validate(
            _window_dict(residual_position_policy="EXIT_ONLY_EXTENSION", residual_extension_end="15:10:00")
        )
    with pytest.raises(ValidationError, match="after hard_flat"):
        TradingWindow.model_validate(
            _window_dict(residual_position_policy="EXIT_ONLY_EXTENSION", residual_policy_owner_decision="OD-999")
        )
    with pytest.raises(ValidationError, match="residual_extension_end set"):
        TradingWindow.model_validate(_window_dict(residual_extension_end="15:10:00"))
    ok = TradingWindow.model_validate(
        _window_dict(
            residual_position_policy="EXIT_ONLY_EXTENSION",
            residual_extension_end="15:10:00",
            residual_policy_owner_decision="OD-999",
        )
    )
    assert ok.residual_position_policy is ResidualPositionPolicy.EXIT_ONLY_EXTENSION


def test_broker_exit_all_requires_od_and_no_extension() -> None:
    from pydantic import ValidationError

    from project100c.sessions import TradingWindow

    with pytest.raises(ValidationError, match="OD-007"):
        TradingWindow.model_validate(_window_dict(residual_position_policy="BROKER_EXIT_ALL"))
    with pytest.raises(ValidationError, match="not used by BROKER_EXIT_ALL"):
        TradingWindow.model_validate(
            _window_dict(
                residual_position_policy="BROKER_EXIT_ALL",
                residual_policy_owner_decision="OD-007",
                residual_extension_end="15:10:00",
            )
        )
    ok = TradingWindow.model_validate(
        _window_dict(residual_position_policy="BROKER_EXIT_ALL", residual_policy_owner_decision="OD-007")
    )
    assert ok.residual_position_policy is ResidualPositionPolicy.BROKER_EXIT_ALL
    # default when unspecified stays the conservative fallback
    assert (
        TradingWindow.model_validate(_window_dict()).residual_position_policy is ResidualPositionPolicy.HALT_AND_ALERT
    )

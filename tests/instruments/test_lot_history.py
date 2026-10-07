"""NIFTY lot-size history (2021 onward) vs the exchange circulars and the 30-Sep-2026 vendor instrument files."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.calendar.model import ExpiryKind
from project100c.errors import ConfigError, LotSizeHistoryError
from project100c.instruments import parse_kite_csv, parse_upstox_json
from project100c.instruments.lot_history import LotSizeHistory, load_lot_history
from project100c.sessions import IST

W, M = ExpiryKind.WEEKLY, ExpiryKind.MONTHLY


@pytest.fixture(scope="module")
def hist(configs_dir: Path) -> LotSizeHistory:
    return load_lot_history(configs_dir / "instruments" / "nifty_lot_sizes.toml")


@pytest.fixture(scope="module")
def exp(configs_dir: Path) -> ExpiryCalendar:
    cal = TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml"))
    return ExpiryCalendar(cal, load_expiry_rules(configs_dir / "calendar" / "nifty_expiry_rules.toml"))


# (expiry, cycle, trade date, expected lot, why) - each row restates a sentence of the cited circular
CIRCULAR_CASES = [
    (date(2021, 1, 28), M, date(2021, 1, 4), 75, "base lot 75 (FAOP47854 'present market lot')"),
    (date(2021, 6, 24), M, date(2021, 5, 3), 75, "FAOP47854: June 2021 keeps existing lot"),
    (date(2021, 7, 29), M, date(2021, 4, 30), 50, "FAOP47854: July 2021 monthly revised"),
    (date(2021, 7, 22), W, date(2021, 7, 1), 75, "FAOP47854: weeklies before August 2021 keep 75"),
    (date(2021, 8, 5), W, date(2021, 7, 29), 50, "FAOP47854: August 2021 weekly & beyond revised"),
    (date(2021, 9, 30), M, date(2021, 6, 24), 75, "FAOP47854: long-term options revised only after June expiry"),
    (date(2021, 9, 30), M, date(2021, 6, 25), 50, "... from 25-Jun-2021"),
    (date(2024, 4, 25), M, date(2024, 4, 25), 50, "2024: no revision for April 2024 monthly"),
    (date(2024, 5, 2), W, date(2024, 4, 25), 50, "2024: contract existed with lot 50 before the effective date"),
    (date(2024, 5, 2), W, date(2024, 4, 26), 25, "2024: all contracts traded from 26-Apr-2024 revised"),
    (date(2024, 5, 30), M, date(2024, 4, 26), 25, "2024: first monthly with revised lot"),
    (date(2024, 12, 19), W, date(2024, 12, 2), 25, "FAOP64625: last weekly with existing lot"),
    (date(2024, 12, 26), M, date(2024, 12, 26), 25, "FAOP64625: existing monthly keeps lot"),
    (date(2025, 1, 2), W, date(2024, 11, 21), 75, "FAOP64625: first weekly with revised lot (listed from 21-Nov)"),
    (date(2025, 1, 30), M, date(2025, 1, 20), 25, "FAOP64625: last monthly with existing lot"),
    (date(2025, 2, 27), M, date(2024, 12, 27), 75, "FAOP64625: first monthly with revised lot"),
    (date(2025, 3, 27), M, date(2024, 12, 26), 25, "FAOP64625: quarterly revised at 26-Dec-2024 EOD"),
    (date(2025, 3, 27), M, date(2024, 12, 27), 75, "... from the next trade date"),
    (date(2025, 12, 23), W, date(2025, 12, 22), 75, "FAOP70616: last weekly with existing lot"),
    (date(2025, 12, 30), M, date(2025, 12, 30), 75, "FAOP70616: last monthly with existing lot"),
    (date(2026, 1, 6), W, date(2025, 12, 31), 65, "FAOP70616: first weekly with revised lot"),
    (date(2026, 1, 27), M, date(2025, 10, 29), 65, "FAOP70616: first monthly with revised lot"),
    (date(2026, 3, 30), M, date(2025, 12, 30), 75, "FAOP70616: quarterly revised at 30-Dec-2025 EOD"),
    (date(2026, 3, 30), M, date(2025, 12, 31), 65, "... from the next trade date"),
]


@pytest.mark.parametrize(("expiry", "kind", "trade", "lot", "why"), CIRCULAR_CASES)
def test_circular_statements(
    hist: LotSizeHistory, expiry: date, kind: ExpiryKind, trade: date, lot: int, why: str
) -> None:
    assert hist.lot_size(expiry, kind, trade) == lot, why


def test_last_old_and_first_new_are_consecutive_expiries(hist: LotSizeHistory, exp: ExpiryCalendar) -> None:
    """No contract of a cycle may fall strictly between a revision's last-old and first-new expiry."""
    for r in hist.revisions:
        for kind in (W, M):
            lo, hi = r.last_old(kind), r.first_new(kind)
            between = [e for e in exp.expiries_between(lo, hi) if e.date not in (lo, hi) and (e.kind is kind)]
            assert between == [], (r.id, kind, between)
            for d in (lo, hi):
                assert d in {e.date for e in exp.expiries_between(d, d) if e.kind is kind}, (r.id, kind, d)


def test_vendor_files_30sep2026_agree(hist: LotSizeHistory, exp: ExpiryCalendar, fixtures_dir: Path) -> None:
    as_of = datetime(2026, 9, 30, 23, 27, tzinfo=IST)
    up, _ = parse_upstox_json(
        fixtures_dir / "instruments" / "upstox_NSE_subset_20260930.json", as_of=as_of, underlyings=frozenset({"NIFTY"})
    )
    kite, _ = parse_kite_csv(
        fixtures_dir / "instruments" / "kite_NFO_subset_20260930.csv", as_of=as_of, underlyings=frozenset({"NIFTY"})
    )
    n = 0
    for master in (up, kite):
        for c in master.contracts:
            assert hist.lot_size_for(exp, c.expiry, date(2026, 9, 30)) == c.lot_size, c
            n += 1
    assert n > 50
    # NSE fo_mktlots.csv of 2-Oct-2026: 65 for every listed month, so the history is known through 2-Oct
    assert hist.known_through_trade_date == date(2026, 10, 2)
    assert hist.lot_size(date(2026, 10, 27), M, date(2026, 10, 1)) == 65
    assert hist.lot_size(date(2026, 10, 6), W, date(2026, 10, 2)) == 65


def test_refuses_outside_coverage(hist: LotSizeHistory) -> None:
    with pytest.raises(LotSizeHistoryError, match="before lot history coverage"):
        hist.lot_size(date(2020, 12, 31), M, date(2020, 12, 1))
    with pytest.raises(LotSizeHistoryError, match="later revision may exist"):
        hist.lot_size(date(2026, 10, 27), M, date(2026, 10, 5))
    with pytest.raises(LotSizeHistoryError, match="after expiry"):
        hist.lot_size(date(2025, 1, 2), W, date(2025, 1, 3))
    with pytest.raises(LotSizeHistoryError, match="not listed before"):
        hist.lot_size(date(2025, 1, 9), W, date(2024, 11, 15))  # weekly listed only under the new regime
    with pytest.raises(LotSizeHistoryError, match="between last-old and first-new"):
        hist.lot_size(date(2024, 12, 24), W, date(2024, 12, 2))  # no such weekly (a Tuesday in the gap)


def test_chain_and_verification_rules(configs_dir: Path, tmp_path: Path) -> None:
    text = (configs_dir / "instruments" / "nifty_lot_sizes.toml").read_text()
    bad = tmp_path / "b.toml"
    bad.write_text(text.replace("base_lot = 75", "base_lot = 70"))
    with pytest.raises(ConfigError, match="does not continue the chain"):
        load_lot_history(bad)
    h = load_lot_history(configs_dir / "instruments" / "nifty_lot_sizes.toml")
    last = h.revisions[-1].model_copy(update={"verified": False})
    h2 = h.model_copy(update={"revisions": (*h.revisions[:-1], last)})
    with pytest.raises(LotSizeHistoryError, match="UNVERIFIED"):
        h2.lot_size(date(2026, 1, 6), W, date(2025, 12, 31))
    assert h2.lot_size(date(2026, 1, 6), W, date(2025, 12, 31), allow_unverified=True) == 65
    assert h.current_lot == 65

"""The holdout partition (docs/research/validation.md §13.1): the most recent 6 months plus a fixed 20% of earlier ISO
weeks."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from project100c.errors import ConfigError
from project100c.validation.holdout import Holdout, load_holdout
from tests.data.dhan_fakes import CONFIGS


@pytest.fixture(scope="module")
def h() -> Holdout:
    return load_holdout(CONFIGS / "validation" / "holdout.toml")


def test_recent_six_months_are_holdout(h: Holdout) -> None:
    assert h.version == "HD-2026-10-03.1" and h.status == "PROPOSED"
    assert h.contains(date(2026, 4, 1)) and h.contains(date(2026, 10, 1)) and h.contains(date(2026, 6, 15))
    assert not h.contains(date(2021, 9, 30)) and not h.contains(date(2026, 10, 2))


def test_twenty_percent_of_earlier_weeks_whole_weeks_and_stable(h: Holdout) -> None:
    days = [date(2021, 10, 1) + timedelta(days=i) for i in range((date(2026, 4, 1) - date(2021, 10, 1)).days)]
    n_weeks = len({d.isocalendar()[:2] for d in days})
    assert len(h.earlier_weeks) == round(0.2 * n_weeks)
    for d in days[3:]:  # a week is in or out as a whole (after the partial first week: 1-Oct-2021 is a Friday)
        assert h.contains(d) == h.contains(d - timedelta(days=d.weekday()))
    share = sum(h.contains(d) for d in days) / len(days)
    assert 0.17 < share < 0.23
    again = load_holdout(CONFIGS / "validation" / "holdout.toml")
    assert again.earlier_weeks == h.earlier_weeks  # deterministic: a hash of the salt and the week, no data
    other = h.model_copy(update={"selection_salt": "x"})
    assert other.earlier_weeks != h.earlier_weeks


def test_prior_looks_and_version_lookup(h: Holdout) -> None:
    assert h.prior_looks["S-ORB-001"] == h.prior_looks["S-ORB-002"] == 2  # pipeline checks + the 3-Oct read
    assert len(h.prior_looks) == 18  # the public research library (private candidates: the alpha library's copy)
    assert all(v >= 1 for v in h.prior_looks.values())
    with pytest.raises(ConfigError):
        load_holdout(CONFIGS / "validation" / "holdout.toml", version="HD-2026-01-01.1")

"""Default entry caps (owner defaults, 2-Oct-2026): a new or uncapped strategy gets 1 a day; the library strategies
get 1-3 by style (docs/research/strategy-hypotheses.md "Entry caps"); H17 scalping is held at 1 until the cost-drag
study justifies more; the
system-wide cap stays at 10."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from project100c.kernel.limits import load_risk_limits
from project100c.spec.io import load_spec_file
from project100c.spec.models import SCALPING_MAX_ENTRIES_PER_DAY, StrategySpec
from tests.data.dhan_fakes import CONFIGS, REPO

SPECS = REPO / "specs"
# the documented table (docs/research/strategy-hypotheses.md "Entry caps"): change both together
DOCUMENTED = {
    "S-GAPGO-001": 1, "S-GAPFADE-001": 1, "S-ORB-001": 1, "S-ORB-002": 1, "S-FBO-001": 2, "S-VWAPC-001": 2,
    "S-VWAPMR-001": 2,
    "S-VOLX-001": 2, "S-IVRV-001": 1, "S-EXP0-001": 1, "S-VIXSTR-001": 1, "S-EVTBO-001": 1, "S-LUNCH-001": 1,
    "S-DAYVOL-001": 1, "S-NOISE-001": 1, "S-IMOM-001": 1, "S-GEXMO-001": 1,
    "S-VOLCHEAP-001": 1, "S-VOLHOLD-001": 1, "S-EXPVOL-001": 1,
    "S-TRDAY-001": 1, "S-NR7-001": 1, "S-OOPS-001": 1, "S-PBTREND-001": 1,
}  # fmt: skip


def raw(sid: str = "S-VWAPMR-001") -> dict[str, Any]:
    d: dict[str, Any] = load_spec_file(SPECS / f"{sid}.yaml").model_dump(mode="json")
    return d


def test_the_shipped_caps_match_the_documented_table_and_stay_in_1_to_3() -> None:
    shipped = {p.stem: load_spec_file(p).entry.max_entries_per_day for p in sorted(SPECS.glob("S-*.yaml"))}
    assert shipped == DOCUMENTED
    assert all(1 <= n <= 3 for n in shipped.values())
    design = (REPO / "docs" / "research" / "strategy-hypotheses.md").read_text(encoding="utf-8")
    for sid, n in DOCUMENTED.items():
        assert f"| {sid} | {n} |" in design, sid  # each cap is documented with its reason


def test_an_uncapped_spec_gets_one_a_day() -> None:
    d = raw()
    del d["entry"]["max_entries_per_day"]
    assert StrategySpec.model_validate(d).entry.max_entries_per_day == 1


def test_the_system_cap_stays_at_10() -> None:
    assert load_risk_limits(CONFIGS / "risk" / "limits.toml").max_trades_per_day == 10


def test_scalping_is_held_at_one_entry_a_day_until_the_cost_study_justifies_more() -> None:
    d = raw()
    d["id"] = "S-SCALP-001"
    d["entry"]["max_entries_per_day"] = SCALPING_MAX_ENTRIES_PER_DAY
    assert StrategySpec.model_validate(d).entry.max_entries_per_day == 1
    d["entry"]["max_entries_per_day"] = 6
    with pytest.raises(ValidationError, match="cost-drag study"):
        StrategySpec.model_validate(d)
    assert (REPO / "docs" / "research" / "cost-drag-study.md").exists()

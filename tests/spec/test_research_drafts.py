"""Research specs H19-H23 (docs/research/institutional-intelligence-layer.md): StrategySpecs at RESEARCH status,
checked by the project's own spec loader. A draft lives in specs/drafts/ until it has a library plug-in and a documented
entry cap (tests/strategies/test_library.py, tests/spec/test_entry_cap_defaults.py); then it moves to specs/. H19-H22
were promoted on 3-Oct-2026 (plug-ins in strategies/library/signals_intel.py). H23 stays a draft: it is a veto on
H01b signals evaluated as a research overlay (scripts/orbml_overlay.py), not a library plug-in."""

from __future__ import annotations

from datetime import time

import pytest

from project100c.kernel.limits import load_risk_limits
from project100c.spec.io import load_spec_file
from project100c.spec.models import MANDATORY_PROHIBITED, LegRight, Lifecycle, StrategySpec
from tests.data.dhan_fakes import CONFIGS, REPO

DRAFTS = REPO / "specs" / "drafts"
# draft id -> docs/research/strategy-hypotheses.md hypothesis number (change both together)
EXPECTED = {
    "S-DAYVOL-001": "H19", "S-NOISE-001": "H20", "S-IMOM-001": "H21", "S-GEXMO-001": "H22", "S-ORBML-001": "H23",
}  # fmt: skip
DOC = REPO / "docs" / "research" / "institutional-intelligence-layer.md"


PROMOTED = {"S-DAYVOL-001", "S-NOISE-001", "S-IMOM-001", "S-GEXMO-001"}


def draft(sid: str) -> StrategySpec:  # authored: RESEARCH, evidence pending, no confidence
    return load_spec_file((REPO / "specs" if sid in PROMOTED else DRAFTS) / f"{sid}.yaml")


def test_the_drafts_are_exactly_the_documented_set() -> None:
    assert {p.stem for p in DRAFTS.glob("S-*.yaml")} == set(EXPECTED) - PROMOTED
    for sid in EXPECTED:
        assert draft(sid).id == sid


@pytest.mark.parametrize("sid", sorted(EXPECTED))
def test_each_draft_is_a_long_only_research_spec_inside_the_owner_windows(sid: str) -> None:
    sp = draft(sid)
    assert sp.status is Lifecycle.RESEARCH and sp.evidence.is_pending()
    assert all(leg.side == "BUY" for leg in sp.legs)  # OD-006: no selling, no spread legs
    assert sp.entry.window_start >= time(9, 20) and sp.entry.window_end <= time(14, 0)  # OD-008
    assert sp.exit.time_exit <= time(14, 50)  # before the forced flatten (OD-002 hard flat 15:00)
    assert MANDATORY_PROHIBITED <= sp.prohibited_regimes and sp.falsification_criteria
    assert len(sp.cost_sensitivity) >= 20 and sp.sizing.max_lots == 1
    assert sp.entry.max_entries_per_day == 1  # new-strategy default (OD-014 style)
    assert not sp.event_certified


def test_a_spec_is_either_a_draft_or_in_the_library_never_both() -> None:
    library = {p.stem for p in (REPO / "specs").glob("S-*.yaml")}
    assert library & set(EXPECTED) == PROMOTED


def test_the_two_leg_spec_is_a_straddle_with_the_od013_allowance() -> None:
    sp = draft("S-DAYVOL-001")
    assert {leg.right for leg in sp.legs} == {LegRight.CE, LegRight.PE}
    assert "OD-013" in sp.sizing.method
    limits = load_risk_limits(CONFIGS / "risk" / "limits.toml")
    assert limits.version == "RL-2026-10-03.2" and "OD-013" in limits.owner_decisions
    assert limits.lot_cap("S-DAYVOL-001") == 2  # OD-013: a long straddle may hold 2 lots (one CE + one PE)


def test_each_draft_is_registered_in_design_12_and_the_research_doc() -> None:
    design = (REPO / "docs" / "research" / "strategy-hypotheses.md").read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    for sid, h in EXPECTED.items():
        assert f"### {h} — `{sid}`" in design, sid
        assert sid in doc and h in doc, sid

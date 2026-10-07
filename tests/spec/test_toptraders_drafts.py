"""Top-traders shortlist (a literature review of documented traders): TT-1..TT-4 were drafted at RESEARCH status in
specs/drafts/top-traders/ and promoted to specs/ on 3-Oct-2026 as H37-H40 (plug-ins in strategies/library/signals_tt.py,
pre-registered before any run). Still RESEARCH."""

from __future__ import annotations

from datetime import time

import pytest

from project100c.spec.io import load_spec_file
from project100c.spec.models import MANDATORY_PROHIBITED, Lifecycle, StrategySpec
from tests.data.dhan_fakes import REPO

DRAFTS = REPO / "specs" / "drafts" / "top-traders"
LIBRARY = REPO / "specs"
EXPECTED = {"S-TRDAY-001": "TT-1", "S-NR7-001": "TT-2", "S-OOPS-001": "TT-3", "S-PBTREND-001": "TT-4"}


def draft(sid: str) -> StrategySpec:  # authored: RESEARCH, evidence pending, no confidence
    return load_spec_file(LIBRARY / f"{sid}.yaml")


def test_the_drafts_were_promoted_out_of_the_draft_folder() -> None:
    assert not list(DRAFTS.glob("*.yaml"))
    for sid in EXPECTED:
        assert draft(sid).id == sid


@pytest.mark.parametrize("sid", sorted(EXPECTED))
def test_each_draft_is_a_long_only_research_spec_inside_the_owner_windows(sid: str) -> None:
    sp = draft(sid)
    assert sp.status is Lifecycle.RESEARCH and sp.evidence.is_pending()
    assert [leg.side for leg in sp.legs] == ["BUY"]  # OD-006: one long leg, no selling, no spreads
    assert sp.entry.window_start >= time(9, 20) and sp.entry.window_end <= time(14, 0)  # OD-008/OD-009
    assert sp.exit.time_exit <= time(14, 50)  # before the forced flatten (OD-002 hard flat 15:00)
    assert MANDATORY_PROHIBITED <= sp.prohibited_regimes and sp.eligible_regimes and sp.falsification_criteria
    assert len(sp.cost_sensitivity) >= 20 and sp.sizing.max_lots == 1
    assert sp.entry.max_entries_per_day == 1 and not sp.event_certified


def test_the_promoted_specs_are_in_the_library_not_the_h19_draft_folder() -> None:
    from project100c.strategies.library import PLUGINS

    assert not {p.stem for p in (REPO / "specs" / "drafts").glob("S-*.yaml")} & set(EXPECTED)
    assert set(EXPECTED) <= {p.stem for p in LIBRARY.glob("S-*.yaml")} and set(EXPECTED) <= set(PLUGINS)


def test_each_draft_is_registered_in_the_hypothesis_catalogue() -> None:
    design = (REPO / "docs" / "research" / "strategy-hypotheses.md").read_text(encoding="utf-8")
    for sid, tt in EXPECTED.items():
        assert f"### {tt} — `{sid}`" in design, sid

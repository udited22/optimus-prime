"""OD-019 research structures: the defined-risk schema, payoff maths and the live path staying long-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from project100c.errors import SpecValidationError
from project100c.spec.io import load_spec_file
from project100c.spec.structure import StructureSpec, load_structure_file
from project100c.structures import LegPos, expiry_payoff, is_defined_risk, margin_estimate, max_loss_points, shock_value

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "examples" / "structures"
FILES = sorted(DEMO.glob("X-*.yaml"))  # SYNTHETIC examples; real candidates live in the private alpha library


def ic(credit_each: float = 10.0, wing_cost: float = 3.0, qty: int = 65) -> list[LegPos]:
    return [LegPos(-1, True, 25200, qty, credit_each), LegPos(-1, False, 24800, qty, credit_each),
            LegPos(1, True, 25300, qty, wing_cost), LegPos(1, False, 24700, qty, wing_cost)]  # fmt: skip


def test_iron_condor_max_loss_is_width_minus_credit() -> None:
    legs = ic()
    credit = 2 * (10 - 3)
    assert is_defined_risk(legs)
    assert max_loss_points(legs) == pytest.approx((100 - credit) * 65)
    assert expiry_payoff(legs, 25000) == pytest.approx(credit * 65)


def test_naked_short_call_is_undefined_and_a_short_put_loses_its_strike() -> None:
    assert max_loss_points([LegPos(-1, True, 25000, 65, 50)]) == float("inf")
    assert max_loss_points([LegPos(-1, False, 25000, 65, 50)]) == pytest.approx((25000 - 50) * 65)


def test_shock_never_beats_max_loss_for_a_defined_risk_structure() -> None:
    legs = ic()
    for move in (-0.13, 0.13):
        assert -shock_value(legs, 25000, move, 0.86, 1 / 252) <= max_loss_points(legs) + 1e-6


def test_margin_estimate_adds_elm_and_the_expiry_day_extra() -> None:
    legs = ic()
    base = margin_estimate(legs, 25000, elm_frac=0.02, expiry_extra_frac=0.02, expiry_day=False)
    exp = margin_estimate(legs, 25000, elm_frac=0.02, expiry_extra_frac=0.02, expiry_day=True)
    assert base == pytest.approx(max_loss_points(legs) + 65 * 25000 * 0.02)
    assert exp - base == pytest.approx(65 * 25000 * 0.02)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_structure_file_loads_as_research_and_defined_risk(path: Path) -> None:
    sp = load_structure_file(path)
    assert sp.status == "RESEARCH" and sp.scope == "RESEARCH_PAPER_OD019" and sp.max_lots == 1
    overnight = [f"{sp.id}-{v.code}" for v in sp.variants if v.holds_overnight]
    assert sp.needs_od002_exception() == overnight  # overnight = research only until an OD-002 exception
    if sp.id == "X-DEMO-SPREAD-001":  # the only intraday-only example: expiry day only, no gate, no target/stop
        assert overnight == [] and sp.variants[0].expiry_day_only and sp.gate == "NONE"
        assert sp.target_frac_of_credit is None and sp.stop_loss_multiple_of_credit is None
    else:
        assert overnight == [f"{sp.id}-O"]


def test_the_synthetic_examples_exist_and_are_marked() -> None:
    assert [p.stem for p in FILES] == ["X-DEMO-CS-001", "X-DEMO-IC-001", "X-DEMO-SPREAD-001", "X-DEMO-STRANGLE-001"]
    for p in FILES:
        assert p.read_text().startswith("# SYNTHETIC EXAMPLE") and "DEMO ONLY" in p.read_text()


def test_expiry_day_only_must_be_an_intraday_expiry_entry() -> None:
    d = load_structure_file(DEMO / "X-DEMO-SPREAD-001.yaml").model_dump(mode="json")
    d["variants"][0]["allow_expiry_day_entry"] = False
    with pytest.raises(ValueError, match="expiry_day_only"):
        StructureSpec.model_validate(d)


def test_an_uncovered_short_is_refused() -> None:
    d = load_structure_file(DEMO / "X-DEMO-IC-001.yaml").model_dump(mode="json")
    d["leg_sets"]["ALWAYS"] = [
        {"side": "SELL", "right": "CE", "offset": 4},
        {"side": "BUY", "right": "PE", "offset": -6},
    ]
    with pytest.raises(ValueError, match="UNDEFINED_RISK"):
        StructureSpec.model_validate(d)
    d["leg_sets"]["ALWAYS"] = [
        {"side": "SELL", "right": "CE", "offset": 4},
        {"side": "BUY", "right": "CE", "offset": 3},
    ]
    with pytest.raises(ValueError, match="UNDEFINED_RISK"):  # the long is nearer the money: not a cover
        StructureSpec.model_validate(d)


def test_the_live_strategy_loader_still_refuses_these_files() -> None:
    with pytest.raises(SpecValidationError):
        load_spec_file(FILES[0])

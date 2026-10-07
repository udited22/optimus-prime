"""StrategySpec regime policy, invalidation rules and data availability (docs/architecture/strategyspec.md)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from project100c.errors import SpecValidationError
from project100c.spec import load_authored_spec, parse_yaml
from project100c.spec.models import (
    REGIME_CONDITIONS,
    REGIME_DIMENSIONS,
    DataAvailability,
    InvalidationKind,
    Regime,
    StrategySpec,
)
from project100c.spec.regime_policy import (
    RegimeCheck,
    RegimePolicy,
    regime_permits,
    triggered_regime_invalidations,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "tests" / "fixtures" / "spec" / "S-ORB-001.yaml"
R = Regime


@pytest.fixture
def raw() -> dict[str, Any]:
    return parse_yaml(EXAMPLE.read_text())


@pytest.fixture
def spec(raw: dict[str, Any]) -> StrategySpec:
    return load_authored_spec(raw)


def tags(*t: Regime) -> frozenset[Regime]:
    return frozenset(t)


def test_the_example_spec_carries_invalidation_rules_and_data_availability(spec: StrategySpec) -> None:
    kinds = {r.kind for r in spec.invalidation_rules}
    assert {InvalidationKind.PRICE_LEVEL, InvalidationKind.REGIME_CHANGE, InvalidationKind.TIME} <= kinds
    fut = next(d for d in spec.required_data if d.dataset == "nifty_fut_1m")
    assert fut.availability is DataAvailability.UNCERTAIN
    assert fut.substitute and "index" in fut.substitute


def test_a_permitted_regime(spec: StrategySpec) -> None:
    p = RegimePolicy.from_spec(spec)
    v = regime_permits(p, tags(R.TRENDING_UP, R.VOLATILITY_EXPANSION))
    assert v.permitted and v.check is RegimeCheck.OK
    # GAP_REGIME is listed as eligible: a condition there is permitted, never required
    assert regime_permits(p, tags(R.TRENDING_DOWN, R.VOLATILITY_EXPANSION, R.GAP_REGIME)).permitted


@pytest.mark.parametrize(
    ("t", "check", "fragment"),
    [
        (tags(), RegimeCheck.REGIME_UNKNOWN, "no regime tags"),
        (tags(R.NO_EDGE), RegimeCheck.REGIME_BLOCKED, "NO_EDGE"),
        (tags(R.TRENDING_UP, R.VOLATILITY_EXPANSION, R.EVENT_REGIME), RegimeCheck.REGIME_BLOCKED, "EVENT_REGIME"),
        (tags(R.TRENDING_UP, R.VOLATILITY_EXPANSION, R.ABNORMAL_MARKET), RegimeCheck.REGIME_BLOCKED, "ABNORMAL"),
        (tags(R.MEAN_REVERTING, R.VOLATILITY_EXPANSION), RegimeCheck.REGIME_NOT_ALLOWED, "trend is MEAN_REVERTING"),
        (tags(R.TRENDING_UP, R.VOLATILITY_NORMAL), RegimeCheck.REGIME_NOT_ALLOWED, "volatility is VOLATILITY_NORMAL"),
        (tags(R.TRENDING_UP), RegimeCheck.REGIME_NOT_ALLOWED, "volatility not decided"),
    ],
)
def test_refusals(spec: StrategySpec, t: frozenset[Regime], check: RegimeCheck, fragment: str) -> None:
    v = regime_permits(RegimePolicy.from_spec(spec), t)
    assert not v.permitted
    assert v.check is check
    assert fragment in v.detail


def test_required_conditions_must_all_be_present() -> None:
    p = RegimePolicy(
        "S-X-001",
        eligible=tags(R.OPENING_DRIVE),
        prohibited=tags(R.NO_EDGE, R.ABNORMAL_MARKET),
        required=tags(R.EXPIRY_REGIME),
    )
    assert regime_permits(p, tags(R.OPENING_DRIVE, R.MEAN_REVERTING, R.EXPIRY_REGIME)).permitted
    v = regime_permits(p, tags(R.OPENING_DRIVE))
    assert v.check is RegimeCheck.REGIME_NOT_ALLOWED and "EXPIRY_REGIME" in v.detail
    # opening named but not decided yet
    v2 = regime_permits(p, tags(R.TRENDING_UP, R.EXPIRY_REGIME))
    assert v2.check is RegimeCheck.REGIME_NOT_ALLOWED and "opening not decided" in v2.detail


def test_dimensions_the_spec_does_not_name_are_unconstrained() -> None:
    p = RegimePolicy("S-X-001", eligible=tags(R.VOLATILITY_COMPRESSION), prohibited=tags(R.NO_EDGE, R.ABNORMAL_MARKET))
    for trend in REGIME_DIMENSIONS["trend"]:
        assert regime_permits(p, tags(trend, R.VOLATILITY_COMPRESSION)).permitted


_ALL = sorted(Regime)


@given(st.frozensets(st.sampled_from(_ALL)), st.frozensets(st.sampled_from(_ALL), min_size=1))
def test_property_ok_means_every_rule_holds(t: frozenset[Regime], eligible: frozenset[Regime]) -> None:
    prohibited = tags(R.NO_EDGE, R.ABNORMAL_MARKET, R.EVENT_REGIME) - eligible
    p = RegimePolicy("S-P-001", eligible=eligible, prohibited=prohibited)
    v = regime_permits(p, t)
    if v.permitted:
        assert t
        assert not t & prohibited
        for values in REGIME_DIMENSIONS.values():
            if eligible & values:
                assert t & values & eligible
    else:
        assert v.detail


def test_regime_change_invalidations_trigger_on_their_tags(spec: StrategySpec) -> None:
    hit = triggered_regime_invalidations(spec.invalidation_rules, tags(R.MEAN_REVERTING, R.VOLATILITY_EXPANSION))
    assert len(hit) == 1 and hit[0].kind is InvalidationKind.REGIME_CHANGE
    assert not triggered_regime_invalidations(spec.invalidation_rules, tags(R.TRENDING_UP, R.VOLATILITY_EXPANSION))


@pytest.mark.parametrize(
    ("mutate", "msg"),
    [
        (lambda r: r.update(invalidation_rules=[]), "invalidation_rules"),
        (lambda r: r.update(required_regimes=["TRENDING_UP"]), "must be conditions"),
        (lambda r: r.update(required_regimes=["EVENT_REGIME"]), "both required and prohibited"),
        (
            lambda r: r["invalidation_rules"].append(
                {"kind": "REGIME_CHANGE", "description": "no regimes listed here", "action": "EXIT_POSITION"}
            ),
            "must list the regimes",
        ),
        (
            lambda r: r["invalidation_rules"].append(
                {
                    "kind": "TIME",
                    "description": "a time rule with regimes",
                    "action": "EXIT_POSITION",
                    "regimes": ["NO_EDGE"],
                }
            ),
            "only REGIME_CHANGE",
        ),
        (
            lambda r: r["required_data"].append(
                {"dataset": "x_1m", "granularity": "1m", "availability": "UNAVAILABLE"}
            ),
            "needs a named substitute",
        ),
    ],
)
def test_spec_validation_of_the_new_fields(raw: dict[str, Any], mutate: Any, msg: str) -> None:
    bad = copy.deepcopy(raw)
    mutate(bad)
    with pytest.raises(SpecValidationError, match=msg):
        load_authored_spec(bad)


def test_conditions_and_dimensions_partition_the_tags() -> None:
    dims = frozenset().union(*REGIME_DIMENSIONS.values())
    assert dims | REGIME_CONDITIONS == frozenset(Regime)
    assert not dims & REGIME_CONDITIONS

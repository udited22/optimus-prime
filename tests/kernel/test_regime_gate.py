"""The Governor's regime gate (check 17): the spec's policy, keyed by strategy id, against the K-11 reading."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.calendar import MarketClock
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.kernel.governor import Reason, RiskGovernor
from project100c.kernel.limits import RiskLimits
from project100c.kernel.regime_gate import RegimeGate, RegimeReading
from project100c.kernel.state import OrderKind
from project100c.regime import RegimeClassifier, load_regime_config
from project100c.spec import load_authored_spec, parse_yaml
from project100c.spec.models import Lifecycle, Regime
from project100c.synthetic import DayPlan, Segment, generate_day

from .conftest import DAY, KEY, day, intent, ist, market

EXAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "spec" / "S-ORB-001.yaml"
GOOD = frozenset({Regime.TRENDING_UP, Regime.VOLATILITY_EXPANSION})


@pytest.fixture(scope="module")
def gated(limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str) -> RiskGovernor:
    spec = load_authored_spec(parse_yaml(EXAMPLE.read_text()))
    return RiskGovernor(limits, market_clock, costs, plan_id, regime_gate=RegimeGate.from_specs([spec]))


def reading(t: frozenset[Regime] = GOOD, *, validated: bool = True, age_s: int = 30) -> RegimeReading:
    return RegimeReading(t, ist(10, 0) - timedelta(seconds=age_s), "RC-TEST", validated)


def test_a_permitted_validated_regime_approves(gated: RiskGovernor) -> None:
    d = gated.evaluate(intent(), day().state, market(regime=reading()))
    assert d.approved, d.reasons


def test_an_unvalidated_classifier_means_no_edge_for_live_money(gated: RiskGovernor) -> None:
    d = gated.evaluate(intent(), day().state, market(regime=reading(validated=False)))
    assert not d.approved
    assert d.reasons == (Reason.REGIME_BLOCKED,)
    assert any("UNVALIDATED" in n and "NO_EDGE" in n for n in d.notes)
    prod = gated.evaluate(
        intent(strategy_status=Lifecycle.PRODUCTION), day().state, market(regime=reading(validated=False))
    )
    assert Reason.REGIME_BLOCKED in prod.reasons


def test_shadow_sees_the_unvalidated_labels_as_they_are(gated: RiskGovernor) -> None:
    ok = gated.evaluate(intent(strategy_status=Lifecycle.SHADOW), day().state, market(regime=reading(validated=False)))
    assert ok.approved and ok.ticket is not None and ok.ticket.simulate_only
    bad = frozenset({Regime.MEAN_REVERTING, Regime.VOLATILITY_EXPANSION})
    no = gated.evaluate(
        intent(strategy_status=Lifecycle.SHADOW), day().state, market(regime=reading(bad, validated=False))
    )
    assert no.reasons == (Reason.REGIME_NOT_ALLOWED,)


@pytest.mark.parametrize(
    ("r", "reason"),
    [
        (None, Reason.REGIME_UNKNOWN),
        (reading(age_s=600), Reason.REGIME_STALE),
        (reading(age_s=-60), Reason.REGIME_STALE),  # a reading from the future is not trusted either
        (reading(frozenset()), Reason.REGIME_UNKNOWN),
        (reading(GOOD | {Regime.EVENT_REGIME}), Reason.REGIME_BLOCKED),
        (reading(frozenset({Regime.TRENDING_UP, Regime.VOLATILITY_NORMAL})), Reason.REGIME_NOT_ALLOWED),
    ],
)
def test_refusals(gated: RiskGovernor, r: RegimeReading | None, reason: Reason) -> None:
    d = gated.evaluate(intent(), day().state, market(regime=r))
    assert reason in d.reasons
    assert not d.approved


def test_a_strategy_without_a_registered_policy_is_refused(gated: RiskGovernor) -> None:
    d = gated.evaluate(intent(strategy_id="S-NEW-001"), day().state, market(regime=reading()))
    assert d.reasons == (Reason.REGIME_NOT_ALLOWED,)
    assert any("no regime policy" in n for n in d.notes)


def test_exits_are_never_blocked_by_the_regime(gated: RiskGovernor) -> None:
    e = day()
    e.add(
        "ORDER_SUBMITTED", client_order_id="E1", strategy_id="S-ORB-001", instrument_key=KEY, side="BUY", qty=65,
        price=D("3.00"), kind="ENTRY", lot_size=65,
    )  # fmt: skip
    e.add("FILL", client_order_id="E1", trade_id="T1", qty=65, price=D("3.00"), charges=D("5"))
    ex = intent(
        side=OrderSide.SELL, exit_kind=OrderKind.EXIT, limit_price=D("2.90"), stop_trigger=None, stop_limit=None
    )
    d = gated.evaluate(ex, e.state, market(regime=None))
    assert d.approved


def test_without_a_gate_the_governor_ignores_the_regime(gov: RiskGovernor) -> None:
    assert gov.evaluate(intent(), day().state, market(regime=None)).approved
    assert gov.evaluate(intent(), day().state, market(regime=reading(frozenset({Regime.NO_EDGE})))).approved


def test_a_reading_from_a_real_classifier_label(configs_dir: Path) -> None:
    cfg = load_regime_config(configs_dir / "regime" / "classifier.toml")
    d = generate_day(DayPlan(DAY, 1, segments=(Segment(60, 0.006, 10),)))
    clf = RegimeClassifier(cfg)
    clf.start_session(DAY, D(25000))
    lab = None
    for b in d.index:
        lab = clf.on_bar(b)
    assert lab is not None
    r = RegimeReading.from_label(lab)
    assert r.as_of == lab.ts and r.classifier == cfg.version
    assert r.validated is False  # the shipped classifier is UNVALIDATED
    assert r.tags == lab.tags()


def test_gate_rejects_a_non_positive_max_age() -> None:
    with pytest.raises(ValueError, match="positive"):
        RegimeGate([], max_age=timedelta(0))


def test_the_policy_is_looked_up_by_strategy_id(gated: RiskGovernor) -> None:
    # the same intent under another strategy id does not borrow S-ORB-001's policy
    d = gated.evaluate(replace(intent(), strategy_id="S-VWAPMR-001"), day().state, market(regime=reading()))
    assert Reason.REGIME_NOT_ALLOWED in d.reasons

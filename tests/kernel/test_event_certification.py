"""Event certification in the Governor (OD-014 default, 2-Oct-2026): on an EVENT_REGIME day only a strategy whose
spec is ``event_certified`` may enter. When the Governor is built from the specs, the spec decides; the intent's
own flag cannot vouch for itself. Exits are never blocked."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from project100c.calendar import MarketClock
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.kernel.governor import Reason, RiskGovernor, event_certified_from_specs
from project100c.kernel.limits import RiskLimits
from project100c.kernel.regime_gate import RegimeReading
from project100c.kernel.state import OrderKind
from project100c.spec.models import Regime
from tests.strategies.library_rig import spec

from .conftest import day, intent, ist, market
from .test_governor import _long

EVENT_TAGS = frozenset({Regime.TRENDING_UP, Regime.VOLATILITY_NORMAL, Regime.EVENT_REGIME})


def _gov(limits: RiskLimits, clock: MarketClock, costs: CostModel, plan: str, certified: set[str]) -> RiskGovernor:
    return RiskGovernor(limits, clock, costs, plan, event_certified_strategies=certified)


def _event_reading() -> RegimeReading:
    return RegimeReading(EVENT_TAGS, ist(9, 59) - timedelta(seconds=30), "RC-TEST", validated=True)


def test_uncertified_spec_is_refused_even_if_the_intent_claims_certification(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    g = _gov(limits, market_clock, costs, plan_id, set())
    st = day().state
    for spoof in (False, True):
        d = g.evaluate(intent(event_certified=spoof), st, market(event_day=True))
        assert Reason.EVENT_DAY_NOT_CERTIFIED in d.reasons and not d.approved


def test_an_event_regime_tag_alone_makes_it_an_event_day(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    g = _gov(limits, market_clock, costs, plan_id, set())
    d = g.evaluate(intent(), day().state, market(regime=_event_reading()))
    assert Reason.EVENT_DAY_NOT_CERTIFIED in d.reasons
    assert Reason.EVENT_DAY_NOT_CERTIFIED not in g.evaluate(intent(), day().state, market()).reasons


def test_a_certified_spec_passes_the_event_check(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    g = _gov(limits, market_clock, costs, plan_id, {"S-ORB-001"})
    for m in (market(event_day=True), market(regime=_event_reading())):
        d = g.evaluate(intent(), day().state, m)
        assert Reason.EVENT_DAY_NOT_CERTIFIED not in d.reasons and d.approved, d.reasons
    other = g.evaluate(intent(strategy_id="S-GAPGO-001"), day().state, market(event_day=True))
    assert Reason.EVENT_DAY_NOT_CERTIFIED in other.reasons


def test_exits_are_never_blocked_on_an_event_day(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    g = _gov(limits, market_clock, costs, plan_id, set())
    ex = intent(side=OrderSide.SELL, exit_kind=OrderKind.EXIT, limit_price=D("2.90"), stop_trigger=None,
                stop_limit=None)  # fmt: skip
    d = g.evaluate(ex, _long().state, market(event_day=True, regime=_event_reading()))
    assert d.approved, d.reasons
    assert Reason.EVENT_DAY_NOT_CERTIFIED not in d.reasons


def test_built_from_specs_no_library_strategy_is_certified() -> None:
    ids = ["S-ORB-001", "S-VIXSTR-001", "S-EVTBO-001", "S-GAPGO-001"]
    assert event_certified_from_specs([spec(s) for s in ids]) == frozenset()

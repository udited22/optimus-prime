"""PAPER on a paper venue (docs/research/paper-trading.md §14.1): the Governor accepts a PAPER intent only when it was
built for a paper
venue, and the runtime only lets such a Governor drive a broker that declares ``paper_venue``. Every other rule is
the live rule. Plus ``cancel_entries``, the cancel-only path a strategy uses for an entry it no longer wants."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.broker import FakeBroker, OrderStatus, OrderType
from project100c.calendar import MarketClock
from project100c.costs import CostModel
from project100c.errors import KernelInvariantError
from project100c.journal import Journal
from project100c.kernel.governor import Reason, RiskGovernor, Verdict
from project100c.kernel.limits import RiskLimits
from project100c.kernel.regime_gate import RegimeGate, RegimeReading
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.spec.models import Lifecycle, Regime

from .conftest import DAY, KEY, Clock, day, intent, ist, market, quote


@pytest.fixture
def paper_gov(limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str) -> RiskGovernor:
    return RiskGovernor(limits, market_clock, costs, plan_id, paper_venue=True)


def test_a_default_governor_refuses_paper(gov: RiskGovernor) -> None:
    assert gov.paper_venue is False
    d = gov.evaluate(intent(strategy_status=Lifecycle.PAPER), day().state, market())
    assert d.verdict is Verdict.REJECT and Reason.STRATEGY_STATUS in d.reasons
    assert any("paper venue" in n for n in d.notes)


def test_a_paper_venue_governor_approves_paper_and_sends_it(paper_gov: RiskGovernor) -> None:
    d = paper_gov.evaluate(intent(strategy_status=Lifecycle.PAPER), day().state, market())
    assert d.verdict is Verdict.APPROVE and d.ticket is not None and not d.ticket.simulate_only
    for st in (Lifecycle.RESEARCH, Lifecycle.VALIDATED, Lifecycle.QUARANTINED):  # still refused
        assert Reason.STRATEGY_STATUS in paper_gov.evaluate(intent(strategy_status=st), day().state, market()).reasons


def test_paper_keeps_every_live_limit(paper_gov: RiskGovernor) -> None:
    late = paper_gov.evaluate(intent(strategy_status=Lifecycle.PAPER, decided_at=ist(14, 5)), day().state,
                              market(ist(14, 5), q=quote(ts=ist(14, 5))))  # fmt: skip
    assert late.verdict is Verdict.REJECT  # the entry cutoff
    big = paper_gov.evaluate(intent(strategy_status=Lifecycle.PAPER, qty=130), day().state, market())
    assert Reason.MAX_POSITION in big.reasons  # the 1-lot cap


def test_an_unvalidated_classifier_is_no_edge_for_canary_but_read_as_is_for_paper(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    from tests.strategies.library_rig import spec

    sp = spec("S-GAPGO-001")
    gate = RegimeGate.from_specs([sp])
    reading = RegimeReading(
        frozenset({Regime.TRENDING_UP, Regime.GAP_REGIME, Regime.OPENING_DRIVE, Regime.VOLATILITY_NORMAL}),
        ist(9, 59),
        "RC-T",
        validated=False,
    )
    i = intent(strategy_id=sp.id, strategy_status=Lifecycle.PAPER)
    m = market(regime=reading)
    paper = RiskGovernor(limits, market_clock, costs, plan_id, regime_gate=gate, paper_venue=True)
    assert paper.evaluate(i, day().state, m).verdict is Verdict.APPROVE
    d = paper.evaluate(replace(i, strategy_status=Lifecycle.CANARY), day().state, m)
    assert d.verdict is Verdict.REJECT and any("UNVALIDATED" in n for n in d.notes)


class RealLookingBroker(FakeBroker):
    paper_venue = False  # stands in for a real-broker adapter


def test_the_runtime_refuses_a_paper_governor_on_a_broker_that_is_not_a_paper_venue(
    tmp_path: Path, paper_gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c = Clock(ist(9, 16))
    j = Journal(tmp_path / "j.db", clock=c)
    with pytest.raises(KernelInvariantError, match="paper_venue"):
        KernelRuntime(j, RealLookingBroker(c, funds=D(10000)), paper_gov, costs, plan_id, c, MemoryAlerts())
    KernelRuntime(j, FakeBroker(c, funds=D(10000)), paper_gov, costs, plan_id, c, MemoryAlerts())  # a paper venue


def test_cancel_entries_cancels_a_working_entry_and_never_a_stop(
    tmp_path: Path, paper_gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c = Clock(ist(9, 16))
    b = FakeBroker(c, funds=D(10000))
    b.set_quote(KEY, D("3.40"), D("3.45"))  # above the limit: the entry rests
    rt = KernelRuntime(Journal(tmp_path / "j.db", clock=c), b, paper_gov, costs, plan_id, c, MemoryAlerts())
    rt.start_day(DAY, D(10000), week_start=True)
    c.t = ist(10, 0)
    assert rt.submit(intent(strategy_status=Lifecycle.PAPER), market(q=quote())).approved  # decided at 3.00
    rt.step({KEY: quote("3.40", "3.45")})  # the market moved away
    assert rt.cancel_entries(KEY) == 1 and rt.state.open_orders == {}
    assert [o.status for o in b.orders()] == [OrderStatus.CANCELLED]
    # filled and protected: cancel_entries leaves the stop alone
    assert rt.submit(intent(intent_id="I2", strategy_status=Lifecycle.PAPER), market(q=quote())).approved
    b.set_quote(KEY, D("2.95"), D("3.00"))
    rt.step({KEY: quote()})
    c.t += timedelta(seconds=1)
    rt.step({KEY: quote()})
    assert KEY in rt.state.positions
    assert rt.cancel_entries(KEY) == 0
    assert any(o.request.order_type is OrderType.SL and o.status is OrderStatus.TRIGGER_PENDING for o in b.orders())

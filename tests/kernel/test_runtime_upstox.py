"""The kernel runtime through the production ExecutionGateway over the Upstox adapter, against the in-memory
Upstox fake (no network). Same code path a live venue uses; every number is SIMULATED."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

from project100c.broker import OrderStatus, OrderType
from project100c.broker.upstox import UpstoxBroker, UpstoxSession
from project100c.costs import CostModel
from project100c.execution import ExecutionGateway
from project100c.journal import Journal
from project100c.kernel.governor import RiskGovernor
from project100c.kernel.kills import HaltKind
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.market_types import Quote
from tests.broker.upstox_fake import TOKEN, FakeUpstox

from .conftest import DAY, KEY, Clock, intent, ist, market, quote


def _rig(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> tuple[Clock, FakeUpstox, KernelRuntime, MemoryAlerts, UpstoxBroker]:
    c = Clock(ist(9, 16))
    fake = FakeUpstox(c)
    fake.set_quote(KEY, "2.95", "3.00")
    ub = UpstoxBroker(UpstoxSession(TOKEN, ist(8, 0)), clock=c, transport=fake)
    alerts = MemoryAlerts()
    rt = KernelRuntime(Journal(tmp_path / "j.db", clock=c), ExecutionGateway(ub, c), gov, costs, plan_id, c, alerts)
    rt.start_day(DAY, D("10000"), week_start=True)
    c.t = ist(10, 0)
    return c, fake, rt, alerts, ub


def test_entry_protective_and_stop_out_through_upstox(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c, fake, rt, _alerts, ub = _rig(tmp_path, gov, costs, plan_id)
    assert rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t))).approved
    rt.step({KEY: quote(ts=c.t)})
    c.adv(1)
    rt.step({KEY: quote(ts=c.t)})
    assert rt.state.positions[KEY].protective_confirmed
    (s,) = [o for o in ub.orders() if o.request.order_type is OrderType.SL]
    assert s.status is OrderStatus.TRIGGER_PENDING and s.request.qty == 65
    c.adv(60)
    fake.set_quote(KEY, "2.25", "2.30")
    rt.step({KEY: quote("2.25", "2.30", ts=c.t)})
    assert rt.state.positions == {} and rt.state.kills == {}
    assert rt.state.realised_today < 0


def test_od007_exit_all_at_1500_through_upstox(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c, fake, rt, alerts, ub = _rig(tmp_path, gov, costs, plan_id)
    rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t)))
    rt.step({KEY: quote(ts=c.t)})
    c.adv(1)
    rt.step({KEY: quote(ts=c.t)})
    c.t = ist(14, 50)
    rt.step({KEY: Quote(KEY, c.t, c.t, None, D("3.00"), None, None, None, None)})  # no bid: cannot flatten
    assert KEY in rt.state.positions
    c.t = ist(15, 0)
    fake.set_quote(KEY, "2.90", "3.00")
    rt.step({KEY: quote("2.90", "3.00", ts=c.t)})
    assert rt.state.positions == {}
    assert HaltKind.EXIT_ALL_FAILED not in rt.state.halts
    assert ("POST", "/v2/order/positions/exit") in fake.calls
    assert [p.net_qty for p in ub.positions()] == [0]
    assert not [o for o in ub.orders() if o.status in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING)]
    assert any("Exit-All completed" in m for m in alerts.urgent())

"""K-09: the standalone reconciler, an independent journal-vs-broker check on its own timer. Fake broker only;
every number is SIMULATED."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from project100c.broker import BrokerPosition, FakeBroker, OrderRequest, OrderType
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.errors import BrokerDisconnectedError
from project100c.execution import ReconcilerService
from project100c.journal import Journal
from project100c.kernel.governor import RiskGovernor
from project100c.kernel.kills import KillSwitch
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts

from .conftest import DAY, KEY, Clock, intent, ist, market, quote

Rig = tuple[Clock, FakeBroker, KernelRuntime, MemoryAlerts, ReconcilerService]


def _rig(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str, *, unknown: bool = True) -> Rig:
    c = Clock(ist(9, 16))
    b = FakeBroker(c, funds=D("100000"))
    b.set_quote(KEY, D("2.95"), D("3.00"))
    alerts = MemoryAlerts()
    rt = KernelRuntime(Journal(tmp_path / "j.db", clock=c), b, gov, costs, plan_id, c, alerts)
    rt.start_day(DAY, D("10000"), week_start=True)
    c.t = ist(10, 0)
    rc = ReconcilerService(
        lambda: rt.state,
        b,
        alerts,
        rt.report_reconciliation_mismatch,
        unknown_orders=(lambda: rt.unknown_orders) if unknown else (lambda: ()),
    )
    return c, b, rt, alerts, rc


def _run(c: Clock, rt: KernelRuntime, rc: ReconcilerService, seconds: float, step: float = 0.5) -> None:
    end = c.t + timedelta(seconds=seconds)
    while c.t < end:
        rt.step({KEY: quote(ts=c.t)})
        rc.tick(c.t)
        c.adv(step)


def test_a_normal_trade_is_clean(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> None:
    c, _, rt, _, rc = _rig(tmp_path, gov, costs, plan_id)
    assert rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t))).approved
    _run(c, rt, rc, 20)
    assert rt.state.positions[KEY].protective_confirmed
    assert rc.reports == [] and rc.polls >= 7 and rt.state.kills == {}


def test_a_phantom_position_latches_the_kill_within_6_seconds(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c, b, rt, alerts, rc = _rig(tmp_path, gov, costs, plan_id)
    rc.tick(c.t)
    start = c.t
    b.add_phantom_position("NSE_FO|PHANTOM-CE", 65)
    while (KillSwitch.POSITION_RECONCILIATION, "") not in rt.state.kills:
        c.adv(0.5)
        rc.tick(c.t)
        assert c.t - start <= timedelta(seconds=6)
    assert rc.reports == ["UNEXPECTED_POSITION NSE_FO|PHANTOM-CE: broker 65, journal 0"]
    assert any("reconciler" in m for m in alerts.urgent())
    for _ in range(20):  # reported once while it persists
        c.adv(0.5)
        rc.tick(c.t)
    assert len(rc.reports) == 1


def test_an_order_the_journal_never_sent_is_found(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c, b, rt, _, rc = _rig(tmp_path, gov, costs, plan_id)
    b.place(OrderRequest("ROGUE.1", KEY, OrderSide.BUY, 65, OrderType.LIMIT, D("2.00")))  # rests, never fills
    for _ in range(3):
        rc.tick(c.t)
        c.adv(2.5)
    assert rc.reports and rc.reports[0].startswith("UNKNOWN_BROKER_ORDER ROGUE.1")
    assert (KillSwitch.POSITION_RECONCILIATION, "") in rt.state.kills


class _Flaky:
    """Wraps a broker: one read shows a phantom (a fill in flight), or reads fail."""

    def __init__(self, b: FakeBroker) -> None:
        self.b, self.blip, self.fail = b, 0, 0

    def positions(self) -> list[BrokerPosition]:
        if self.fail:
            self.fail -= 1
            raise BrokerDisconnectedError("link down")
        out = self.b.positions()
        if self.blip:
            self.blip -= 1
            out.append(BrokerPosition("NSE_FO|INFLIGHT-CE", 65, 65, 0, D("195"), D(0)))
        return out

    def __getattr__(self, name: str) -> object:
        return getattr(self.b, name)


def test_a_one_read_blip_is_not_reported_and_read_errors_alert(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    c, b, rt, alerts, rc = _rig(tmp_path, gov, costs, plan_id)
    fl = _Flaky(b)
    rc.broker = fl  # type: ignore[assignment]
    fl.blip = 1
    for _ in range(4):
        rc.tick(c.t)
        c.adv(2.5)
    assert rc.reports == [] and rt.state.kills == {}
    fl.fail = 3
    for _ in range(3):
        assert rc.tick(c.t) is None
        c.adv(2.5)
    assert rc.read_errors == 3 and any("3 broker reads failed" in m for m in alerts.urgent())
    assert rc.tick(c.t) is not None and rc.read_errors == 0
    assert rc.tick(c.t) is None  # not due again yet


def test_an_unknown_order_is_left_to_the_runtime(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    for unknown, reported in ((True, False), (False, True)):
        (tmp_path / str(unknown)).mkdir()
        c, b, rt, _, rc = _rig(tmp_path / str(unknown), gov, costs, plan_id, unknown=unknown)
        b.faults.timeout_next_places.append(False)  # timed out and never reached the broker
        rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t)))
        assert rt.unknown_orders
        for _ in range(3):
            rc.tick(c.t)
            c.adv(2.5)
        assert bool(rc.reports) is reported

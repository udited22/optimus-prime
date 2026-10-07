"""K-12: the supervised host loop, its persistent journal, backups, and the restart drill. Paper venue only;
every fill is SIMULATED."""

from __future__ import annotations

import signal
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.broker import OrderStatus, OrderType
from project100c.broker.paper import PaperBroker
from project100c.calendar import MarketClock
from project100c.costs import CostModel
from project100c.execution import ExecutionGateway, ReconcilerService
from project100c.journal import Journal
from project100c.kernel.governor import MarketSnapshot, RiskGovernor, TradeIntent
from project100c.kernel.limits import RiskLimits
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.kernel.tickets import TicketSigner
from project100c.market_types import Quote
from project100c.ops import TokenGate
from project100c.ops.host import EXIT_FAILED, EXIT_STOPPED, HostService, backup_journal, open_journal
from project100c.paper.live import LivePaperSession

from .conftest import DAY, KEY, Clock, intent, ist, market, quote


class Feed:
    def __init__(self, clock: Clock) -> None:
        self.clock, self.bid, self.ask = clock, "2.95", "3.00"

    def poll(self) -> Mapping[str, Quote]:
        return {KEY: quote(self.bid, self.ask, ts=self.clock.t, qty=1000)}


GovArgs = tuple[RiskLimits, MarketClock, CostModel, str]


class Proc:
    """One host process: everything except the broker and the journal file is new."""

    def __init__(self, jpath: Path, pb: PaperBroker, c: Clock, feed: Feed, gov_args: GovArgs) -> None:
        limits, mc, costs, plan_id = gov_args
        signer = TicketSigner()  # a new key per process
        gov = RiskGovernor(limits, mc, costs, plan_id, ticket_signer=signer)
        self.alerts = MemoryAlerts()
        self.journal = open_journal(jpath, clock=c, forbid_under=jpath.parent / "never")
        self.rt = KernelRuntime(
            self.journal,
            ExecutionGateway(pb, c, ticket_signer=signer),
            gov,
            costs,
            plan_id,
            c,
            self.alerts,
        )
        self.gate = TokenGate(
            lambda: ist(3, 30) + timedelta(days=1), self.alerts, broker_name="X", ref_factory=lambda: "R1"
        )
        self.wanted: list[tuple[TradeIntent, MarketSnapshot]] = []

        def decide(now: datetime, q: Mapping[str, Quote]) -> list[tuple[TradeIntent, MarketSnapshot]]:
            out, self.wanted[:] = list(self.wanted), []
            return out

        session = LivePaperSession(self.rt, pb, feed, decide, c, self.alerts, gate=self.gate)
        rc = ReconcilerService(
            lambda: self.rt.state,
            pb,
            self.alerts,
            self.rt.report_reconciliation_mismatch,
            unknown_orders=lambda: self.rt.unknown_orders,
        )
        self.host = HostService(session, c, self.alerts, reconciler=rc, sleep=lambda s: c.adv(1))
        self.rc = rc
        self.gate.start(c.t)
        self.gate.on_token(ist(3, 30) + timedelta(days=1), c.t)


@pytest.mark.parametrize("ending", ["stop_out", "owner_kill"])
def test_restart_drill_rebuilds_state_without_duplicate_orders(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str, ending: str
) -> None:
    c = Clock(ist(9, 16))
    feed = Feed(c)
    pb = PaperBroker(c, funds=D("10000"))  # stands in for the broker, which outlives the host process
    args: GovArgs = (limits, market_clock, costs, plan_id)
    jpath = tmp_path / "host" / "journal.db"
    a = Proc(jpath, pb, c, feed, args)
    assert a.host.ensure_day_started(DAY, D("10000"), week_start=True)
    c.t = ist(10, 0)
    a.wanted.append((intent(decided_at=c.t), market(c.t, q=quote(ts=c.t))))
    assert a.host.run(max_steps=6) == EXIT_STOPPED
    assert a.rt.state.positions[KEY].protective_confirmed and a.rt.state.kills == {}
    orders_before = [(o.request.client_order_id, o.status) for o in pb.orders()]
    assert len(orders_before) == 2
    a.journal.close()  # the process dies (a crash would not close it; SQLite is durable either way)

    b = Proc(jpath, pb, c, feed, args)
    assert not b.host.ensure_day_started(DAY, D("10000"), week_start=True)  # no second DAY_START
    assert any("restarted" in m for _, m in b.alerts.sent)
    assert b.rt.state.positions[KEY].protective_confirmed  # rebuilt from the journal
    assert b.host.run(max_steps=20) == EXIT_STOPPED
    assert [(o.request.client_order_id, o.status) for o in pb.orders()] == orders_before  # nothing new, nothing lost
    (sl,) = [o for o in pb.orders() if o.request.order_type is OrderType.SL]
    assert sl.status is OrderStatus.TRIGGER_PENDING
    assert b.rt.state.kills == {} and b.rc.reports == [] and b.host.total_failures == 0
    if ending == "stop_out":  # the protective stop placed by process A still protects process B's position
        feed.bid, feed.ask = "2.25", "2.30"
        assert b.host.run(max_steps=10) == EXIT_STOPPED
        assert b.rt.state.positions == {} and b.rt.state.realised_today < 0 and b.rt.state.kills == {}
    else:  # process B can cancel process A's protective stop and flatten
        assert b.rt.manual_master_kill("restart drill", requested_by="test")
        assert b.host.run(max_steps=10) == EXIT_STOPPED
        assert b.rt.state.positions == {} and b.rt.state.open_orders == {}
        assert all(o.status is not OrderStatus.TRIGGER_PENDING for o in pb.orders())


class Boom:
    def __init__(self, rt: KernelRuntime, fail: list[bool]) -> None:
        self.runtime, self.fail = rt, fail

    def step(self) -> object:
        if self.fail and self.fail.pop(0):
            raise RuntimeError("feed decode failed")
        return None


def _rt(tmp_path: Path, limits: RiskLimits, mc: MarketClock, costs: CostModel, plan: str, c: Clock) -> KernelRuntime:
    pb = PaperBroker(c, funds=D("10000"))
    gov = RiskGovernor(limits, mc, costs, plan)
    return KernelRuntime(Journal(tmp_path / "j.db", clock=c), pb, gov, costs, plan, c, MemoryAlerts())


def test_failed_steps_are_alerted_and_the_service_exits_for_the_supervisor(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    c = Clock(ist(10, 0))
    alerts = MemoryAlerts()
    s = Boom(_rt(tmp_path, limits, market_clock, costs, plan_id, c), [True, True, False, True, True, True])
    h = HostService(s, c, alerts, max_consecutive_failures=3, sleep=lambda _: None)
    assert h.run() == EXIT_FAILED
    assert h.steps == 1 and h.total_failures == 5 and h.failures == 3
    assert sum("host step failed" in m for m in alerts.urgent()) == 5
    assert "supervisor restarts it" in alerts.urgent()[-1]


def test_stop_and_signals(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    c = Clock(ist(10, 0))
    h = HostService(
        Boom(_rt(tmp_path, limits, market_clock, costs, plan_id, c), []),
        c,
        MemoryAlerts(),
        sleep=lambda _: h.stop.set(),
    )
    assert h.run() == EXIT_STOPPED and h.steps == 1
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        h.stop.clear()
        h.install_stop_signals()
        signal.raise_signal(signal.SIGTERM)
        assert h.stop.is_set()
    finally:
        for s, f in old.items():
            signal.signal(s, f)
    with pytest.raises(ValueError):
        HostService(h.session, c, MemoryAlerts(), max_consecutive_failures=0)


def test_journal_location_and_backup(tmp_path: Path) -> None:
    c = Clock(ist(10, 0))
    with pytest.raises(ValueError, match="persistent disk"):
        open_journal(tmp_path / "j.db", clock=c, forbid_under=tmp_path)
    j = open_journal(tmp_path / "data" / "journal.db", clock=c, forbid_under=tmp_path / "scratch")
    for i in range(3):
        j.append("ALERT", {"severity": "INFO", "message": f"m{i}"})
    out = backup_journal(tmp_path / "data" / "journal.db", tmp_path / "backup", c.t)
    j.append("ALERT", {"severity": "INFO", "message": "after"})
    assert out.name == "journal-20261005T100000.db"
    copy = Journal(out, clock=c)
    assert [r.payload["message"] for r in copy.records()] == ["m0", "m1", "m2"]

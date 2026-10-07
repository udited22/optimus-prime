"""P-02 paper mode on live quotes: PaperBroker behind the production gateway and runtime, driven by the host step
(token gate, Telegram commands, strategy intents). A scripted quote source stands in for the live feed; every fill
is SIMULATED."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.broker import FakeBroker, OrderRequest, OrderStatus, OrderType
from project100c.broker.paper import PaperBroker
from project100c.calendar import MarketClock
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.errors import BrokerError, KernelInvariantError
from project100c.execution import ExecutionGateway
from project100c.journal import Journal
from project100c.kernel.governor import MarketSnapshot, RiskGovernor, TradeIntent
from project100c.kernel.kills import KillSwitch
from project100c.kernel.limits import RiskLimits
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.market_types import Quote
from project100c.ops import GateState, TokenGate
from project100c.paper.live import LivePaperSession
from project100c.spec.models import Lifecycle

from .conftest import DAY, KEY, Clock, intent, ist, market, quote


class Feed:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.bid, self.ask, self.qty = "2.95", "3.00", 1000

    def poll(self) -> Mapping[str, Quote]:
        return {KEY: quote(self.bid, self.ask, ts=self.clock.t, qty=self.qty)}


@dataclass
class Cmd:
    command: str
    arg: str = ""


class Cmds:
    def __init__(self) -> None:
        self.pending: list[Cmd] = []

    def poll_commands(self) -> list[Cmd]:
        out, self.pending = self.pending, []
        return out


# -- PaperBroker ---------------------------------------------------------------------------------------------


def _buy(cid: str = "P1.ENTRY.1", qty: int = 65) -> OrderRequest:
    return OrderRequest(cid, KEY, OrderSide.BUY, qty, OrderType.LIMIT, D("3.00"))


def test_paper_fills_at_the_ask_only_after_latency() -> None:
    c = Clock(ist(10, 0))
    pb = PaperBroker(c, funds=D("10000"))
    assert pb.paper_venue is True
    pb.on_quotes({KEY: quote(ts=c.t)})
    oid = pb.place(_buy())
    pb.on_quotes({KEY: quote(ts=c.t)})
    assert pb.orders()[0].status is OrderStatus.PENDING_ACK
    c.adv(1)
    pb.on_quotes({KEY: quote(ts=c.t)})
    (o,) = pb.orders()
    assert o.broker_order_id == oid and o.status is OrderStatus.FILLED and o.avg_fill_price == D("3.00")


def test_paper_depth_haircut_gives_a_partial_fill() -> None:
    c = Clock(ist(10, 0))
    pb = PaperBroker(c, funds=D("10000"), ack_latency=timedelta(0), fill_latency=timedelta(0))
    pb.on_quotes({KEY: quote(ts=c.t, qty=100)})  # 100 shown, 50 ours
    pb.place(_buy())
    o = pb.orders()[0]
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == 50
    pb.tick()  # the same snapshot cannot be used twice
    assert pb.orders()[0].filled_qty == 50
    pb.on_quotes({KEY: quote(ts=c.t, qty=100)})  # a fresh snapshot: the rest fills
    assert pb.orders()[0].status is OrderStatus.FILLED


@pytest.mark.parametrize("age_s", [4, -4])
def test_paper_stale_or_future_quote_never_fills(age_s: int) -> None:
    c = Clock(ist(10, 0))
    pb = PaperBroker(c, funds=D("10000"), ack_latency=timedelta(0), fill_latency=timedelta(0))
    pb.on_quotes({KEY: quote(ts=c.t - timedelta(seconds=age_s))})
    pb.place(_buy())
    pb.on_quotes({KEY: quote(ts=c.t - timedelta(seconds=age_s))})
    assert pb.orders()[0].filled_qty == 0 and KEY in pb.stale
    pb.on_quotes({KEY: quote(ts=c.t)})
    assert pb.orders()[0].status is OrderStatus.FILLED and KEY not in pb.stale


def test_paper_missing_size_never_fills() -> None:
    c = Clock(ist(10, 0))
    pb = PaperBroker(c, funds=D("10000"), ack_latency=timedelta(0), fill_latency=timedelta(0))
    pb.on_quotes({KEY: Quote(KEY, c.t, c.t, D("2.95"), D("3.00"), None, None, None, None)})
    pb.place(_buy())
    pb.on_quotes({KEY: Quote(KEY, c.t, c.t, D("2.95"), D("3.00"), None, None, None, None)})
    assert pb.orders()[0].filled_qty == 0


@pytest.mark.parametrize(
    "kw",
    [
        {"depth_haircut": D("0")},
        {"depth_haircut": D("1.5")},
        {"ack_latency": timedelta(seconds=-1)},
        {"max_quote_age": timedelta(0)},
    ],
)
def test_paper_config_is_validated(kw: dict[str, object]) -> None:
    with pytest.raises(BrokerError):
        PaperBroker(Clock(ist(10, 0)), funds=D("1"), **kw)  # type: ignore[arg-type]


# -- the host step ---------------------------------------------------------------------------------------------


@dataclass
class Rig:
    clock: Clock
    feed: Feed
    pb: PaperBroker
    rt: KernelRuntime
    alerts: MemoryAlerts
    cmds: Cmds
    gate: TokenGate
    session: LivePaperSession
    wanted: list[tuple[TradeIntent, MarketSnapshot]]


def _rig(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> Rig:
    c = Clock(ist(8, 45))
    feed = Feed(c)
    pb = PaperBroker(c, funds=D("10000"))
    alerts = MemoryAlerts()
    rt = KernelRuntime(Journal(tmp_path / "j.db", clock=c), ExecutionGateway(pb, c), gov, costs, plan_id, c, alerts)
    rt.start_day(DAY, D("10000"), week_start=True)
    gate = TokenGate(lambda: ist(3, 30) + timedelta(days=1), alerts, broker_name="Upstox", ref_factory=lambda: "AB12CD")
    cmds = Cmds()
    wanted: list[tuple[TradeIntent, MarketSnapshot]] = []

    def decide(now: datetime, q: Mapping[str, Quote]) -> list[tuple[TradeIntent, MarketSnapshot]]:
        out = list(wanted)
        wanted.clear()
        return out

    s = LivePaperSession(rt, pb, feed, decide, c, alerts, gate=gate, commands=cmds)
    return Rig(c, feed, pb, rt, alerts, cmds, gate, s, wanted)


def _want(r: Rig, **kw: object) -> None:
    now = r.clock.t
    r.wanted.append((intent(decided_at=now, **kw), market(now, q=quote(ts=now))))


def test_no_token_no_entries(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> None:
    r = _rig(tmp_path, gov, costs, plan_id)
    r.gate.start(r.clock.t)
    r.clock.t = ist(10, 0)
    _want(r)
    rep = r.session.step()
    assert not rep.trading_allowed and rep.decisions == () and r.gate.state is GateState.LAPSED
    assert r.pb.orders() == []


def test_entry_fill_protective_and_status(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> None:
    r = _rig(tmp_path, gov, costs, plan_id)
    r.gate.start(r.clock.t)
    r.gate.on_token(ist(3, 30) + timedelta(days=1), r.clock.t)
    r.clock.t = ist(10, 0)
    _want(r)
    rep = r.session.step()
    assert rep.trading_allowed and len(rep.decisions) == 1 and rep.decisions[0].approved
    for _ in range(3):
        r.clock.adv(1)
        r.session.step()
    assert r.rt.state.positions[KEY].protective_confirmed
    sl = [o for o in r.pb.orders() if o.request.order_type is OrderType.SL]
    assert sl and sl[0].status is OrderStatus.TRIGGER_PENDING
    r.cmds.pending.append(Cmd("/status"))
    rep = r.session.step()
    assert rep.commands == ("/status",) and "PAPER (SIMULATED)" in r.alerts.sent[-1][1]


def test_deny_flattens_open_positions(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> None:
    r = _rig(tmp_path, gov, costs, plan_id)
    r.gate.start(r.clock.t)
    r.gate.on_token(ist(3, 30) + timedelta(days=1), r.clock.t)
    r.clock.t = ist(10, 0)
    _want(r)
    for _ in range(4):
        r.session.step()
        r.clock.adv(1)
    assert KEY in r.rt.state.positions
    r.cmds.pending.append(Cmd("/deny", "ab12cd"))
    rep = r.session.step()
    assert not rep.trading_allowed and rep.exits_requested == (KEY,)
    for _ in range(5):
        r.clock.adv(1)
        r.session.step()
    assert r.rt.state.positions == {}
    _want(r, intent_id="I2")
    assert r.session.step().decisions == ()


def test_kill_command_latches_the_master_kill(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    r = _rig(tmp_path, gov, costs, plan_id)
    r.gate.start(r.clock.t)
    r.gate.on_token(ist(3, 30) + timedelta(days=1), r.clock.t)
    r.clock.t = ist(10, 0)
    r.cmds.pending.append(Cmd("/kill"))
    _want(r)
    rep = r.session.step()
    assert (KillSwitch.MANUAL_MASTER, "") in r.rt.state.kills
    assert len(rep.decisions) == 1 and not rep.decisions[0].approved
    r.cmds.pending.append(Cmd("/resume"))
    assert r.session.step().commands == ("ignored /resume",)


def test_paper_governor_needs_the_paper_venue(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    c = Clock(ist(9, 16))
    pg = RiskGovernor(limits, market_clock, costs, plan_id, paper_venue=True)
    pb = PaperBroker(c, funds=D("10000"))
    rt = KernelRuntime(
        Journal(tmp_path / "a.db", clock=c), ExecutionGateway(pb, c), pg, costs, plan_id, c, MemoryAlerts()
    )
    rt.start_day(DAY, D("10000"), week_start=True)
    c.t = ist(10, 0)
    assert rt.submit(intent(decided_at=c.t, strategy_status=Lifecycle.PAPER), market(c.t, q=quote(ts=c.t))).approved

    class NotPaper(FakeBroker):
        paper_venue = False

    with pytest.raises(KernelInvariantError):
        KernelRuntime(
            Journal(tmp_path / "b.db", clock=c),
            ExecutionGateway(NotPaper(c, funds=D("1")), c),
            pg,
            costs,
            plan_id,
            c,
            MemoryAlerts(),
        )

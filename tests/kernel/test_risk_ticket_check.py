"""K-07: the gateway sends an ENTRY only with the Governor's signed, unexpired RiskTicket for that exact order.
Local only (fake broker); every number is SIMULATED."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.broker import FakeBroker, OrderRequest, OrderType
from project100c.calendar import MarketClock
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.errors import TicketRejectedError
from project100c.execution import ExecutionGateway
from project100c.journal import Journal
from project100c.kernel.governor import RiskGovernor, RiskTicket
from project100c.kernel.kills import KillSwitch
from project100c.kernel.limits import RiskLimits
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.kernel.tickets import TicketSigner

from .conftest import DAY, KEY, Clock, day, intent, ist, market, quote


def _gov(limits: RiskLimits, mc: MarketClock, costs: CostModel, plan: str, s: TicketSigner | None) -> RiskGovernor:
    return RiskGovernor(limits, mc, costs, plan, ticket_signer=s)


def _ticket(gov: RiskGovernor, c: Clock) -> RiskTicket:
    d = gov.evaluate(intent(decided_at=c.t), day().state, market(c.t, q=quote(ts=c.t)))
    assert d.approved and d.ticket is not None
    return d.ticket


def _rig(
    tmp_path: Path, gov: RiskGovernor, gw_signer: TicketSigner | None, costs: CostModel, plan: str
) -> tuple[Clock, FakeBroker, ExecutionGateway, KernelRuntime, MemoryAlerts]:
    c = Clock(ist(9, 16))
    b = FakeBroker(c, funds=D("100000"))
    b.set_quote(KEY, D("2.95"), D("3.00"))
    gw = ExecutionGateway(b, c, ticket_signer=gw_signer)
    alerts = MemoryAlerts()
    rt = KernelRuntime(Journal(tmp_path / "j.db", clock=c), gw, gov, costs, plan, c, alerts)
    rt.start_day(DAY, D("10000"), week_start=True)
    c.t = ist(10, 0)
    return c, b, gw, rt, alerts


def _buy(px: str = "3.00", qty: int = 65, key: str = KEY, side: OrderSide = OrderSide.BUY) -> OrderRequest:
    return OrderRequest("I1.1", key, side, qty, OrderType.LIMIT, D(px), tag="ENTRY")


def test_sign_and_verify(limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str) -> None:
    s = TicketSigner()
    t = _ticket(_gov(limits, market_clock, costs, plan_id, s), Clock(ist(10, 0)))
    assert s.verify(t) and len(t.signature) == 64 and "key" in repr(s) and "hidden" in repr(s)
    assert t.instrument_key == KEY and t.side == "BUY" and t.qty == 65 and t.price_ceiling == D("3.00")
    for edit in (
        {"qty": 130},
        {"price_ceiling": D("3.50")},
        {"expires_at": t.expires_at + timedelta(hours=1)},
        {"instrument_key": "NSE_FO|OTHER"},
        {"risk_at_stop": D("1")},
        {"simulate_only": True},
    ):
        assert not s.verify(replace(t, **edit))
    assert not TicketSigner().verify(t)  # another process's key
    assert not s.verify(replace(t, signature=""))
    with pytest.raises(ValueError):
        TicketSigner(b"short")


def test_valid_ticket_reaches_the_broker(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    s = TicketSigner()
    c, b, _, rt, _ = _rig(tmp_path, _gov(limits, market_clock, costs, plan_id, s), s, costs, plan_id)
    assert rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t))).approved
    assert len(b.orders()) == 1 and rt.state.kills == {}
    rt.step({KEY: quote(ts=c.t)})
    c.adv(1)
    rt.step({KEY: quote(ts=c.t)})
    assert rt.state.positions[KEY].protective_confirmed  # the protective stop needs no ticket
    assert rt.state.kills == {}


def test_unsigned_ticket_is_refused_and_latches_system_integrity(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    c, b, _, rt, alerts = _rig(
        tmp_path, _gov(limits, market_clock, costs, plan_id, None), TicketSigner(), costs, plan_id
    )
    rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t)))
    assert b.orders() == []
    assert (KillSwitch.SYSTEM_INTEGRITY, "") in rt.state.kills
    assert rt.state.open_orders == {}
    assert any("GATEWAY_TICKET" in m and "signature invalid" in m for m in alerts.urgent())


@pytest.mark.parametrize(
    ("edit", "req", "why"),
    [
        ({"qty": 130}, _buy(qty=130), "signature invalid"),  # forged: edited after signing
        ({}, _buy(qty=130), "qty 130"),
        ({}, _buy(px="3.05"), "above the approved"),
        ({}, _buy(key="NSE_FO|OTHER-CE"), "instrument"),
        ({}, _buy(side=OrderSide.SELL), "side"),
    ],
)
def test_forged_or_mismatched_tickets_are_refused_before_the_broker(
    limits: RiskLimits,
    market_clock: MarketClock,
    costs: CostModel,
    plan_id: str,
    edit: dict[str, object],
    req: OrderRequest,
    why: str,
) -> None:
    s = TicketSigner()
    c = Clock(ist(10, 0))
    b = FakeBroker(c, funds=D("100000"))
    b.set_quote(KEY, D("2.95"), D("3.00"))
    gw = ExecutionGateway(b, c, ticket_signer=s)
    gw.attach_ticket("I1.1", replace(_ticket(_gov(limits, market_clock, costs, plan_id, s), c), **edit))  # type: ignore[arg-type]
    with pytest.raises(TicketRejectedError, match=why):
        gw.place(req)
    assert b.orders() == [] and gw.order_state("I1.1") is None


def test_expired_missing_and_reused_tickets_are_refused(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    s = TicketSigner()
    c = Clock(ist(10, 0))
    b = FakeBroker(c, funds=D("100000"))
    b.set_quote(KEY, D("2.95"), D("3.00"))
    gw = ExecutionGateway(b, c, ticket_signer=s)
    t = _ticket(_gov(limits, market_clock, costs, plan_id, s), c)
    with pytest.raises(TicketRejectedError, match="no RiskTicket"):
        gw.place(_buy())
    gw.attach_ticket("I1.1", t)
    c.t = t.expires_at
    with pytest.raises(TicketRejectedError, match="not valid at"):
        gw.place(_buy())
    with pytest.raises(TicketRejectedError, match="no RiskTicket"):  # single use: the failed check consumed it
        gw.place(_buy())
    assert b.orders() == []
    # only ENTRY-tagged orders need a ticket (protective stops and exits are risk-reducing)
    assert gw.place(OrderRequest("I0.1", KEY, OrderSide.BUY, 65, OrderType.LIMIT, D("3.00"), tag="EXIT"))


def test_no_signer_keeps_the_old_behaviour(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    c, b, _, rt, _ = _rig(tmp_path, _gov(limits, market_clock, costs, plan_id, None), None, costs, plan_id)
    assert rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t))).approved
    assert len(b.orders()) == 1 and rt.state.kills == {}

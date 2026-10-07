"""Hypothesis properties for K-02 (docs/risk/risk-engine.md §9.5)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from project100c.broker import FakeBroker
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.journal import Journal
from project100c.kernel.governor import RiskGovernor
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.kernel.state import OrderKind
from project100c.sessions import IST

from .conftest import DAY, KEY, Clock, day, intent, ist, market, quote

TICK = D("0.05")
ticks = st.integers(min_value=1, max_value=600).map(lambda n: TICK * n)
secs = st.integers(min_value=0, max_value=24 * 3600 - 1)


@st.composite
def entries(draw: st.DrawFn) -> tuple[D, D, int, int, int]:
    n = draw(st.integers(min_value=2, max_value=300))  # entry premium in ticks (Rs0.10 - Rs15)
    gap = draw(st.integers(min_value=1, max_value=min(n - 1, 60)))
    lots = draw(st.sampled_from([1, 1, 1, 2, 3]))
    nav = draw(st.integers(2000, 200000))
    t = draw(st.one_of(st.integers(9 * 3600 + 15 * 60, 14 * 3600 + 5 * 60), secs))  # bias to the entry window
    return TICK * n, TICK * (n - gap), lots, nav, t


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(e=entries())
def test_no_approved_entry_exceeds_budget_or_window(gov: RiskGovernor, e: tuple[D, D, int, int, int]) -> None:
    entry, stop_limit, lots, nav, t = e
    now = datetime(DAY.year, DAY.month, DAY.day, tzinfo=IST) + timedelta(seconds=t)
    q = quote(str(entry - TICK), str(entry), ts=now)
    i = intent(
        qty=65 * lots,
        limit_price=entry,
        stop_trigger=stop_limit + TICK if stop_limit + TICK < entry else stop_limit,
        stop_limit=stop_limit,
        spec_stop_limit=stop_limit,
        decided_at=now,
    )
    d = gov.evaluate(i, day(str(nav)).state, market(now, cash=str(nav), q=q))
    event(f"verdict={d.verdict}")
    if d.approved:
        assert d.risk_at_stop is not None and d.budget is not None
        assert d.risk_at_stop <= d.budget == D("0.02") * nav
        assert lots == 1  # 1 lot max
        assert ist(9, 20) <= now < ist(14, 0)  # entry window 09:20 (OD-009) - 14:00 (OD-008)


actions = st.lists(
    st.tuples(
        st.sampled_from(["enter", "enter2", "sell", "move", "tick"]),
        st.integers(1, 120),  # seconds to advance
        st.integers(-20, 20),  # price move in ticks
    ),
    min_size=1,
    max_size=25,
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(seq=actions, start=st.integers(9 * 3600 + 14 * 60, 15 * 3600 + 5 * 60))
def test_runtime_invariants_under_random_sequences(
    gov: RiskGovernor, costs: CostModel, plan_id: str, seq: list[tuple[str, int, int]], start: int
) -> None:
    c = Clock(datetime(DAY.year, DAY.month, DAY.day, tzinfo=IST) + timedelta(seconds=start))
    b = FakeBroker(c, funds=D("10000"))
    ask = D("3.00")
    b.set_quote(KEY, ask - TICK, ask)
    with tempfile.TemporaryDirectory() as td, Journal(Path(td) / "j.db", clock=c) as j:
        rt = KernelRuntime(j, b, gov, costs, plan_id, c, MemoryAlerts())
        rt.start_day(DAY, D("10000"), week_start=True)
        n = 0
        for kind, dt, mv in seq:
            c.adv(dt)
            ask = max(TICK * 2, ask + TICK * mv)
            b.set_quote(KEY, ask - TICK, ask)
            q = quote(str(ask - TICK), str(ask), ts=c.t)
            n += 1
            if kind in ("enter", "enter2"):
                lots = 2 if kind == "enter2" else 1
                stop = max(TICK, ask - TICK * 10)
                rt.submit(
                    intent(
                        intent_id=f"I{n}",
                        qty=65 * lots,
                        limit_price=ask,
                        stop_trigger=stop + TICK,
                        stop_limit=stop,
                        spec_stop_limit=stop,
                        decided_at=c.t,
                    ),
                    market(c.t, q=q),
                )
            elif kind == "sell":
                rt.submit(
                    intent(
                        intent_id=f"S{n}",
                        side=OrderSide.SELL,
                        exit_kind=OrderKind.EXIT,
                        qty=65,
                        limit_price=ask - TICK,
                        decided_at=c.t,
                    ),
                    market(c.t, q=q),
                )
            rt.step({KEY: q})
            s = rt.state
            if s.positions:
                event("held a position")
            assert all(qty <= 65 for qty in s.open_qty().values())  # <= 1 lot
            assert all(p.net_qty >= 0 for p in b.positions())  # never net short at the broker
            assert sum(p.net_qty for p in b.positions()) <= 65
        for o in b.orders():
            t = o.request  # every order we sent obeys the window; only broker Exit-All may act at 15:00
            placed = [
                r
                for r in j.records()
                if r.event_type == "ORDER_SUBMITTED" and r.payload["client_order_id"] == t.client_order_id
            ]
            if t.tag == "EXIT_ALL":
                continue
            assert placed, "every broker order was journalled first"
            ts = placed[0].ts.astimezone(IST)
            assert ist(9, 15) <= ts < ist(15, 0)
            if t.tag == str(OrderKind.ENTRY):
                assert ist(9, 20) <= ts < ist(14, 0)

"""OD-014: the system-wide cap on new entries per day is 10 (was 3), and each spec has
its own ``entry.max_entries_per_day`` under it. The Governor and the allocator enforce both. Every other limit is
unchanged, so e.g. the daily-loss kill still stops entries long before the 10th."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from project100c.calendar import MarketClock
from project100c.costs import CostModel
from project100c.kernel.governor import Reason, RiskGovernor, Verdict, entry_caps_from_specs
from project100c.kernel.kills import KillSwitch
from project100c.kernel.limits import RiskLimits

from .conftest import DAY, Events, day, intent, market


def entries(n: int, sid: str = "S-ORB-001", nav: str = "100000", start: Events | None = None) -> Events:
    """``n`` round trips today, each one ENTRY order filled and closed flat (no position left)."""
    e = start or day(nav)
    for i in range(n):
        cid = f"E{sid}{i}"
        e.add("ORDER_SUBMITTED", client_order_id=cid, strategy_id=sid, instrument_key=f"K{i}", side="BUY", qty=65,
              price=D("3.00"), kind="ENTRY", lot_size=65)  # fmt: skip
        e.add("FILL", client_order_id=cid, trade_id=f"T{sid}{i}", qty=65, price=D("3.00"), charges=D("0"))
        x = f"X{sid}{i}"
        e.add("ORDER_SUBMITTED", client_order_id=x, strategy_id=sid, instrument_key=f"K{i}", side="SELL", qty=65,
              price=D("3.00"), kind="EXIT", lot_size=65)  # fmt: skip
        e.add("FILL", client_order_id=x, trade_id=f"U{sid}{i}", qty=65, price=D("3.00"), charges=D("0"))
    return e


def test_the_shipped_limits_record_od_014(limits: RiskLimits) -> None:
    assert limits.version == "RL-2026-10-03.2" and {"OD-013", "OD-014"} <= set(limits.owner_decisions)
    assert limits.max_trades_per_day == 10
    # every other limit is unchanged
    assert limits.per_trade_max_loss_frac == D("0.02") and limits.daily_stop_frac == D("0.04")
    assert limits.dd_suspend_frac == D("0.125") and limits.max_lots == 1 and limits.max_open_entry_orders == 1
    assert limits.lot_cap("S-VIXSTR-001") == 2  # the OD-013 straddle exception still applies


def test_the_state_counts_entries_per_strategy_and_resets_each_day() -> None:
    s = entries(2, start=entries(3, sid="S-GAPGO-001")).state
    assert s.entries_today == 5 and dict(s.entries_by_strategy) == {"S-GAPGO-001": 3, "S-ORB-001": 2}
    nxt = Events()
    nxt.state = s
    nxt.add("DAY_START", trading_date=DAY.replace(day=DAY.day + 1), sod_nav=D(100000), week_start=False)
    assert nxt.state.entries_today == 0 and dict(nxt.state.entries_by_strategy) == {}


def test_the_tenth_entry_passes_and_the_eleventh_is_refused(gov: RiskGovernor) -> None:
    s9 = entries(9, nav="100000").state
    ok = gov.evaluate(intent(), s9, market(cash="100000"))
    assert ok.verdict is Verdict.APPROVE, ok.reasons
    s10 = entries(10, nav="100000").state
    d = gov.evaluate(intent(), s10, market(cash="100000"))
    assert d.verdict is Verdict.REJECT and Reason.MAX_TRADES_PER_DAY in d.reasons
    assert any("system cap 10" in n for n in d.notes)


def test_the_daily_loss_kill_still_stops_entries_earlier(gov: RiskGovernor) -> None:
    e = entries(2, nav="10000")
    e.add("KILL_LATCHED", switch=str(KillSwitch.DAILY_LOSS), scope="", reason="realised loss over 4% of SOD NAV")
    d = gov.evaluate(intent(), e.state, market())
    assert e.state.entries_today == 2 and d.verdict is Verdict.REJECT
    assert Reason.KILL_ACTIVE in d.reasons and Reason.MAX_TRADES_PER_DAY not in d.reasons


def test_the_daily_headroom_also_binds_before_the_cap(gov: RiskGovernor) -> None:
    e = day("10000")
    e.add("ORDER_SUBMITTED", client_order_id="L", strategy_id="S-ORB-001", instrument_key="KL", side="BUY", qty=65,
          price=D("10.00"), kind="ENTRY", lot_size=65)  # fmt: skip
    e.add("FILL", client_order_id="L", trade_id="TL", qty=65, price=D("10.00"), charges=D("0"))
    e.add("ORDER_SUBMITTED", client_order_id="LX", strategy_id="S-ORB-001", instrument_key="KL", side="SELL", qty=65,
          price=D("4.30"), kind="EXIT", lot_size=65)  # fmt: skip
    e.add("FILL", client_order_id="LX", trade_id="TLX", qty=65, price=D("4.30"), charges=D("0"))  # -370.50 realised
    d = gov.evaluate(intent(), e.state, market())
    assert e.state.entries_today == 1 and Reason.DAILY_HEADROOM in d.reasons


@pytest.fixture
def capped(limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str) -> RiskGovernor:
    return RiskGovernor(limits, market_clock, costs, plan_id, strategy_entry_caps={"S-ORB-001": 3, "S-BIG": 50})


def test_the_spec_cap_binds_under_the_system_cap(capped: RiskGovernor) -> None:
    assert capped.evaluate(intent(), entries(2).state, market(cash="100000")).verdict is Verdict.APPROVE
    d = capped.evaluate(intent(), entries(3).state, market(cash="100000"))
    assert Reason.STRATEGY_MAX_ENTRIES in d.reasons and Reason.MAX_TRADES_PER_DAY not in d.reasons
    # another strategy's entries do not use this strategy's cap, but they do use the book's
    other = entries(3, sid="S-GAPGO-001").state
    assert Reason.STRATEGY_MAX_ENTRIES not in capped.evaluate(intent(), other, market(cash="100000")).reasons
    # a spec cap above the system cap is still held to 10
    big = capped.evaluate(intent(strategy_id="S-BIG"), entries(10, sid="S-BIG").state, market(cash="100000"))
    assert Reason.MAX_TRADES_PER_DAY in big.reasons and Reason.STRATEGY_MAX_ENTRIES not in big.reasons
    # a strategy without a configured cap gets 1 (fail closed)
    none = capped.evaluate(intent(strategy_id="S-NEW"), entries(1, sid="S-NEW").state, market(cash="100000"))
    assert Reason.STRATEGY_MAX_ENTRIES in none.reasons


def test_caps_come_from_the_specs() -> None:
    from tests.strategies.library_rig import spec

    caps = entry_caps_from_specs([spec("S-ORB-001"), spec("S-VIXSTR-001")])
    assert caps == {"S-ORB-001": 1, "S-VIXSTR-001": 1}  # a straddle is ONE entry (its second leg is not counted)


def test_an_unconfigured_governor_checks_the_system_cap_only(gov: RiskGovernor) -> None:
    d = gov.evaluate(intent(), entries(5).state, market(cash="100000"))
    assert d.verdict is Verdict.APPROVE  # no per-spec caps configured (e.g. the dashboard simulator)

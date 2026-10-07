"""OD-013: a two-leg LONG straddle may use 2 lots, one BUY CE plus one BUY PE with the
same underlying, expiry and strike, provided the COMBINED risk at the stops of both legs is at most 2% of NAV. It is
the only exception to the 1-lot canary cap (limits ``strategy_max_lots``: S-VIXSTR-001 and S-IVRV-001's straddle
variant). Every other strategy, shape and size keeps 1 lot."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.calendar import MarketClock
from project100c.core_types import OptionRight
from project100c.costs import CostModel
from project100c.errors import ConfigError
from project100c.kernel.governor import MarketSnapshot, Reason, RiskGovernor, TradeIntent, Verdict
from project100c.kernel.limits import RiskLimits, load_risk_limits

from .conftest import Events, contract, day, intent, market, quote

SID = "S-VIXSTR-001"
CE = "NSE_FO|NIFTY06OCT2625000CE"
PE = "NSE_FO|NIFTY06OCT2625000PE"
SHIPPED = 'strategy_max_lots = { "S-VIXSTR-001" = 2, "S-IVRV-001" = 2 }'


@pytest.fixture
def g(limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str) -> RiskGovernor:
    return RiskGovernor(limits, market_clock, costs, plan_id)  # fresh: the leg memory is per Governor


def leg(key: str, right: OptionRight = OptionRight.PE, sid: str = SID, **kw: object) -> TradeIntent:
    c = replace(contract(key), right=right)
    return intent(intent_id=f"I-{key[-2:]}", strategy_id=sid, contract=c, multi_leg=True, **kw)


def approve_ce(g: RiskGovernor, nav: str = "100000", sid: str = SID) -> None:
    d = g.evaluate(leg(CE, OptionRight.CE, sid), day(nav).state, market(cash=nav, q=quote(key=CE)))
    assert d.verdict is Verdict.APPROVE, d.reasons


def holding(key: str = CE, nav: str = "100000", *, sid: str = SID, protected: bool = True) -> Events:
    e = day(nav)
    e.add("ORDER_SUBMITTED", client_order_id="E1", strategy_id=sid, instrument_key=key, side="BUY", qty=65,
          price=D("3.00"), kind="ENTRY", lot_size=65)  # fmt: skip
    e.add("FILL", client_order_id="E1", trade_id="T1", qty=65, price=D("3.00"), charges=D("5"))
    if protected:
        e.add("ORDER_SUBMITTED", client_order_id="P1", strategy_id=sid, instrument_key=key, side="SELL", qty=65,
              price=D("2.25"), kind="PROTECTIVE", lot_size=65)  # fmt: skip
    return e


def pe_market(cash: str = "100000") -> MarketSnapshot:
    return market(cash=cash, q=quote(key=PE))


def test_the_shipped_limits_record_od_013(limits: RiskLimits) -> None:
    assert limits.version == "RL-2026-10-03.2" and "OD-013" in limits.owner_decisions
    assert limits.max_lots == 1  # the canary cap itself is unchanged
    assert limits.lot_cap(SID) == 2 and limits.lot_cap("S-IVRV-001") == 2
    assert limits.lot_cap("S-ORB-001") == 1 and limits.lot_cap("S-GAPGO-001") == 1


def test_a_ce_plus_pe_straddle_gets_its_second_lot(g: RiskGovernor) -> None:
    approve_ce(g)
    d = g.evaluate(leg(PE), holding().state, pe_market())
    assert d.verdict is Verdict.APPROVE, d.reasons


def test_the_combined_risk_over_2pct_is_refused(g: RiskGovernor) -> None:
    approve_ce(g, "10000")
    # unprotected first leg: its whole premium (INR 195) counts; with the new leg it exceeds 2% of INR 10,000
    d = g.evaluate(leg(PE), holding(nav="10000", protected=False).state, market(q=quote(key=PE)))
    assert d.verdict is Verdict.REJECT and Reason.RISK_BUDGET_EXCEEDED in d.reasons
    assert d.risk_at_stop is not None and d.budget is not None and d.risk_at_stop > d.budget
    # protected first leg: its stop risk (3.00 - 2.25) x 65 is added to the new leg's
    approve_ce(g)
    ok = g.evaluate(leg(PE), holding().state, pe_market())
    alone = g.evaluate(leg(PE), day("100000").state, pe_market())
    assert ok.risk_at_stop is not None and alone.risk_at_stop is not None
    assert ok.risk_at_stop - alone.risk_at_stop == D("48.75")
    # a NAV where each leg fits 2% alone but the pair does not
    nav = str(((alone.risk_at_stop + D("48.75")) / D("0.02")).quantize(D(1)) - 1)
    assert alone.risk_at_stop <= D("0.02") * D(nav)
    approve_ce(g, nav)
    tight = g.evaluate(leg(PE), holding(nav=nav).state, pe_market(nav))
    assert Reason.RISK_BUDGET_EXCEEDED in tight.reasons


@pytest.mark.parametrize(
    ("key", "right", "expiry", "strike"),
    [
        ("NSE_FO|NIFTY06OCT2625100CE", OptionRight.CE, None, D(25100)),  # CE + CE
        ("NSE_FO|NIFTY13OCT2625000PE", OptionRight.PE, date(2026, 10, 13), None),  # another expiry
        ("NSE_FO|NIFTY06OCT2624900PE", OptionRight.PE, None, D(24900)),  # a strangle, not a straddle
    ],
)
def test_a_second_lot_that_is_not_a_straddle_is_refused(
    g: RiskGovernor, key: str, right: OptionRight, expiry: date | None, strike: D | None
) -> None:
    approve_ce(g)
    c = replace(contract(key), right=right, expiry=expiry or contract().expiry, strike=strike or contract().strike)
    d = g.evaluate(replace(leg(key, right), contract=c), holding().state, market(cash="100000", q=quote(key=key)))
    assert d.verdict is Verdict.REJECT and Reason.NOT_A_STRADDLE in d.reasons


def test_every_other_two_lot_attempt_is_refused(g: RiskGovernor) -> None:
    approve_ce(g)
    s = holding().state
    single = g.evaluate(replace(leg(PE), multi_leg=False), s, pe_market())
    assert Reason.MAX_POSITION in single.reasons  # a single-leg intent keeps the 1-lot cap
    two = g.evaluate(leg(PE, qty=130), s, pe_market())
    assert Reason.MAX_POSITION in two.reasons  # one lot per leg
    same = g.evaluate(leg(CE, OptionRight.CE), s, market(cash="100000", q=quote(key=CE)))
    assert Reason.NO_AVERAGING in same.reasons  # the same instrument again is pyramiding
    other = g.evaluate(leg(PE, sid="S-ORB-001"), s, pe_market())
    assert Reason.MAX_POSITION in other.reasons  # a strategy without the exception, multi-leg or not
    # a strategy listed for the exception still needs the CE+PE shape: S-IVRV-001 holding another's leg
    ivrv = g.evaluate(leg(PE, sid="S-IVRV-001"), s, pe_market())
    assert Reason.MAX_POSITION in ivrv.reasons  # the open CE is S-VIXSTR-001's, not its own


def test_a_third_lot_and_a_leg_unknown_after_restart_are_refused(
    g: RiskGovernor, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    approve_ce(g)
    both = holding()
    both.add("ORDER_SUBMITTED", client_order_id="E2", strategy_id=SID, instrument_key=PE, side="BUY", qty=65,
             price=D("3.00"), kind="ENTRY", lot_size=65)  # fmt: skip
    third = "NSE_FO|NIFTY06OCT2625050CE"
    d = g.evaluate(leg(third, OptionRight.CE), both.state, market(cash="100000", q=quote(key=third)))
    assert Reason.MAX_POSITION in d.reasons
    fresh = RiskGovernor(limits, market_clock, costs, plan_id)  # e.g. after a restart: the first leg is unknown
    d2 = fresh.evaluate(leg(PE), holding().state, pe_market())
    assert Reason.NOT_A_STRADDLE in d2.reasons  # fail closed


def test_the_book_wide_cap_still_holds_around_a_straddle(g: RiskGovernor) -> None:
    # another strategy holds the canary's one lot: the straddle's first leg may not open a second
    d = g.evaluate(leg(CE, OptionRight.CE), holding(sid="S-ORB-001").state, market(cash="100000", q=quote(key=CE)))
    assert Reason.MAX_POSITION in d.reasons
    # the straddle holds a leg: no other strategy may open anything
    approve_ce(g)
    o = g.evaluate(replace(intent(contract=replace(contract(PE), right=OptionRight.PE))), holding().state, pe_market())
    assert Reason.MAX_POSITION in o.reasons


def test_the_cap_is_validated(limits: RiskLimits) -> None:
    for bad in ({SID: 3}, {SID: 0}, {"": 2}):
        with pytest.raises(ValueError, match="one lot per leg"):
            RiskLimits.model_validate(limits.model_dump() | {"strategy_max_lots": bad})


def test_a_malformed_edit_fails_loudly(configs_dir: Path, tmp_path: Path) -> None:
    src = (configs_dir / "risk" / "limits.toml").read_text(encoding="utf-8")
    assert src.count(SHIPPED) >= 1
    head, _, tail = src.rpartition(SHIPPED)  # the latest table
    p = tmp_path / "limits.toml"
    p.write_text(head + 'strategy_max_lots = { "S-VIXSTR-001" = 5 }' + tail, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_risk_limits(p)


# ---------------------------------------------------------------- a straddle is ONE entry (owner default, 2-Oct-2026)
def test_a_straddle_counts_as_one_entry_toward_the_spec_cap(
    limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    g1 = RiskGovernor(limits, market_clock, costs, plan_id, strategy_entry_caps={SID: 1})
    first = g1.evaluate(leg(CE, OptionRight.CE), day("100000").state, market(cash="100000", q=quote(key=CE)))
    assert first.verdict is Verdict.APPROVE and first.counts_as_entry
    s = holding().state  # the CE leg: one entry today, the spec cap of 1 reached
    assert s.entries_by_strategy[SID] == 1
    second = g1.evaluate(leg(PE), s, pe_market())
    assert second.verdict is Verdict.APPROVE, second.reasons
    assert not second.counts_as_entry  # the completing leg is the same entry
    # a new single-leg entry after the straddle is over the spec cap
    new = g1.evaluate(intent(strategy_id=SID), s, market(cash="100000"))
    assert Reason.STRATEGY_MAX_ENTRIES in new.reasons


def test_the_completing_leg_is_exempt_from_the_system_cap_but_nothing_else_is(g: RiskGovernor) -> None:
    approve_ce(g)
    e = holding()
    for i in range(9):  # nine other round trips: with the CE leg that is 10 entries today
        cid = f"O{i}"
        e.add("ORDER_SUBMITTED", client_order_id=cid, strategy_id="S-ORB-001", instrument_key=f"K{i}", side="BUY",
              qty=65, price=D("3.00"), kind="ENTRY", lot_size=65)  # fmt: skip
        e.add("ORDER_TERMINAL", client_order_id=cid, status="CANCELLED")
    assert e.state.entries_today == 10
    d = g.evaluate(leg(PE), e.state, pe_market())
    assert d.verdict is Verdict.APPROVE and not d.counts_as_entry, d.reasons
    other = g.evaluate(intent(strategy_id="S-GAPGO-001"), e.state, market(cash="100000"))
    assert Reason.MAX_TRADES_PER_DAY in other.reasons
    # a second leg that is not a straddle gets no exemption: it is refused and would count
    ce2 = g.evaluate(leg("NSE_FO|NIFTY06OCT2625050CE", OptionRight.CE), e.state,
                     market(cash="100000", q=quote(key="NSE_FO|NIFTY06OCT2625050CE")))  # fmt: skip
    assert ce2.verdict is Verdict.REJECT and Reason.NOT_A_STRADDLE in ce2.reasons
    assert Reason.MAX_TRADES_PER_DAY in ce2.reasons and ce2.counts_as_entry


def test_the_state_does_not_count_a_straddles_second_leg() -> None:
    e = holding(protected=False)
    e.add("ORDER_SUBMITTED", client_order_id="E2", strategy_id=SID, instrument_key=PE, side="BUY", qty=65,
          price=D("3.00"), kind="ENTRY", lot_size=65, counts_as_entry=False)  # fmt: skip
    assert e.state.entries_today == 1 and e.state.entries_by_strategy[SID] == 1
    assert len(e.state.open_orders) == 1  # still a working order like any other
    from project100c.errors import KernelInvariantError

    with pytest.raises(KernelInvariantError):
        e.add("ORDER_SUBMITTED", client_order_id="E3", strategy_id=SID, instrument_key="K3", side="BUY", qty=65,
              price=D("3.00"), kind="ENTRY", lot_size=65, counts_as_entry="no")  # fmt: skip

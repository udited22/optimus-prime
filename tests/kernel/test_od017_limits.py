"""OD-017 (2-Oct-2026): the delegated risk limits -- abnormal market from the open, broker errors (consecutive and
windowed), and the slippage kill -- in the limits file, the health kills, the Governor and the runtime."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from project100c.broker import FakeBroker
from project100c.calendar import MarketClock
from project100c.costs import CostModel
from project100c.errors import KernelInvariantError
from project100c.journal import Journal
from project100c.kernel.governor import (
    ActionKind,
    Reason,
    RiskGovernor,
    abnormal_market_notes,
    modelled_slippage,
    slippage_breach,
)
from project100c.kernel.health import HealthObservation, detect_kills, market_moves
from project100c.kernel.kills import KillSwitch
from project100c.kernel.limits import RiskLimits, load_risk_limits
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.kernel.state import SLIPPAGE_KEEP

from .conftest import DAY, KEY, Clock, day, intent, ist, market, quote


def test_shipped_limits_carry_od017(limits: RiskLimits) -> None:
    assert limits.version == "RL-2026-10-03.2" and "OD-017" in limits.owner_decisions
    assert limits.pending_signoff == ()
    assert limits.abnormal_index_from_open_frac == D("0.02")
    assert limits.abnormal_vix_jump_frac == D("0.20")
    assert (limits.abnormal_index_move_frac, limits.abnormal_index_move_window_min) == (D("0.01"), 5)
    assert limits.abnormal_cooloff_min == 30
    assert limits.broker_consecutive_error_threshold == 3
    assert (limits.broker_error_threshold, limits.broker_error_window_s) == (5, D("900"))
    assert limits.slippage_window_fills == 5
    assert (limits.strategy_slippage_multiple, limits.slippage_single_fill_multiple) == (D("2"), D("3"))
    # the hard rules are unchanged
    assert limits.per_trade_max_loss_frac == D("0.02") and limits.dd_suspend_frac == D("0.125")
    assert limits.max_lots == 1 and limits.max_trades_per_day == 10
    assert limits.strategy_max_lots == {
        "S-VIXSTR-001": 2,
        "S-IVRV-001": 2,
        "S-DAYVOL-001": 2,
        "S-VOLCHEAP-001": 2,
        "S-VOLHOLD-001": 2,
        "S-EXPVOL-001": 2,
    }  # OD-013 straddles


def test_older_versions_load_without_the_new_rules(configs_dir: Path) -> None:
    old = load_risk_limits(configs_dir / "risk" / "limits.toml", "RL-2026-10-02.2")
    assert old.abnormal_index_from_open_frac is None and old.broker_consecutive_error_threshold is None
    assert old.slippage_window_fills is None and old.slippage_single_fill_multiple is None
    assert slippage_breach([(D(100), D(1))] * 5, old) == ""


@pytest.mark.parametrize(
    "over",
    [
        {"abnormal_index_from_open_frac": "1.5"},
        {"abnormal_index_from_open_frac": 0.02},  # float forbidden
        {"slippage_window_fills": None},  # the two slippage fields go together
        {"slippage_single_fill_multiple": "1.5"},  # below the window multiple
        {"strategy_slippage_multiple": "1"},  # must exceed 1x
        {"broker_consecutive_error_threshold": 0},
    ],
)
def test_od017_validation(limits: RiskLimits, over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RiskLimits.model_validate(limits.model_dump() | over)


# ---------------------------------------------------------------- abnormal market


def test_market_moves() -> None:
    assert market_moves(D(25000), D(25500), D(14), D("16.8")) == (D("0.02"), D("0.2"))
    assert market_moves(D(25000), D(24400), D(14), D(12))[0] == D("0.024")  # either direction
    assert market_moves(D(25000), D(24400), D(14), D(12))[1] == 0  # a VIX fall is not a jump
    assert market_moves(None, D(25000), D(0), D(14)) == (D(0), D(0))  # unmeasurable -> 0 (DQ kill covers a dead feed)


@pytest.mark.parametrize(
    ("obs", "kills"),
    [
        (HealthObservation(index_move_from_open_frac=D("0.0201")), True),
        (HealthObservation(index_move_from_open_frac=D("0.02")), False),
        (HealthObservation(vix_jump_frac=D("0.21")), True),
        (HealthObservation(vix_jump_frac=D("0.19")), False),
        (HealthObservation(index_move_frac_in_window=D("0.011")), True),
        (HealthObservation(market_halt_or_circuit=True), True),
        (HealthObservation(), False),
    ],
)
def test_abnormal_market_kill(limits: RiskLimits, obs: HealthObservation, kills: bool) -> None:
    got = [sw for sw, _ in detect_kills(obs, limits)]
    assert (KillSwitch.ABNORMAL_MARKET in got) is kills


def test_governor_refuses_entries_in_an_abnormal_market(gov: RiskGovernor) -> None:
    s = day().state
    bad: list[dict[str, Any]] = [
        {"index_move_from_open_frac": D("0.025")},
        {"vix_jump_frac": D("0.3")},
        {"market_halt_or_circuit": True},
    ]
    for kw in bad:
        d = gov.evaluate(intent(), s, market(**kw))
        assert not d.approved and Reason.ABNORMAL_MARKET in d.reasons
    assert gov.evaluate(intent(), s, market(index_move_from_open_frac=D("0.015"), vix_jump_frac=D("0.1"))).approved
    assert abnormal_market_notes(market(), gov.limits) == []


# ---------------------------------------------------------------- broker errors


@pytest.mark.parametrize(("consecutive", "in_window", "kills"), [(3, 3, True), (2, 4, False), (0, 5, True)])
def test_broker_error_kill(limits: RiskLimits, consecutive: int, in_window: int, kills: bool) -> None:
    obs = HealthObservation(broker_consecutive_errors=consecutive, broker_errors_in_window=in_window)
    assert (KillSwitch.BROKER_CONNECTIVITY in [sw for sw, _ in detect_kills(obs, limits)]) is kills


def _rt(tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> tuple[KernelRuntime, FakeBroker, Clock]:
    c = Clock(ist(9, 16))
    b = FakeBroker(c, funds=D("10000"))
    b.set_quote(KEY, D("2.95"), D("3.00"))
    rt = KernelRuntime(Journal(tmp_path / "j.db", clock=c), b, gov, costs, plan_id, c, MemoryAlerts())
    rt.start_day(DAY, D("10000"), week_start=True)
    c.t = ist(10, 0)
    return rt, b, c


def test_three_consecutive_order_errors_latch_but_a_success_resets(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    rt, b, c = _rt(tmp_path, gov, costs, plan_id)
    b.faults.reject_next_places.extend(["EXCHANGE: price band"] * 2)
    for i in range(2):
        rt.submit(intent(intent_id=f"R{i}", decided_at=c.t), market(c.t, q=quote(ts=c.t)))
    rt.step()
    assert (KillSwitch.BROKER_CONNECTIVITY, "") not in rt.state.kills  # 2 in a row: not yet
    b.set_quote(KEY, D("2.95"), D("3.10"))  # the next entry rests unfilled at the broker
    assert rt.submit(intent(intent_id="OK", decided_at=c.t), market(c.t, q=quote(ts=c.t))).approved  # success resets
    assert rt.cancel_entries(KEY) == 1
    c.adv(901)  # outside the 15-minute window: only the consecutive rule can fire now
    rt.step()
    b.faults.reject_next_places.extend(["EXCHANGE: price band"] * 3)
    for i in range(3):
        rt.step()
        assert (KillSwitch.BROKER_CONNECTIVITY, "") not in rt.state.kills
        rt.submit(intent(intent_id=f"X{i}", decided_at=c.t), market(c.t, q=quote(ts=c.t)))
        c.adv(1)
    rt.step()
    latch = rt.state.kills[(KillSwitch.BROKER_CONNECTIVITY, "")]
    assert "3 consecutive" in latch.reason


# ---------------------------------------------------------------- slippage


def test_slippage_rules(limits: RiskLimits) -> None:
    m = D("0.10")
    assert slippage_breach([(D("0.31"), m)], limits).startswith("single fill")  # > 3x on one fill
    assert slippage_breach([(D("0.30"), m)], limits) == ""  # exactly 3x is allowed
    assert slippage_breach([(D("0.25"), m)] * 4, limits) == ""  # 2.5x but fewer than 5 fills
    assert "last 5 fills" in slippage_breach([(D("0.25"), m)] * 5, limits)
    assert slippage_breach([(D("0.20"), m)] * 5, limits) == ""  # exactly 2x is allowed
    # only the latest 5 count: old bad fills roll off
    assert slippage_breach([(D("0.29"), m)] * 5 + [(D("0.05"), m)] * 5, limits) == ""


def test_slippage_observed_reducer_and_strategy_kill(gov: RiskGovernor) -> None:
    e = day()
    for i in range(SLIPPAGE_KEEP + 3):
        e.add("SLIPPAGE_OBSERVED", strategy_id="S1", client_order_id=f"c{i}", realised=D("0.01"), modelled=D("0.1"))
    assert len(e.state.slippage_fills["S1"]) == SLIPPAGE_KEEP
    assert not [a for a in gov.required_actions(e.state, ist(10, 0)) if a.scope == "S1"]
    for i in range(5):
        e.add("SLIPPAGE_OBSERVED", strategy_id="S1", client_order_id=f"b{i}", realised=D("0.25"), modelled=D("0.1"))
    (a,) = [a for a in gov.required_actions(e.state, ist(10, 0)) if a.scope == "S1"]
    assert a.kind is ActionKind.LATCH_KILL and a.kill is KillSwitch.STRATEGY
    e.add("KILL_LATCHED", switch=str(KillSwitch.STRATEGY), scope="S1", reason=a.reason)
    e.add("KILL_RESET", switch=str(KillSwitch.STRATEGY), scope="S1", actor="OWNER")
    assert "S1" not in e.state.slippage_fills  # reviewed: the window restarts
    with pytest.raises(KernelInvariantError):
        e.add("SLIPPAGE_OBSERVED", strategy_id="S1", client_order_id="z", realised=D("-1"), modelled=D("0.1"))
    with pytest.raises(KernelInvariantError):
        e.add("SLIPPAGE_OBSERVED", strategy_id="S1", client_order_id="z", realised=D("0"), modelled=D("0"))


def test_modelled_slippage_floor(limits: RiskLimits) -> None:
    assert modelled_slippage(market(), D("0.05"), limits) == D("0.10")  # 2 ticks
    assert modelled_slippage(market(measured_slippage_per_side=D("0.4")), D("0.05"), limits) == D("0.4")


def test_runtime_measures_fills_and_a_bad_fill_kills_the_strategy(
    tmp_path: Path, limits: RiskLimits, market_clock: MarketClock, costs: CostModel, plan_id: str
) -> None:
    # a measured p90 of 0.02 per unit, so a fill 0.075 above the decision mid is 3.75x modelled
    g = RiskGovernor(limits.model_copy(update={"min_slippage_ticks_per_side": 0}), market_clock, costs, plan_id)
    rt, b, c = _rt(tmp_path, g, costs, plan_id)
    b.set_quote(KEY, D("2.95"), D("3.05"))  # the book moved between decision and fill
    d = rt.submit(
        intent(decided_at=c.t, limit_price=D("3.05")),
        market(c.t, q=quote(ts=c.t), measured_slippage_per_side=D("0.02")),
    )
    assert d.approved
    rt.step({KEY: quote("2.95", "3.05", ts=c.t)})
    (obs,) = rt.state.slippage_fills["S-ORB-001"]
    assert obs == (D("0.075"), D("0.02"))  # 3.05 - mid 2.975 of the decision quote
    assert rt.state.strategy_killed("S-ORB-001")
    assert "single fill" in rt.state.kills[(KillSwitch.STRATEGY, "S-ORB-001")].reason


def test_runtime_measures_entries_not_protective_stops(
    tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str
) -> None:
    rt, b, c = _rt(tmp_path, gov, costs, plan_id)
    assert rt.submit(intent(decided_at=c.t), market(c.t, q=quote(ts=c.t))).approved
    rt.step({KEY: quote(ts=c.t)})
    c.adv(1)
    rt.step({KEY: quote(ts=c.t)})
    c.adv(60)
    b.set_quote(KEY, D("2.25"), D("2.30"))
    rt.step({KEY: quote("2.25", "2.30", ts=c.t)})
    assert rt.state.positions == {}  # stopped out
    (entry,) = rt.state.slippage_fills["S-ORB-001"]  # the stop fill (2.25 vs trigger 2.30) is within the budget
    assert entry == (D("0.025"), D("0.10"))  # filled at the 3.00 ask vs the 2.975 mid
    assert not rt.state.strategy_killed("S-ORB-001")

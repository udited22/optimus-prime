"""K-02 Risk Governor: every check, its reason code, and the protective/limit arithmetic."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from project100c.core_types import OrderSide
from project100c.costs import ChargeBreakdown
from project100c.errors import GovernorError
from project100c.instruments import InstrumentKind
from project100c.kernel.governor import ActionKind, Reason, RequiredAction, RiskGovernor, Verdict
from project100c.kernel.kills import HaltKind, KillSwitch
from project100c.kernel.limits import RiskLimits
from project100c.kernel.state import OrderKind
from project100c.spec.models import Lifecycle

from .conftest import DAY, KEY, Events, contract, day, intent, ist, market, quote


def test_happy_path_approves_with_ticket(gov: RiskGovernor) -> None:
    d = gov.evaluate(intent(), day().state, market())
    assert d.verdict is Verdict.APPROVE, d.notes
    t = d.ticket
    assert t is not None and not t.simulate_only and not t.is_exit
    assert t.expires_at - t.approved_at == timedelta(seconds=2)
    assert t.limits_version == "RL-2026-10-03.2" and t.window_version == "TW-2026-10-01.2"
    # risk = 0.75*65 + charges(~47.57) + 2 * 2 ticks * 65 = 48.75 + 47.57 + 13 ~= 109.3 <= 200
    assert d.risk_at_stop is not None and D("100") < d.risk_at_stop < D("120")
    assert d.budget == D("200.00")
    assert t.valid_at(ist(10, 0, 1)) and not t.valid_at(ist(10, 0, 2))


def test_mandate_first_and_all_reasons_reported(gov: RiskGovernor) -> None:
    d = gov.evaluate(intent(side=OrderSide.SELL, exit_kind=OrderKind.EXIT), day().state, market(ist(15, 5)))
    assert d.reasons[0] is Reason.MANDATE_LONG_ONLY
    assert Reason.OUTSIDE_TRADING_WINDOW in d.reasons


def test_smallest_lot_exceeds_budget(gov: RiskGovernor) -> None:
    # premium 20, stop 15: 5*65 = 325 alone > 200 budget at 10k NAV
    i = intent(limit_price=D("20.00"), stop_trigger=D("15.05"), stop_limit=D("15.00"), spec_stop_limit=D("15.00"))
    d = gov.evaluate(i, day().state, market(q=quote("19.95", "20.00")))
    assert d.reasons == (Reason.SMALLEST_LOT_EXCEEDS_BUDGET,)
    assert d.risk_at_stop is not None and d.risk_at_stop > d.budget  # type: ignore[operator]


def test_risk_budget_exceeded_for_multi_lot_when_cap_allows(limits: RiskLimits, gov: RiskGovernor) -> None:
    lim2 = limits.model_copy(update={"max_lots": 2})  # model_copy skips validation: test-only
    g2 = RiskGovernor(lim2, gov.clock, gov._costs, gov._plan)
    d = g2.evaluate(
        intent(qty=130, limit_price=D("4.00"), stop_trigger=D("3.05"), stop_limit=D("3.00"), spec_stop_limit=D("3.00")),
        day().state,
        market(q=quote("3.95", "4.00")),
    )
    assert Reason.RISK_BUDGET_EXCEEDED in d.reasons and Reason.SMALLEST_LOT_EXCEEDS_BUDGET not in d.reasons


@pytest.mark.parametrize(
    ("hh", "mm", "reason"),
    [
        (9, 14, Reason.OUTSIDE_TRADING_WINDOW),
        (9, 17, Reason.ENTRY_WINDOW_CLOSED),  # before entry_start 09:20 (OD-009)
        (14, 0, Reason.ENTRY_WINDOW_CLOSED),  # OD-008 cutoff
        (14, 30, Reason.ENTRY_WINDOW_CLOSED),
        (14, 50, Reason.ENTRY_WINDOW_CLOSED),
        (15, 0, Reason.OUTSIDE_TRADING_WINDOW),
    ],
)
def test_window(gov: RiskGovernor, hh: int, mm: int, reason: Reason) -> None:
    now = ist(hh, mm)
    d = gov.evaluate(intent(decided_at=now), day().state, market(now))
    assert reason in d.reasons


def test_1359_is_last_entry_minute(gov: RiskGovernor) -> None:
    now = ist(13, 59, 59)
    assert gov.evaluate(intent(decided_at=now), day().state, market(now)).approved


def test_holiday_is_closed(gov: RiskGovernor) -> None:
    from datetime import date, datetime

    from project100c.sessions import IST

    now = datetime(2026, 10, 2, 10, 0, tzinfo=IST)  # Gandhi Jayanti
    assert Reason.OUTSIDE_TRADING_WINDOW in gov.evaluate(intent(), day().state, market(now)).reasons
    assert date(2026, 10, 2) != DAY


def test_nav_unknown_without_day_start(gov: RiskGovernor) -> None:
    from project100c.kernel.state import KernelState

    assert Reason.NAV_UNKNOWN in gov.evaluate(intent(), KernelState(), market()).reasons


def test_kills_and_halts_block_entries(gov: RiskGovernor) -> None:
    for sw in KillSwitch:
        scope = "S-ORB-001" if sw is KillSwitch.STRATEGY else ""
        s = day().add("KILL_LATCHED", switch=str(sw), scope=scope, reason="t").state
        d = gov.evaluate(intent(), s, market())
        assert (Reason.STRATEGY_KILLED if scope else Reason.KILL_ACTIVE) in d.reasons, sw
    for hk in HaltKind:
        s = day().add("HALT_LATCHED", kind=str(hk), reason="t").state
        assert Reason.HALT_ACTIVE in gov.evaluate(intent(), s, market()).reasons
    other = day().add("KILL_LATCHED", switch="STRATEGY_KILL", scope="OTHER", reason="t").state
    assert gov.evaluate(intent(), other, market()).approved  # a strategy kill is scoped


@pytest.mark.parametrize(
    ("status", "ok", "sim"),
    [
        (Lifecycle.CANARY, True, False),
        (Lifecycle.PRODUCTION, True, False),
        (Lifecycle.SHADOW, True, True),
        (Lifecycle.PAPER, False, False),
        (Lifecycle.DEGRADED, False, False),
        (Lifecycle.RESEARCH, False, False),
    ],
)
def test_strategy_status(gov: RiskGovernor, status: Lifecycle, ok: bool, sim: bool) -> None:
    d = gov.evaluate(intent(strategy_status=status), day().state, market())
    assert d.approved is ok
    if ok:
        assert d.ticket is not None and d.ticket.simulate_only is sim
    else:
        assert Reason.STRATEGY_STATUS in d.reasons


def _long() -> Events:
    e = day()
    e.add(
        "ORDER_SUBMITTED",
        client_order_id="E1",
        strategy_id="S-ORB-001",
        instrument_key=KEY,
        side="BUY",
        qty=65,
        price=D("3.00"),
        kind="ENTRY",
        lot_size=65,
    )
    e.add("FILL", client_order_id="E1", trade_id="T1", qty=65, price=D("3.00"), charges=D("5"))
    return e


def test_one_lot_max_and_no_averaging(gov: RiskGovernor) -> None:
    s = _long().state
    d = gov.evaluate(intent(intent_id="I2"), s, market())
    assert Reason.MAX_POSITION in d.reasons and Reason.NO_AVERAGING in d.reasons
    # a different strike still breaches the 1-lot cap and the per-strategy no-pyramiding rule
    d2 = gov.evaluate(intent(contract=contract("NSE_FO|OTHER")), s, market(q=quote(key="NSE_FO|OTHER")))
    assert Reason.MAX_POSITION in d2.reasons and Reason.NO_AVERAGING in d2.reasons


def test_pending_entry_counts_toward_cap_and_open_orders(gov: RiskGovernor) -> None:
    s = (
        day()
        .add(
            "ORDER_SUBMITTED",
            client_order_id="E1",
            strategy_id="X",
            instrument_key="NSE_FO|OTHER",
            side="BUY",
            qty=65,
            price=D("3.00"),
            kind="ENTRY",
            lot_size=65,
        )
        .state
    )
    d = gov.evaluate(intent(), s, market())
    assert Reason.MAX_POSITION in d.reasons and Reason.OPEN_ORDER_LIMIT in d.reasons


def test_two_lots_rejected(gov: RiskGovernor) -> None:
    assert Reason.MAX_POSITION in gov.evaluate(intent(qty=130), day().state, market()).reasons


def test_invalid_quantity_not_lot_multiple(gov: RiskGovernor) -> None:
    assert Reason.INVALID_QUANTITY in gov.evaluate(intent(qty=64), day().state, market()).reasons


def _closed(ev: Events, pnl_price: str, kind: str, n: int, ts_min: int = 30) -> None:
    for i in range(n):
        cid, xid = f"E{i}{kind}", f"X{i}{kind}"
        ev.add(
            "ORDER_SUBMITTED",
            ist(9, ts_min + i),
            client_order_id=cid,
            strategy_id="S-ORB-001",
            instrument_key=KEY,
            side="BUY",
            qty=65,
            price=D("3.00"),
            kind="ENTRY",
            lot_size=65,
        )
        ev.add(
            "FILL",
            ist(9, ts_min + i),
            client_order_id=cid,
            trade_id=f"T{cid}",
            qty=65,
            price=D("3.00"),
            charges=D("5"),
        )
        ev.add(
            "ORDER_SUBMITTED",
            ist(9, ts_min + i),
            client_order_id=xid,
            strategy_id="S-ORB-001",
            instrument_key=KEY,
            side="SELL",
            qty=65,
            price=D(pnl_price),
            kind=kind,
            lot_size=65,
        )
        ev.add(
            "FILL",
            ist(9, ts_min + i),
            client_order_id=xid,
            trade_id=f"T{xid}",
            qty=65,
            price=D(pnl_price),
            charges=D("5"),
        )


def test_trades_per_day_and_cooldown(gov: RiskGovernor) -> None:
    e = day()
    _closed(e, "3.50", "EXIT", 10)  # OD-014: the system-wide cap is 10
    assert Reason.MAX_TRADES_PER_DAY in gov.evaluate(intent(), e.state, market()).reasons
    e2 = day()
    _closed(e2, "2.25", "PROTECTIVE", 1, ts_min=50)  # stopped out 09:50
    d = gov.evaluate(intent(decided_at=ist(10, 0)), e2.state, market(ist(10, 0)))
    assert Reason.COOLDOWN in d.reasons
    later = ist(10, 5, 1)
    assert Reason.COOLDOWN not in gov.evaluate(intent(), e2.state, market(later, q=quote(ts=later))).reasons
    assert e2.state.consecutive_stops["S-ORB-001"] == 1


def test_martingale(limits: RiskLimits, gov: RiskGovernor) -> None:
    lim2 = limits.model_copy(update={"max_lots": 2, "max_trades_per_day": 10, "per_trade_max_loss_frac": D("0.05")})
    g2 = RiskGovernor(lim2, gov.clock, gov._costs, gov._plan)
    e = day("100000")
    _closed(e, "2.50", "EXIT", 1)  # a losing 1-lot trade
    d = g2.evaluate(intent(qty=130), e.state, market(cash="100000"))
    assert Reason.MARTINGALE in d.reasons


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        (dict(stop_trigger=None), Reason.NO_PROTECTIVE_EXIT),
        (dict(stop_limit=None), Reason.NO_PROTECTIVE_EXIT),
        (dict(spec_stop_limit=None), Reason.NO_PROTECTIVE_EXIT),
        (dict(stop_trigger=D("3.10")), Reason.STOP_INVALID),  # trigger above entry
        (dict(stop_limit=D("2.40")), Reason.STOP_INVALID),  # limit above trigger
        (dict(stop_limit=D("2.20"), spec_stop_limit=D("2.25")), Reason.STOP_WIDENED),
        (dict(stop_trigger=D("2.32")), Reason.TICK_SIZE),
        (dict(limit_price=D("2.99")), Reason.TICK_SIZE),
    ],
)
def test_protective_exit_rules(gov: RiskGovernor, kw: dict[str, object], reason: Reason) -> None:
    assert reason in gov.evaluate(intent(**kw), day().state, market()).reasons


@pytest.mark.parametrize(
    ("q", "reason"),
    [
        (quote("2.50", "3.00"), Reason.LOW_LIQUIDITY),  # spread 0.50 > max(0.10, 3% of 2.75)
        (quote("2.95", "3.00", qty=100), Reason.LOW_LIQUIDITY),  # < 2 lots on top of book
        (quote(ts=ist(9, 59, 56)), Reason.STALE_QUOTE),
        (quote("3.20", "3.25"), Reason.PRICE_SANITY),  # limit 3.00 below bid - 5 ticks? no: 3.20-0.25=2.95 <=3 ok
    ],
)
def test_market_checks(gov: RiskGovernor, q: object, reason: Reason) -> None:
    from project100c.market_types import Quote

    assert isinstance(q, Quote)
    d = gov.evaluate(intent(), day().state, market(q=q))
    if reason is Reason.PRICE_SANITY:
        assert d.approved  # 3.00 is within [3.20 - 5 ticks, 3.25 + 2 ticks]
        d = gov.evaluate(
            intent(limit_price=D("3.40"), stop_trigger=D("2.70"), stop_limit=D("2.65"), spec_stop_limit=D("2.65")),
            day().state,
            market(q=q),
        )
    assert reason in d.reasons, d.reasons


def test_no_quote_band_min_oi_and_wrong_instrument(limits: RiskLimits, gov: RiskGovernor) -> None:
    from project100c.market_types import Quote

    t = ist(10, 0)
    one_sided = Quote(KEY, t, t, None, D("3.00"), None, 1000, None, None)
    assert Reason.NO_QUOTE in gov.evaluate(intent(), day().state, market(q=one_sided)).reasons
    assert Reason.PRICE_SANITY in gov.evaluate(intent(), day().state, market(price_band=(D("3.05"), D("9")))).reasons
    g2 = RiskGovernor(limits.model_copy(update={"min_oi": 10**9}), gov.clock, gov._costs, gov._plan)
    assert Reason.LOW_LIQUIDITY in g2.evaluate(intent(), day().state, market()).reasons
    with pytest.raises(GovernorError):
        gov.evaluate(intent(), day().state, market(q=quote(key="NSE_FO|OTHER")))
    with pytest.raises(GovernorError, match="aware"):
        from datetime import datetime

        gov.evaluate(intent(), day().state, market(datetime(2026, 10, 5, 10), q=quote()))


def test_buying_power_and_headroom(gov: RiskGovernor) -> None:
    assert Reason.INSUFFICIENT_BUYING_POWER in gov.evaluate(intent(), day().state, market(cash="600")).reasons
    s = day().add("MARK", unrealised=D("-300")).state  # 3% down today; +~110 risk > 4%
    assert Reason.DAILY_HEADROOM in gov.evaluate(intent(), s, market()).reasons
    w = day("10000", sow="10800").state  # week already 7.4% down; +~110 > 8% of 10800
    assert Reason.WEEKLY_HEADROOM in gov.evaluate(intent(), w, market()).reasons


def test_drawdown_headroom(gov: RiskGovernor) -> None:
    e = day("10000").add("MARK", unrealised=D("0"))
    e.state = replace(e.state, hwm=D("11350"))  # already 11.9% below HWM
    assert Reason.DRAWDOWN_HEADROOM in gov.evaluate(intent(), e.state, market()).reasons


def test_event_day_and_expired_instrument(gov: RiskGovernor) -> None:
    assert Reason.EVENT_DAY_NOT_CERTIFIED in gov.evaluate(intent(), day().state, market(event_day=True)).reasons
    assert gov.evaluate(intent(event_certified=True), day().state, market(event_day=True)).approved
    from datetime import date

    old = replace(contract(), expiry=date(2026, 9, 29))
    assert Reason.INSTRUMENT_NOT_TRADEABLE in gov.evaluate(intent(contract=old), day().state, market()).reasons
    fut = replace(contract(), kind=InstrumentKind.FUTURE, right=None, strike=None)
    assert Reason.MANDATE_INSTRUMENT_TYPE in gov.evaluate(intent(contract=fut), day().state, market()).reasons


def test_cost_model_unavailable_is_a_reject_not_a_crash(gov: RiskGovernor) -> None:
    from project100c.costs import CostModel
    from project100c.errors import NoScheduleForDateError

    class NoSchedule(CostModel):
        def round_trip(self, *a: object, **k: object) -> ChargeBreakdown:
            raise NoScheduleForDateError("no VERIFIED charge schedule for this date")

    base = gov._costs
    broken = NoSchedule(base.book, {gov._plan: base.plan(gov._plan)})
    g = RiskGovernor(gov.limits, gov.clock, broken, gov._plan)
    d = g.evaluate(intent(), day().state, market())
    assert d.reasons == (Reason.COST_MODEL_UNAVAILABLE,)
    assert any("VERIFIED" in n for n in d.notes)


def test_evaluate_outside_session_coverage_raises_typed_error(gov: RiskGovernor) -> None:
    from datetime import datetime

    from project100c.errors import Project100CError
    from project100c.sessions import IST

    now = datetime(2020, 6, 3, 10, 0, tzinfo=IST)  # before calendar + session coverage (2021)
    with pytest.raises(Project100CError):
        gov.evaluate(intent(decided_at=now), day().state, market(now))


# ---- exits (sell-to-close) ----
def test_exit_allowed_under_kills_but_not_outside_window_or_exit_all_halt(gov: RiskGovernor) -> None:
    e = _long()
    e.add("KILL_LATCHED", switch="MANUAL_MASTER_KILL", scope="", reason="owner")
    ex = intent(
        side=OrderSide.SELL, exit_kind=OrderKind.EXIT, limit_price=D("2.90"), stop_trigger=None, stop_limit=None
    )
    s = e.state
    d = gov.evaluate(ex, s, market(ist(14, 55)))
    assert d.approved and d.ticket is not None and d.ticket.is_exit
    assert Reason.OUTSIDE_TRADING_WINDOW in gov.evaluate(ex, s, market(ist(15, 0))).reasons
    e.add("HALT_LATCHED", kind="EXIT_ALL_FAILED", reason="t")
    assert Reason.ALL_TRADING_HALTED in gov.evaluate(ex, e.state, market(ist(14, 55))).reasons
    too_many = replace(ex, qty=130)
    assert Reason.MANDATE_LONG_ONLY in gov.evaluate(too_many, s, market(ist(14, 55))).reasons


def test_exit_price_and_stop_sanity(gov: RiskGovernor) -> None:
    s = _long().state
    stuck = intent(side=OrderSide.SELL, exit_kind=OrderKind.EXIT, limit_price=D("5.00"))
    assert Reason.PRICE_SANITY in gov.evaluate(stuck, s, market()).reasons
    bad_sl = intent(side=OrderSide.SELL, exit_kind=OrderKind.PROTECTIVE, limit_price=D("2.25"), stop_trigger=D("2.20"))
    assert Reason.STOP_INVALID in gov.evaluate(bad_sl, s, market()).reasons
    off_tick = intent(
        side=OrderSide.SELL, exit_kind=OrderKind.PROTECTIVE, limit_price=D("2.25"), stop_trigger=D("2.31")
    )
    assert Reason.TICK_SIZE in gov.evaluate(off_tick, s, market()).reasons
    ok = intent(side=OrderSide.SELL, exit_kind=OrderKind.PROTECTIVE, limit_price=D("2.25"), stop_trigger=D("2.30"))
    assert gov.evaluate(ok, s, market()).approved
    assert Reason.TICK_SIZE in gov.evaluate(replace(ok, limit_price=D("2.26")), s, market()).reasons


# ---- required actions ----
def _kinds(acts: Sequence[RequiredAction]) -> list[ActionKind]:
    return [a.kind for a in acts]


def test_actions_flatten_phase_and_hard_flat_exit_all(gov: RiskGovernor) -> None:
    e = _long()
    e.add("PROTECTIVE_CONFIRMED", instrument_key=KEY, client_order_id="SL")
    s = e.state
    assert gov.required_actions(s, ist(14, 49, 59)) == []
    assert ActionKind.FLATTEN in _kinds(gov.required_actions(s, ist(14, 50)))
    acts = gov.required_actions(s, ist(15, 0))
    assert _kinds(acts) == [ActionKind.EXIT_ALL, ActionKind.ALERT_URGENT]  # OD-007
    assert gov.required_actions(day().state, ist(15, 0)) == []  # flat: nothing to do


def test_actions_unprotected_position_flattened_after_3s(gov: RiskGovernor) -> None:
    s = _long().state  # opened 09:16:00, no protective
    assert gov.required_actions(s, ist(9, 16, 3)) == []
    acts = gov.required_actions(s, ist(9, 16, 4))
    assert ActionKind.FLATTEN in _kinds(acts) and ActionKind.ALERT_URGENT in _kinds(acts)


def test_actions_loss_limits_latch(gov: RiskGovernor) -> None:
    s = day().add("MARK", unrealised=D("-400")).state
    acts = gov.required_actions(s, ist(10, 0))
    assert any(a.kind is ActionKind.LATCH_KILL and a.kill is KillSwitch.DAILY_LOSS for a in acts)
    w = day("10000", sow="10900").state
    assert any(a.halt is HaltKind.WEEKLY_FREEZE for a in gov.required_actions(w, ist(10, 0)))
    e = day()
    e.state = replace(e.state, hwm=D("11500"))  # 13.0% DD >= 12.5% suspend
    assert any(a.halt is HaltKind.DD_SUSPENSION for a in gov.required_actions(e.state, ist(10, 0)))
    e.state = replace(e.state, hwm=D("11200"))  # 10.7%: warning only
    assert _kinds(gov.required_actions(e.state, ist(10, 0))) == [ActionKind.ALERT]


def test_actions_strategy_kill_triggers(gov: RiskGovernor) -> None:
    e = day()
    _closed(e, "2.25", "PROTECTIVE", 3)
    acts = gov.required_actions(e.state, ist(11, 0))
    assert any(a.kill is KillSwitch.STRATEGY and a.scope == "S-ORB-001" for a in acts)
    e2 = day()
    for _ in range(3):
        e2.add("SLIPPAGE_BREACH", strategy_id="S2")
        e2.add("INTENT_REJECTED", strategy_id="S3", invalid=True)
    scopes = {a.scope for a in gov.required_actions(e2.state, ist(11, 0)) if a.kill is KillSwitch.STRATEGY}
    assert scopes == {"S2", "S3"}


def _fork(base: Events) -> Events:
    e = Events()
    e.state, e.seq = base.state, base.seq
    return e


def test_actions_under_kills(gov: RiskGovernor) -> None:
    base = _long()
    base.add("PROTECTIVE_CONFIRMED", instrument_key=KEY, client_order_id="SL")
    for sw, expect in [
        (KillSwitch.MANUAL_MASTER, {ActionKind.CANCEL_ALL, ActionKind.FLATTEN}),
        (KillSwitch.DATA_QUALITY, {ActionKind.VERIFY_PROTECTIVE}),
        (KillSwitch.SYSTEM_INTEGRITY, {ActionKind.FLATTEN}),
    ]:
        s = base.state.kills
        assert not s
        e = _fork(base)
        e.add("KILL_LATCHED", switch=str(sw), scope="", reason="t")
        got = set(_kinds(gov.required_actions(e.state, ist(11, 0))))
        assert expect <= got, (sw, got)
    e = _fork(base)
    e.add("KILL_LATCHED", switch="STRATEGY_KILL", scope="S-ORB-001", reason="t")
    acts = gov.required_actions(e.state, ist(11, 0))
    assert any(a.kind is ActionKind.FLATTEN and a.instrument_key == KEY for a in acts)


def test_exit_all_failed_halt_only_alerts(gov: RiskGovernor) -> None:
    e = _long()
    e.add("HALT_LATCHED", kind="EXIT_ALL_FAILED", reason="t")
    assert _kinds(gov.required_actions(e.state, ist(15, 5))) == [ActionKind.ALERT_URGENT]


def test_residual_policy_fallbacks(gov: RiskGovernor, configs_dir: object) -> None:
    from pathlib import Path

    from project100c.calendar import MarketClock
    from project100c.sessions import SessionCalendar, load_exchange_sessions, load_trading_windows

    assert isinstance(configs_dir, Path)
    old = load_trading_windows(configs_dir / "sessions" / "trading_window.toml", version="TW-2026-09-30")
    mc = MarketClock(
        gov.clock.calendar,
        SessionCalendar(load_exchange_sessions(configs_dir / "sessions" / "exchange_sessions.toml"), old),
    )
    g = RiskGovernor(gov.limits, mc, gov._costs, gov._plan)
    s = _long().state
    assert _kinds(g.required_actions(s, ist(15, 0))) == [ActionKind.HALT, ActionKind.ALERT_URGENT]
    ext = old.model_copy(update={"residual_position_policy": "EXIT_ONLY_EXTENSION"})
    from project100c.sessions import ResidualPositionPolicy

    ext = old.model_copy(update={"residual_position_policy": ResidualPositionPolicy.EXIT_ONLY_EXTENSION})
    g3 = RiskGovernor(
        gov.limits, MarketClock(gov.clock.calendar, SessionCalendar(mc.sessions.exchange, ext)), gov._costs, gov._plan
    )
    assert _kinds(g3.required_actions(s, ist(15, 0))) == [ActionKind.FLATTEN]

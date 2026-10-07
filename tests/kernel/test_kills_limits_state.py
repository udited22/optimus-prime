"""Kill switches (all nine), reset rules, limits validation, and the journal-derived KernelState."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from project100c.errors import ConfigError, GovernorError, KernelInvariantError
from project100c.journal import Journal
from project100c.kernel.health import HealthObservation, detect_kills
from project100c.kernel.kills import (
    KILL_ACTIONS,
    RESET_RULES,
    HaltKind,
    HaltLatch,
    KillAction,
    KillLatch,
    KillSwitch,
    ResetActor,
    ResetEvidence,
    check_halt_reset,
    check_reset,
)
from project100c.kernel.limits import RiskLimits, load_risk_limits
from project100c.kernel.state import KernelState

from .conftest import DAY, KEY, Clock, day, ist


def test_nine_kills_each_with_action_and_reset_rule() -> None:
    assert len(KillSwitch) == 9
    for sw in KillSwitch:
        assert KillAction.BLOCK_ENTRIES in KILL_ACTIONS[sw]
        assert sw in RESET_RULES
    assert KillAction.HALT in KILL_ACTIONS[KillSwitch.MANUAL_MASTER]
    assert KillAction.FLATTEN_UNEXPECTED in KILL_ACTIONS[KillSwitch.POSITION_RECONCILIATION]


@pytest.mark.parametrize(
    ("obs", "switch"),
    [
        (HealthObservation(atm_quote_age_s=D("3.5")), KillSwitch.DATA_QUALITY),
        (HealthObservation(feed_disagreement=True), KillSwitch.DATA_QUALITY),
        (HealthObservation(timestamp_drift_s_sustained=D("4")), KillSwitch.DATA_QUALITY),
        (HealthObservation(instrument_master_consistent=False), KillSwitch.DATA_QUALITY),
        (HealthObservation(broker_errors_in_window=5), KillSwitch.BROKER_CONNECTIVITY),
        (HealthObservation(ws_down_s=D("10.1")), KillSwitch.BROKER_CONNECTIVITY),
        (HealthObservation(session_valid=False), KillSwitch.BROKER_CONNECTIVITY),
        (HealthObservation(unknown_order_age_s=D("11")), KillSwitch.BROKER_CONNECTIVITY),
        (HealthObservation(index_move_frac_in_window=D("0.02")), KillSwitch.ABNORMAL_MARKET),
        (HealthObservation(vix_jump_frac=D("0.21")), KillSwitch.ABNORMAL_MARKET),
        (HealthObservation(market_halt_or_circuit=True), KillSwitch.ABNORMAL_MARKET),
        (HealthObservation(cas_anomaly=True), KillSwitch.ABNORMAL_MARKET),
        (HealthObservation(reconciliation_mismatch="qty"), KillSwitch.POSITION_RECONCILIATION),
        (HealthObservation(config_hash_ok=False), KillSwitch.SYSTEM_INTEGRITY),
        (HealthObservation(clock_drift_ms=251), KillSwitch.SYSTEM_INTEGRITY),
        (HealthObservation(disk_free_frac=D("0.05")), KillSwitch.SYSTEM_INTEGRITY),
        (HealthObservation(journal_write_failed=True), KillSwitch.SYSTEM_INTEGRITY),
        (HealthObservation(crash_loop=True), KillSwitch.SYSTEM_INTEGRITY),
        (HealthObservation(memory_pressure=True), KillSwitch.SYSTEM_INTEGRITY),
        (HealthObservation(exposure_limit_breached=True), KillSwitch.PORTFOLIO),
        (HealthObservation(unexplained_pnl_jump=D("101"), nav=D("10000")), KillSwitch.PORTFOLIO),
        (HealthObservation(manual_master_command="HALT"), KillSwitch.MANUAL_MASTER),
    ],
)
def test_health_triggers(limits: RiskLimits, obs: HealthObservation, switch: KillSwitch) -> None:
    assert [s for s, _ in detect_kills(obs, limits)] == [switch]


def test_healthy_and_boundaries_do_not_trigger(limits: RiskLimits) -> None:
    assert detect_kills(HealthObservation(), limits) == []
    edge = HealthObservation(
        atm_quote_age_s=D(3),
        ws_down_s=D(10),
        broker_errors_in_window=2,
        clock_drift_ms=250,
        unexplained_pnl_jump=D(100),
        nav=D(10000),
        disk_free_frac=D("0.10"),
    )
    assert detect_kills(edge, limits) == []


def _latch(sw: KillSwitch, at_min: int = 0) -> KillLatch:
    return KillLatch(sw, "", "t", ist(10, at_min), DAY)


def _ev(**kw: Any) -> ResetEvidence:
    kw.setdefault("actor", ResetActor.SYSTEM)
    kw.setdefault("now", ist(11, 0))
    return ResetEvidence(**kw)


def _reset(latch: KillLatch, ev: ResetEvidence) -> None:
    check_reset(latch, ev, dq_healthy_reset_s=D(60), abnormal_cooloff_min=30)


def test_reset_rules() -> None:
    owner: dict[str, Any] = {"actor": ResetActor.OWNER, "owner_ack_id": "ACK-1"}
    for sw in (
        KillSwitch.PORTFOLIO,
        KillSwitch.POSITION_RECONCILIATION,
        KillSwitch.SYSTEM_INTEGRITY,
        KillSwitch.MANUAL_MASTER,
    ):
        with pytest.raises(GovernorError):
            _reset(_latch(sw), _ev())
        with pytest.raises(GovernorError):
            _reset(_latch(sw), _ev(actor=ResetActor.OWNER))  # no ack id
        _reset(_latch(sw), _ev(**owner))
    with pytest.raises(GovernorError):
        _reset(_latch(KillSwitch.STRATEGY), _ev(**owner))
    _reset(_latch(KillSwitch.STRATEGY), _ev(**owner, validation_report_id="VR-1"))
    with pytest.raises(GovernorError):
        _reset(_latch(KillSwitch.DAILY_LOSS), _ev(**owner))  # even the owner cannot reset it the same day
    with pytest.raises(GovernorError):
        _reset(_latch(KillSwitch.DAILY_LOSS), _ev(preflight_trading_date=DAY))
    _reset(_latch(KillSwitch.DAILY_LOSS), _ev(preflight_trading_date=date(2026, 10, 6)))
    dq = _latch(KillSwitch.DATA_QUALITY)
    with pytest.raises(GovernorError):
        _reset(dq, _ev(dq_healthy_since=ist(10, 59, 30), dq_resynced=True))  # only 30 s
    with pytest.raises(GovernorError):
        _reset(dq, _ev(dq_healthy_since=ist(10, 30), dq_resynced=False))
    with pytest.raises(GovernorError):
        _reset(dq, _ev(dq_healthy_since=ist(9, 0), dq_resynced=True))  # healthy "since" before the latch
    _reset(dq, _ev(dq_healthy_since=ist(10, 30), dq_resynced=True))
    br = _latch(KillSwitch.BROKER_CONNECTIVITY)
    with pytest.raises(GovernorError):
        _reset(br, _ev(broker_connected=True))
    with pytest.raises(GovernorError):
        _reset(br, _ev(broker_connected=True, full_reconciliation_ok_at=ist(9, 0)))
    _reset(br, _ev(broker_connected=True, full_reconciliation_ok_at=ist(10, 30)))
    ab = _latch(KillSwitch.ABNORMAL_MARKET, 45)
    with pytest.raises(GovernorError):
        _reset(ab, _ev())  # 15 min < 30 min cool-off
    _reset(ab, _ev(now=ist(11, 15)))
    _reset(ab, _ev(**owner))
    h = HaltLatch(HaltKind.WEEKLY_FREEZE, "t", ist(10, 0))
    with pytest.raises(GovernorError):
        check_halt_reset(h, _ev())
    check_halt_reset(h, _ev(**owner))


# ---- limits ----
def test_limits_values_and_pending_flags(limits: RiskLimits) -> None:
    assert (limits.per_trade_max_loss_frac, limits.daily_stop_frac, limits.weekly_freeze_frac) == (
        D("0.02"),
        D("0.04"),
        D("0.08"),
    )
    assert (limits.dd_warning_frac, limits.dd_suspend_frac, limits.dd_hard_ceiling_frac) == (
        D("0.10"),
        D("0.125"),
        D("0.15"),
    )
    assert limits.version == "RL-2026-10-03.2"
    assert "dd_suspend_frac" not in limits.pending_signoff and limits.max_lots == 1  # OD-010 confirmed
    assert {"OD-008", "OD-009", "OD-010"} <= set(limits.owner_decisions)
    assert limits.pending_signoff == ()  # OD-017: the remaining limits were delegated and are now chosen


def _raw(configs_dir: Path) -> dict[str, Any]:
    import tomllib

    with (configs_dir / "risk" / "limits.toml").open("rb") as fh:
        return dict(tomllib.load(fh)["limits"][0])


@pytest.mark.parametrize(
    "over",
    [
        {"dd_hard_ceiling_frac": "0.16"},  # above directive ceiling
        {"dd_suspend_frac": "0.15"},  # must be < hard
        {"dd_warning_frac": "0.13"},
        {"max_lots": 2},
        {"per_trade_max_loss_frac": "0.05"},  # > daily stop
        {"daily_stop_frac": 0.04},  # float forbidden
        {"quote_max_age_s": "0"},
        {"pending_signoff": ["nope"]},
        {"unknown_field": 1},
        {"spread_max_frac_of_mid": "1.5"},
    ],
)
def test_limits_validation(configs_dir: Path, over: dict[str, Any]) -> None:
    from pydantic import ValidationError

    raw = _raw(configs_dir) | over
    with pytest.raises(ValidationError):
        RiskLimits.model_validate(raw)


def test_limits_loader_errors(tmp_path: Path, configs_dir: Path) -> None:
    with pytest.raises(ConfigError):
        load_risk_limits(tmp_path / "missing.toml")
    (tmp_path / "e.toml").write_text("x = 1\n")
    with pytest.raises(ConfigError):
        load_risk_limits(tmp_path / "e.toml")
    with pytest.raises(ConfigError):
        load_risk_limits(configs_dir / "risk" / "limits.toml", version="nope")
    assert load_risk_limits(configs_dir / "risk" / "limits.toml", version="RL-2026-09-30.1").version


# ---- state ----
def test_state_replay_from_real_journal_keeps_kills(tmp_path: Path) -> None:
    c = Clock(ist(9, 16))
    with Journal(tmp_path / "j.db", clock=c) as j:
        j.append("DAY_START", {"trading_date": DAY, "sod_nav": D("10000"), "sow_nav": D("10000"), "week_start": True})
        j.append("KILL_LATCHED", {"switch": "MANUAL_MASTER_KILL", "scope": "", "reason": "owner"})
        j.append("HALT_LATCHED", {"kind": "WEEKLY_FREEZE", "reason": "t"})
    with Journal(tmp_path / "j.db", clock=c) as j2:  # "restart"
        s = KernelState.from_journal(j2)
    assert (KillSwitch.MANUAL_MASTER, "") in s.kills and HaltKind.WEEKLY_FREEZE in s.halts


def test_state_pnl_nav_and_drawdown() -> None:
    e = day()
    e.add(
        "ORDER_SUBMITTED",
        client_order_id="E",
        strategy_id="S",
        instrument_key=KEY,
        side="BUY",
        qty=65,
        price=D("3"),
        kind="ENTRY",
        lot_size=65,
    )
    e.add("FILL", client_order_id="E", trade_id="1", qty=20, price=D("3"), charges=D("2"))
    e.add("FILL", client_order_id="E", trade_id="2", qty=45, price=D("3.10"), charges=D("3"))
    p = e.state.positions[KEY]
    assert p.qty == 65 and p.avg_price == (D(60) + D("139.5")) / 65 and "E" not in e.state.open_orders
    e.add(
        "ORDER_SUBMITTED",
        client_order_id="X",
        strategy_id="S",
        instrument_key=KEY,
        side="SELL",
        qty=65,
        price=D("2"),
        kind="PROTECTIVE",
        lot_size=65,
    )
    assert e.state.pending_sell_qty() == {KEY: 65}
    e.add("FILL", client_order_id="X", trade_id="3", qty=65, price=D("2"), charges=D("4"))
    assert e.state.positions == {} and e.state.consecutive_stops["S"] == 1
    loss = (D(2) * 65 - (D(60) + D("139.5"))) - D(4) - D(5)
    assert e.state.realised_today == loss and e.state.nav == D(10000) + loss
    assert e.state.daily_loss == -loss and e.state.drawdown_frac > 0
    t = e.state.closed_trades[-1]
    assert t.stopped_out and t.lots == 1 and t.pnl == loss


@pytest.mark.parametrize(
    ("et", "payload"),
    [
        ("NOPE", {}),
        ("FILL", {"client_order_id": "missing", "trade_id": "1", "qty": 1, "price": D(1), "charges": D(0)}),
        ("ORDER_TERMINAL", {"client_order_id": "missing", "status": "X"}),
        ("PROTECTIVE_CONFIRMED", {"instrument_key": KEY, "client_order_id": "x"}),
        ("KILL_RESET", {"switch": "MANUAL_MASTER_KILL", "scope": "", "actor": "OWNER"}),
        ("HALT_RESET", {"kind": "WEEKLY_FREEZE", "actor": "OWNER"}),
        ("MARK", {"unrealised": 1.5}),
        ("POSITION_ADOPTED", {"instrument_key": KEY, "qty": -65, "avg_price": D(1), "lot_size": 65}),
        ("DAY_START", {"trading_date": "2026-10-05", "sod_nav": D(1)}),
    ],
)
def test_state_rejects_malformed_or_impossible_events(et: str, payload: dict[str, Any]) -> None:
    with pytest.raises(KernelInvariantError):
        day().add(et, **payload)


def test_state_invariants_short_duplicate_overnight() -> None:
    e = day()
    e.add(
        "ORDER_SUBMITTED",
        client_order_id="X",
        strategy_id="S",
        instrument_key=KEY,
        side="SELL",
        qty=65,
        price=D("2"),
        kind="EXIT",
        lot_size=65,
    )
    with pytest.raises(KernelInvariantError, match="net short"):
        e.add("FILL", client_order_id="X", trade_id="1", qty=65, price=D(2), charges=D(0))
    with pytest.raises(KernelInvariantError, match="duplicate"):
        e.add(
            "ORDER_SUBMITTED",
            client_order_id="X",
            strategy_id="S",
            instrument_key=KEY,
            side="SELL",
            qty=65,
            price=D("2"),
            kind="EXIT",
            lot_size=65,
        )
    e2 = day().add("POSITION_ADOPTED", instrument_key=KEY, qty=65, avg_price=D(3), lot_size=65)
    with pytest.raises(KernelInvariantError, match="overnight"):
        e2.add("DAY_START", trading_date=date(2026, 10, 6), sod_nav=D(1), week_start=False)
    with pytest.raises(KernelInvariantError, match="before any DAY_START"):
        from .conftest import Events

        Events().add("KILL_LATCHED", switch="MANUAL_MASTER_KILL", scope="", reason="x")
    assert timedelta(0) == timedelta(0)

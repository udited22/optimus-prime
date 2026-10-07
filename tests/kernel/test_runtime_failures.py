"""K-02 failure injection: Governor + fake broker (K-08) + journal (K-01) through the runtime harness.

Covers rejects, partial fills, delayed fills, lost acks, disconnects, reconciliation mismatch, journal failure,
missing protective stop, forced flatten, and the OD-007 15:00 Exit-All path including its failure modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from project100c.broker import BrokerAdapter, FakeBroker, OrderStatus, OrderType
from project100c.core_types import OrderSide
from project100c.costs import CostModel
from project100c.errors import BrokerRejectError, JournalError
from project100c.execution import ExecutionGateway
from project100c.journal import Journal, JournalRecord
from project100c.kernel.governor import Reason, RiskGovernor
from project100c.kernel.health import HealthObservation
from project100c.kernel.kills import HaltKind, KillSwitch
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.kernel.state import KernelState

from .conftest import DAY, KEY, Clock, intent, ist, market, quote


class FlakyJournal(Journal):
    fail = False

    def append(self, event_type: str, payload: dict[str, Any], *, correlation_id: str = "") -> JournalRecord:
        if self.fail:
            raise JournalError("injected: disk full")
        return super().append(event_type, payload, correlation_id=correlation_id)


@dataclass
class Rig:
    clock: Clock
    broker: FakeBroker
    journal: FlakyJournal
    rt: KernelRuntime
    alerts: MemoryAlerts
    gov: RiskGovernor
    costs: CostModel
    plan: str
    path: Path
    venue: BrokerAdapter  # what the runtime talks to: the fake broker itself, or the execution gateway around it

    def q(self, bid: str | None, ask: str | None, *, bid_qty: int = 6500, ask_qty: int = 6500) -> None:
        b = D(bid) if bid is not None else None
        a = D(ask) if ask is not None else None
        self.broker.set_quote(KEY, b, a, bid_qty=bid_qty, ask_qty=ask_qty)
        if b is not None and a is not None:
            self.rt.step({KEY: quote(bid or "0", ask or "0", ts=self.clock.t)})
        else:
            from project100c.market_types import Quote

            self.rt.step({KEY: Quote(KEY, self.clock.t, self.clock.t, b, a, None, None, None, None)})

    def at(self, hh: int, mm: int, ss: int = 0) -> None:
        self.clock.t = ist(hh, mm, ss)

    def enter(self, **kw: Any) -> Any:
        now = self.clock.t
        return self.rt.submit(intent(decided_at=now, **kw), market(now, q=quote(ts=now)))

    def restart(self) -> KernelRuntime:
        j = FlakyJournal(self.path, clock=self.clock)
        return KernelRuntime(j, self.venue, self.gov, self.costs, self.plan, self.clock, self.alerts)

    def events(self) -> list[str]:
        return [r.event_type for r in self.journal.records()]


@pytest.fixture(params=["direct", "gateway"])
def rig(request: pytest.FixtureRequest, tmp_path: Path, gov: RiskGovernor, costs: CostModel, plan_id: str) -> Rig:
    """Every runtime scenario runs twice: against the fake broker directly, and through the production
    ExecutionGateway (K-07) wrapped around it -- the same code path a live or paper venue uses."""
    c = Clock(ist(9, 16))
    b = FakeBroker(c, funds=D("10000"))
    b.set_quote(KEY, D("2.95"), D("3.00"))
    venue: BrokerAdapter = b if request.param == "direct" else ExecutionGateway(b, c)
    path = tmp_path / "j.db"
    j = FlakyJournal(path, clock=c)
    alerts = MemoryAlerts()
    rt = KernelRuntime(j, venue, gov, costs, plan_id, c, alerts)
    rt.start_day(DAY, D("10000"), week_start=True)
    r = Rig(c, b, j, rt, alerts, gov, costs, plan_id, path, venue)
    r.at(10, 0)
    return r


def _sl(r: Rig) -> list[Any]:
    return [o for o in r.broker.orders() if o.request.order_type is OrderType.SL]


def test_happy_path_entry_protective_stop_out(rig: Rig) -> None:
    d = rig.enter()
    assert d.approved
    rig.q("2.95", "3.00")  # sync: fill + protective placed
    rig.clock.adv(1)
    rig.q("2.95", "3.00")  # protective confirmed resting
    assert rig.rt.state.positions[KEY].protective_confirmed
    (sl,) = _sl(rig)
    assert sl.status is OrderStatus.TRIGGER_PENDING and sl.request.qty == 65 and sl.trigger_price == D("2.30")
    rig.clock.adv(60)
    rig.q("2.25", "2.30")  # stop triggers and fills at 2.25
    assert rig.rt.state.positions == {}
    assert rig.rt.state.consecutive_stops["S-ORB-001"] == 1
    assert rig.rt.state.kills == {}
    assert rig.rt.state.realised_today < 0
    assert "PROTECTIVE_CONFIRMED" in rig.events()
    rig.journal.verify()


def test_rejects_are_journalled_and_escalate_to_broker_kill(rig: Rig) -> None:
    rig.broker.faults.reject_next_places.extend(["EXCHANGE: price band"] * 3)
    for i in range(3):
        rig.enter(intent_id=f"R{i}")
        rig.clock.adv(1)
    assert not rig.rt.state.open_orders and not rig.rt.state.positions
    rig.rt.step()
    assert (KillSwitch.BROKER_CONNECTIVITY, "") in rig.rt.state.kills
    assert Reason.KILL_ACTIVE in rig.enter(intent_id="R9").reasons


def test_partial_fill_gets_stop_for_filled_qty_then_resized(rig: Rig) -> None:
    rig.broker.set_quote(KEY, D("2.95"), D("3.00"), ask_qty=20)
    rig.enter()
    rig.rt.step({KEY: quote(ts=rig.clock.t)})
    (sl,) = _sl(rig)
    assert sl.request.qty == 20  # protected for exactly what we own
    rig.clock.adv(1)
    rig.q("2.95", "3.00")  # rest of the entry fills
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    sls = _sl(rig)
    assert [o.status for o in sls] == [OrderStatus.CANCELLED, OrderStatus.TRIGGER_PENDING]
    assert sls[-1].request.qty == 65 and rig.rt.state.positions[KEY].qty == 65


def test_delayed_fill_then_protective(rig: Rig) -> None:
    rig.broker.faults.fill_delay = __import__("datetime").timedelta(seconds=5)
    rig.enter()
    rig.q("2.95", "3.00")
    assert rig.rt.state.positions == {} and len(rig.rt.state.open_orders) == 1
    rig.clock.adv(5)
    rig.q("2.95", "3.00")
    assert rig.rt.state.positions[KEY].qty == 65 and len(_sl(rig)) == 1


def test_disconnect_over_10s_latches_broker_kill_and_stop_still_protects(rig: Rig) -> None:
    rig.enter()
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    from datetime import timedelta

    rig.broker.faults.disconnected_windows.append((rig.clock.t, rig.clock.t + timedelta(seconds=30)))
    rig.q("2.95", "3.00")  # link down noticed
    rig.clock.adv(11)
    rig.q("2.20", "2.25")  # stop fires at the exchange while we are blind
    assert (KillSwitch.BROKER_CONNECTIVITY, "") in rig.rt.state.kills
    assert KEY in rig.rt.state.positions  # we cannot see the fill yet
    rig.clock.adv(20)
    rig.q("2.25", "2.30")  # link back: fill reconciled, no mismatch
    assert rig.rt.state.positions == {}
    assert (KillSwitch.POSITION_RECONCILIATION, "") not in rig.rt.state.kills
    assert any("BROKER_CONNECTIVITY_KILL" in m for m in rig.alerts.urgent())


def test_lost_ack_resolved_by_client_order_id(rig: Rig) -> None:
    rig.broker.faults.timeout_next_places.append(True)
    rig.enter()
    assert rig.rt.unknown_orders
    rig.q("2.95", "3.00")
    assert not rig.rt.unknown_orders and rig.rt.state.positions[KEY].qty == 65
    assert len([o for o in rig.broker.orders() if o.request.side is OrderSide.BUY]) == 1  # never duplicated


def test_unknown_order_over_10s_latches_broker_kill(rig: Rig) -> None:
    rig.broker.faults.timeout_next_places.append(False)
    rig.enter()
    rig.clock.adv(11)
    rig.q("2.95", "3.00")
    assert (KillSwitch.BROKER_CONNECTIVITY, "") in rig.rt.state.kills
    assert Reason.OPEN_ORDER_LIMIT in rig.enter(intent_id="I2").reasons  # the UNKNOWN still blocks entries


def test_reconciliation_mismatch_adopts_and_flattens_unexpected(rig: Rig) -> None:
    rig.broker.add_phantom_position(KEY, 65)
    rig.q("2.95", "3.00")
    assert (KillSwitch.POSITION_RECONCILIATION, "") in rig.rt.state.kills
    assert rig.rt.state.positions[KEY].strategy_id == "__UNEXPECTED__"
    assert Reason.KILL_ACTIVE in rig.enter().reasons
    rig.clock.adv(1)
    rig.q("2.95", "3.00")  # FLATTEN_UNEXPECTED: sell LIMIT at bid - 2 ticks, fills at bid
    assert rig.rt.state.positions == {}
    assert all(p.net_qty == 0 for p in rig.broker.positions())


def test_journal_failure_is_system_integrity_and_nothing_is_sent(rig: Rig) -> None:
    rig.journal.fail = True
    n_before = len(rig.broker.orders())
    d = rig.enter()
    assert d.reasons == (Reason.SYSTEM_INTEGRITY_FAILURE,)
    assert len(rig.broker.orders()) == n_before  # write-ahead: an unjournalled order never leaves
    assert rig.rt.integrity_failed
    assert any("SYSTEM_INTEGRITY_KILL" in m for m in rig.alerts.urgent())
    assert rig.enter(intent_id="I2").reasons == (Reason.SYSTEM_INTEGRITY_FAILURE,)
    rig.rt.step()
    assert sum("SYSTEM_INTEGRITY_KILL" in m for m in rig.alerts.urgent()) >= 2  # keeps alerting


def test_protective_rejected_then_flatten_within_3s(rig: Rig) -> None:
    rig.enter()
    rig.broker.faults.reject_next_places.append("RMS: SL rejected")
    rig.q("2.95", "3.00")  # entry fill; SL placement rejected
    assert not rig.rt.state.positions[KEY].protective_confirmed
    rig.broker.faults.reject_next_places.append("RMS: SL rejected")
    rig.clock.adv(2)
    rig.q("2.95", "3.00")  # retry also rejected, still < 3 s
    assert KEY in rig.rt.state.positions
    rig.broker.faults.reject_next_places.append("RMS: SL rejected")
    rig.clock.adv(2)
    rig.q("2.95", "3.00")  # > 3 s unprotected: FLATTEN (exit LIMIT sent)
    rig.clock.adv(1)
    rig.q("2.95", "3.00")  # exit fill reconciled
    assert rig.rt.state.positions == {}
    assert any("unprotected position" in m for m in rig.alerts.urgent())


def test_forced_flatten_at_1450(rig: Rig) -> None:
    rig.enter()
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    rig.at(14, 49, 59)
    rig.q("3.10", "3.15")
    assert KEY in rig.rt.state.positions
    rig.at(14, 50)
    rig.q("3.10", "3.15")  # cancel SL, sell LIMIT at bid - 2 ticks -> fills at bid
    rig.clock.adv(1)
    rig.q("3.10", "3.15")
    assert rig.rt.state.positions == {} and rig.rt.state.realised_today > D(-100)


def test_od007_exit_all_at_1500_success(rig: Rig) -> None:
    rig.enter()
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    rig.at(14, 50)
    rig.q(None, "3.00")  # no bid during flatten: cannot exit
    assert KEY in rig.rt.state.positions
    rig.at(15, 0)
    rig.broker.set_quote(KEY, D("2.90"), D("3.00"))
    rig.rt.step({KEY: quote("2.90", "3.00", ts=rig.clock.t)})
    assert rig.rt.state.positions == {}
    assert HaltKind.EXIT_ALL_FAILED not in rig.rt.state.halts
    urgent = rig.alerts.urgent()
    assert any("Exit-All triggered" in m and "UNVERIFIED" in m for m in urgent)
    assert any("Exit-All completed" in m for m in urgent)
    assert "EXIT_ALL_REQUESTED" in rig.events() and "EXIT_ALL_RESULT" in rig.events()


def _open_at_1500(rig: Rig) -> None:
    rig.enter()
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    rig.at(14, 50)
    rig.q(None, "3.00")
    rig.at(15, 0)


@pytest.mark.parametrize("mode", ["error", "skip", "shallow", "disconnect"])
def test_od007_exit_all_failure_halts_everything(rig: Rig, mode: str) -> None:
    _open_at_1500(rig)
    from datetime import timedelta

    if mode == "error":
        rig.broker.faults.exit_all_error = BrokerRejectError("EXIT_ALL_UNAVAILABLE")
        rig.broker.set_quote(KEY, D("2.90"), D("3.00"))
    elif mode == "skip":
        rig.broker.faults.exit_all_skip.add(KEY)
        rig.broker.set_quote(KEY, D("2.90"), D("3.00"))
    elif mode == "shallow":
        rig.broker.set_quote(KEY, D("2.90"), D("3.00"), bid_qty=20)  # partial Exit-All
    else:
        rig.broker.faults.disconnected_windows.append((rig.clock.t, rig.clock.t + timedelta(minutes=5)))
    rig.rt.step({KEY: quote("2.90", "3.00", ts=rig.clock.t)})
    s = rig.rt.state
    assert HaltKind.EXIT_ALL_FAILED in s.halts
    assert (KillSwitch.POSITION_RECONCILIATION, "") in s.kills
    assert any("Exit-All FAILED" in m for m in rig.alerts.urgent())
    n = len(rig.alerts.urgent())
    rig.clock.adv(30)
    rig.rt.step()
    assert len(rig.alerts.urgent()) > n  # keeps alerting
    # halt survives a restart; the next session's entries are refused
    rt2 = rig.restart()
    assert HaltKind.EXIT_ALL_FAILED in rt2.state.halts
    now = ist(10, 0)
    d = rig.gov.evaluate(intent(decided_at=now), rt2.state, market(now))
    assert Reason.HALT_ACTIVE in d.reasons and Reason.ALL_TRADING_HALTED in d.reasons


def test_exit_all_called_once_per_day(rig: Rig) -> None:
    _open_at_1500(rig)
    rig.broker.faults.exit_all_skip.add(KEY)
    rig.broker.set_quote(KEY, D("2.90"), D("3.00"))
    rig.rt.step({KEY: quote("2.90", "3.00", ts=rig.clock.t)})
    rig.clock.adv(10)
    rig.rt.step({KEY: quote("2.90", "3.00", ts=rig.clock.t)})
    assert rig.events().count("EXIT_ALL_REQUESTED") == 1


def test_no_order_activity_after_1500_except_exit_all(rig: Rig) -> None:
    rig.at(15, 0, 1)
    d = rig.enter()
    assert Reason.OUTSIDE_TRADING_WINDOW in d.reasons and rig.broker.orders() == []


def test_kills_survive_restart(rig: Rig) -> None:
    rig.rt.step(health=HealthObservation(manual_master_command="owner HALT via CLI"))
    rt2 = rig.restart()
    assert (KillSwitch.MANUAL_MASTER, "") in rt2.state.kills
    now = rig.clock.t
    assert Reason.KILL_ACTIVE in rt2.submit(intent(decided_at=now), market(now, q=quote(ts=now))).reasons


def test_manual_master_kill_cancels_and_flattens(rig: Rig) -> None:
    rig.broker.set_quote(KEY, D("2.95"), D("3.00"), ask_qty=20)
    rig.enter()
    rig.rt.step({KEY: quote(ts=rig.clock.t)})  # partial 20 + SL(20); entry remainder working
    rig.clock.adv(1)
    rig.rt.step({KEY: quote(ts=rig.clock.t)}, health=HealthObservation(manual_master_command="HALT"))
    entry = next(o for o in rig.broker.orders() if o.request.side is OrderSide.BUY)
    assert entry.status is OrderStatus.CANCELLED
    for _ in range(3):  # the entry got another partial fill before our cancel landed (cancel/fill race)
        rig.clock.adv(1)
        rig.rt.step({KEY: quote(ts=rig.clock.t)})
    assert rig.rt.state.positions == {}
    assert rig.rt.state.open_orders == {}
    assert all(p.net_qty == 0 for p in rig.broker.positions())
    assert (KillSwitch.POSITION_RECONCILIATION, "") not in rig.rt.state.kills  # late fill applied, not a mismatch
    assert any("late fill" in m for _, m in rig.alerts.sent)


def test_daily_loss_kill_flattens_and_resets_next_day(rig: Rig, gov: RiskGovernor) -> None:
    # At 1 lot / Rs10k the worst gap (premium to ~0) is ~Rs190 < the Rs400 daily stop, so the canary cannot
    # trip DAILY_LOSS in one trade. Test-only limits (model_copy skips validation) lower the stop to 1.5%.
    g = RiskGovernor(gov.limits.model_copy(update={"daily_stop_frac": D("0.015")}), gov.clock, rig.costs, rig.plan)
    rt = KernelRuntime(rig.journal, rig.broker, g, rig.costs, rig.plan, rig.clock, rig.alerts)
    now = rig.clock.t
    assert rt.submit(intent(decided_at=now), market(now, q=quote(ts=now))).approved
    rt.step({KEY: quote(ts=rig.clock.t)})
    rig.clock.adv(1)
    rt.step({KEY: quote(ts=rig.clock.t)})
    rig.clock.adv(1)
    rig.broker.set_quote(KEY, D("0.05"), D("0.10"))  # gap through the SL-limit: triggered, unfilled
    rt.step({KEY: quote("0.05", "0.10", ts=rig.clock.t)})
    assert (KillSwitch.DAILY_LOSS, "") in rt.state.kills
    rig.clock.adv(1)
    rt.step({KEY: quote("0.05", "0.10", ts=rig.clock.t)})
    assert rt.state.positions == {}  # flattened
    assert (
        Reason.KILL_ACTIVE
        in rt.submit(
            intent(intent_id="I2", decided_at=rig.clock.t), market(rig.clock.t, q=quote(ts=rig.clock.t))
        ).reasons
    )
    # survives restart the same day; auto-resets at the next session's pre-flight
    rt2 = KernelRuntime(rig.journal, rig.broker, g, rig.costs, rig.plan, rig.clock, rig.alerts)
    assert (KillSwitch.DAILY_LOSS, "") in rt2.state.kills
    rt2.start_day(date(2026, 10, 6), rt2.state.nav, week_start=False)
    assert (KillSwitch.DAILY_LOSS, "") not in rt2.state.kills
    assert KernelState.from_journal(rig.journal).kills == {}


def test_duplicate_fill_messages_are_applied_once(rig: Rig) -> None:
    rig.broker.faults.duplicate_fills = True
    rig.enter()
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    assert rig.rt.state.positions[KEY].qty == 65
    assert rig.events().count("FILL") == 1
    assert rig.rt.state.kills == {}


def test_manual_master_kill_latches_flattens_and_survives_restart(rig: Rig) -> None:
    assert rig.enter().approved
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    assert KEY in rig.rt.state.positions
    assert rig.rt.manual_master_kill("owner pressed the dashboard button", requested_by="dashboard:test")
    assert not rig.rt.manual_master_kill("again", requested_by="dashboard:test")  # idempotent
    assert (KillSwitch.MANUAL_MASTER, "") in rig.rt.state.kills
    rig.clock.adv(1)
    rig.q("2.95", "3.00")  # governor's required actions: cancel protective, flatten
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    assert rig.rt.state.positions == {}
    assert Reason.KILL_ACTIVE in rig.enter(intent_id="AFTER").reasons
    rt2 = rig.restart()
    assert (KillSwitch.MANUAL_MASTER, "") in rt2.state.kills  # latched in the journal
    with pytest.raises(Exception, match="reason"):
        rt2.manual_master_kill("  ", requested_by="x")


def test_request_exit_sells_to_close_and_reentry_gets_a_fresh_protective(rig: Rig) -> None:
    assert not rig.rt.request_exit(KEY, "nothing open")  # flat: nothing to do, nothing sent
    assert rig.enter().approved
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("3.40", "3.45")
    assert rig.rt.state.positions[KEY].protective_confirmed
    assert rig.rt.request_exit(KEY, "TARGET")
    rig.clock.adv(1)
    rig.q("3.40", "3.45")
    assert rig.rt.state.positions == {}
    kinds = [r.payload.get("kind") for r in rig.journal.records() if r.event_type == "ORDER_SUBMITTED"]
    assert kinds == ["ENTRY", "PROTECTIVE", "EXIT"]
    # a later trade on the SAME instrument must get its own broker-side stop (a finished exit does not block it)
    rig.at(11, 0)
    assert rig.enter(intent_id="SECOND").approved
    rig.q("2.95", "3.00")
    rig.clock.adv(1)
    rig.q("2.95", "3.00")
    assert rig.rt.state.positions[KEY].protective_confirmed

"""The standalone reconciler (K-09, docs/architecture/execution-engine.md §10.5): an independent second check of the
journal against the
broker, on its own schedule.

The runtime already compares positions on every step. This service adds what that step does not cover and does
not trust the runtime to have done it:

* it reads the broker on its **own** timer (default every 2.5 s) through any ``BrokerAdapter`` (read calls only:
  ``positions()`` and ``orders()``; it never places, modifies or cancels);
* it compares positions **and** live orders with ``execution.reconcile`` (unexpected or missing positions, a net
  short, a live broker order the journal never submitted, a journal-open order the broker does not have);
* it reports a finding only after it has persisted for ``confirm_polls`` reads in a row, so an order or fill in
  flight between the journal and the broker is not a false alarm (a persistent mismatch is reported within
  ``interval * confirm_polls``, 5 s by default);
* each distinct persistent finding set is reported once through ``on_mismatch`` (the host wires this to
  ``KernelRuntime.report_reconciliation_mismatch``, which latches POSITION_RECONCILIATION_KILL) and alerted;
* failing reads are counted and alerted after ``max_read_errors`` in a row (the runtime's own broker-error
  kills still apply).

It runs in the host loop between runtime steps (``tick(now)``), so the journal is never touched from two threads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from project100c.broker.types import BrokerAdapter
from project100c.errors import BrokerError
from project100c.execution.reconcile import ReconReport, reconcile
from project100c.kernel.state import KernelState


class _Alerts(Protocol):
    def send(self, severity: str, message: str) -> None: ...


def _none() -> Iterable[str]:
    return ()


@dataclass
class ReconcilerService:
    state_source: Callable[[], KernelState]
    broker: BrokerAdapter
    alerts: _Alerts
    on_mismatch: Callable[[str], object]
    # orders whose fate is UNKNOWN after a timeout: the runtime resolves them (and kills after
    # broker_unknown_order_s); they are not a reconciliation finding while unknown
    unknown_orders: Callable[[], Iterable[str]] = _none
    interval: timedelta = timedelta(seconds=2.5)
    confirm_polls: int = 2
    max_read_errors: int = 3
    polls: int = 0
    read_errors: int = 0
    reports: list[str] = field(default_factory=list)
    _last_run: datetime | None = None
    _streak: int = 0
    _last_sig: str = ""
    _reported_sig: str = ""

    def __post_init__(self) -> None:
        if self.confirm_polls < 1 or self.interval <= timedelta(0):
            raise ValueError("confirm_polls >= 1 and a positive interval are required")

    def due(self, now: datetime) -> bool:
        return self._last_run is None or now - self._last_run >= self.interval

    def tick(self, now: datetime) -> ReconReport | None:
        """Run one reconciliation if the interval has elapsed; return its report (None if not due or unreadable)."""
        if not self.due(now):
            return None
        self._last_run = now
        state = self.state_source()
        try:
            positions = self.broker.positions()
            orders = self.broker.orders()
        except BrokerError as e:
            self.read_errors += 1
            if self.read_errors == self.max_read_errors:
                self.alerts.send("URGENT", f"reconciler: {self.read_errors} broker reads failed in a row: {e}")
            return None
        self.read_errors = 0
        self.polls += 1
        unknown = set(self.unknown_orders())
        journal_open = [cid for cid in state.open_orders if cid not in unknown]
        report = reconcile(state.open_qty(), journal_open, positions, orders)
        sig = report.summary()
        if report.clean:
            self._streak, self._last_sig, self._reported_sig = 0, "", ""
            return report
        self._streak = self._streak + 1 if sig == self._last_sig else 1
        self._last_sig = sig
        if self._streak >= self.confirm_polls and sig != self._reported_sig:
            self._reported_sig = sig
            self.reports.append(sig)
            self.alerts.send("URGENT", f"reconciler: journal and broker disagree: {sig}")
            self.on_mismatch(sig)
        return report

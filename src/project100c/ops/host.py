"""The host service loop (K-12, docs/engineering/security.md): runs the session step under an OS supervisor.

`HostService` drives any session with ``runtime`` and ``step()`` (``paper.live.LivePaperSession`` today; the
same step with a real broker later) on a fixed interval, plus the standalone reconciler between steps:

* **Persistent journal.** The runtime is built from a journal on persistent disk (``open_journal`` refuses a
  temporary directory). After a crash or restart the new process rebuilds its state from that journal: kills and
  halts stay latched, positions and working orders are known, and ``ensure_day_started`` does not write a second
  DAY_START for the same trading day.
* **Supervision.** An exception from a step is alerted and counted; the loop carries on. After
  ``max_consecutive_failures`` failed steps in a row it stops with exit code 70, so the OS supervisor (systemd,
  ``deploy/project100c-host.service``) restarts the process from the journal. SIGTERM and SIGINT stop it cleanly
  (exit code 0).
* **Backups.** ``backup_journal`` copies the live journal with SQLite's online backup API (safe while it is
  being written) to a timestamped file.

What a restart does not keep: the broker-side state of ``PaperBroker`` (it is in memory, so a paper session that
restarts mid-day reconciles against an empty simulated book and latches the reconciliation kill, fail closed),
and the daily access token (memory only: a restart needs a new approval, docs/engineering/alerts-and-daily-token.md).
"""

from __future__ import annotations

import signal
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from project100c.execution.reconciler import ReconcilerService
from project100c.journal import Journal
from project100c.kernel.runtime import AlertSink, KernelRuntime

EXIT_STOPPED = 0
EXIT_FAILED = 70  # EX_SOFTWARE: the supervisor restarts the service


class Session(Protocol):
    @property
    def runtime(self) -> KernelRuntime: ...

    def step(self) -> object: ...


def open_journal(path: Path, *, clock: Callable[[], datetime], forbid_under: Path | None = None) -> Journal:
    """The host's journal: on persistent disk, never under the temporary directory (``forbid_under``)."""
    p = path.resolve()
    tmp = (forbid_under or Path(tempfile.gettempdir())).resolve()
    if p == tmp or tmp in p.parents:
        raise ValueError(f"{path}: the host journal must be on persistent disk, not under {tmp}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return Journal(p, clock=clock)


def backup_journal(src: Path, dest_dir: Path, now: datetime) -> Path:
    """Online copy of the journal (consistent while the host writes to it). Returns the backup path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.stem}-{now.strftime('%Y%m%dT%H%M%S')}{src.suffix}"
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(out)
    try:
        with d:
            s.backup(d)
    finally:
        d.close()
        s.close()
    return out


@dataclass
class HostService:
    session: Session
    clock: Callable[[], datetime]
    alerts: AlertSink
    reconciler: ReconcilerService | None = None
    interval_s: float = 1.0
    max_consecutive_failures: int = 5
    sleep: Callable[[float], None] = time.sleep
    stop: threading.Event = field(default_factory=threading.Event)
    steps: int = 0
    failures: int = 0
    total_failures: int = 0

    def __post_init__(self) -> None:
        if self.max_consecutive_failures < 1 or self.interval_s <= 0:
            raise ValueError("max_consecutive_failures >= 1 and interval_s > 0 are required")

    @property
    def runtime(self) -> KernelRuntime:
        return self.session.runtime

    def ensure_day_started(
        self, trading_date: date, sod_nav: Decimal, *, week_start: bool, sow_nav: Decimal | None = None
    ) -> bool:
        """Write DAY_START unless the journal already has it for this trading day (a restart). True if written."""
        if self.runtime.state.trading_date == trading_date:
            self.alerts.send("INFO", f"host restarted on {trading_date}: state rebuilt from the journal")
            return False
        self.runtime.start_day(trading_date, sod_nav, week_start=week_start, sow_nav=sow_nav)
        return True

    def run_once(self) -> bool:
        try:
            self.session.step()
            if self.reconciler is not None:
                self.reconciler.tick(self.clock())
        except Exception as e:
            self.failures += 1
            self.total_failures += 1
            self.alerts.send("URGENT", f"host step failed ({self.failures} in a row): {type(e).__name__}: {e}")
            return False
        self.steps += 1
        self.failures = 0
        return True

    def run(self, *, max_steps: int | None = None) -> int:
        n = 0
        while not self.stop.is_set() and (max_steps is None or n < max_steps):
            t0 = time.monotonic()
            self.run_once()
            n += 1
            if self.failures >= self.max_consecutive_failures:
                self.alerts.send("URGENT", f"host stopping after {self.failures} failed steps: supervisor restarts it")
                return EXIT_FAILED
            self.sleep(max(0.0, self.interval_s - (time.monotonic() - t0)))
        return EXIT_STOPPED

    def install_stop_signals(self) -> None:
        """SIGTERM / SIGINT end the loop after the current step (call from the main thread)."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())

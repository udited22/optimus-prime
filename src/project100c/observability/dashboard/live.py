"""Live mode: a background thread runs SIMULATED sessions back to back and publishes their events on the bus at a
chosen speed. It is 'live' only in the sense of a running stream: the market is synthetic and the broker is fake."""

from __future__ import annotations

import queue
import tempfile
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

from project100c.observability.dashboard.events import DashboardError, EventBus
from project100c.observability.dashboard.simulator import REPLAY_SCENARIOS, Kernel, KillPort, Scenario, SimulatedSession
from project100c.sessions import IST


def live_scenarios(kernel: Kernel, start: date, n: int) -> list[Scenario]:
    """``n`` SIMULATED trading days from ``start``; every third one carries a fake-broker link drop."""
    out: list[Scenario] = []
    d = start
    while len(out) < n:
        if kernel.market_clock.calendar.is_trading_day(d):
            i = len(out)
            drop = REPLAY_SCENARIOS[2].broker_drop if i % 3 == 2 else None
            out.append(Scenario(d, 9000 + i, f"live-{d:%Y%m%d}", f"{d:%a %d-%b-%Y} · SIMULATED · live stream", drop))
        d += timedelta(days=1)
    return out


@dataclass
class _KillRequest:
    reason: str
    requested_by: str
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: Exception | None = None


@dataclass
class LiveRunner:
    bus: EventBus
    kernel: Kernel
    speed: float = 30.0  # simulated seconds per wall second
    start: date = date(2026, 10, 5)
    days: int = 20
    step_s: int = 15
    workdir: Path | None = None
    join_at: time | None = None  # first day only: fast-forward (no pacing) to this IST time, i.e. join in progress
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _tmp: tempfile.TemporaryDirectory[str] | None = field(default=None, init=False)
    _session: SimulatedSession | None = field(default=None, init=False)
    _requests: queue.Queue[_KillRequest] = field(default_factory=queue.Queue, init=False)

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise DashboardError("live speed must be > 0")

    @property
    def session(self) -> SimulatedSession | None:
        """The running session (read-only inspection; its journal must only be written on the runner thread)."""
        return self._session

    def kill_port(self) -> KillPort:
        return KillPort(self._kill)

    def _kill(self, reason: str, requested_by: str) -> dict[str, Any]:
        """Called on an HTTP thread. The session (and its SQLite journal) belongs to the runner thread, so the
        request is handed over and executed there between two steps."""
        if self._thread is None or not self._thread.is_alive():
            raise DashboardError("the live simulator is not running")
        req = _KillRequest(reason, requested_by)
        self._requests.put(req)
        if not req.done.wait(10.0):
            raise DashboardError("the live simulator did not process the kill request within 10 s")
        if req.error is not None:
            raise req.error
        assert req.result is not None
        return req.result

    def _serve_requests(self, sess: SimulatedSession, wait: float) -> bool:
        """Wait up to ``wait`` seconds, executing kill requests meanwhile. True if the runner should stop."""
        end = _time.monotonic() + wait
        while True:
            left = end - _time.monotonic()
            if self._stop.is_set():
                return True
            try:
                req = self._requests.get(timeout=max(0.0, min(left, 0.25)))
            except queue.Empty:
                if left <= 0:
                    return False
                continue
            try:
                res = sess.manual_master_kill(req.reason, req.requested_by)
                self.bus.publish(sess.drain_after_kill())
                req.result = {**res, "scenario": sess.scenario.name, "simulated": True}
            except Exception as e:  # handed back to the HTTP thread, which reports it; never swallowed
                req.error = e
            finally:
                req.done.set()

    def start_thread(self) -> None:
        if self._thread is not None:
            raise DashboardError("live runner already started")
        if self.workdir is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="p100c-live-")
            self.workdir = Path(self._tmp.name)
        self._thread = threading.Thread(target=self._run, name="p100c-live-sim", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self.workdir is not None
        pause = self.step_s / self.speed
        first = True
        for sc in live_scenarios(self.kernel, self.start, self.days):
            sess = SimulatedSession(sc, self.kernel, self.workdir, step_s=self.step_s)
            self._session = sess
            batch = []
            last_ts = None
            join = self.join_at if first else None
            first = False
            for ev in sess.run():
                if self._stop.is_set():
                    return
                if join is not None and ev.ts.astimezone(IST).time() < join:
                    batch.append(ev)
                    if len(batch) >= 2000:
                        self.bus.publish(batch)
                        batch = []
                    continue
                if last_ts is not None and ev.ts != last_ts:
                    self.bus.publish(batch)
                    batch = []
                    if self._serve_requests(sess, pause):
                        return
                batch.append(ev)
                last_ts = ev.ts
            self.bus.publish(batch)
            stop = self._serve_requests(sess, max(1.0, 20 * pause))
            sess.journal.close()
            if stop:
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.bus.close()
        if self._tmp is not None:
            self._tmp.cleanup()

    def wait_for(self, predicate: Any, timeout: float = 10.0) -> bool:
        """Test helper: poll until ``predicate()`` is true."""
        end = _time.monotonic() + timeout
        while _time.monotonic() < end:
            if predicate():
                return True
            _time.sleep(0.02)
        return False

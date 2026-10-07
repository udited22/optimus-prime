"""Replay catalogue: whole SIMULATED days built on first request and cached (deterministic per scenario)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from project100c.observability.dashboard.events import DashboardError
from project100c.observability.dashboard.simulator import REPLAY_SCENARIOS, Kernel, Scenario, build_replay


@dataclass
class ReplayLibrary:
    kernel: Kernel
    scenarios: tuple[Scenario, ...] = REPLAY_SCENARIOS
    step_s: int = 15
    _cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def catalogue(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "day": s.day.isoformat(), "title": s.title, "seed": s.seed} for s in self.scenarios]

    def day(self, name: str) -> list[dict[str, Any]]:
        sc = next((s for s in self.scenarios if s.name == name), None)
        if sc is None:
            raise DashboardError(f"no replay named {name!r}")
        with self._lock:
            if name not in self._cache:
                self._cache[name] = build_replay(sc, self.kernel, step_s=self.step_s)
            return self._cache[name]

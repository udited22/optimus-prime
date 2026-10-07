from __future__ import annotations

from typing import Any

import pytest

from project100c.observability.dashboard.simulator import REPLAY_SCENARIOS, Kernel, build_replay


@pytest.fixture(scope="session")
def kernel() -> Kernel:
    return Kernel.load()


@pytest.fixture(scope="session")
def replays(kernel: Kernel) -> dict[str, list[dict[str, Any]]]:
    return {sc.name: build_replay(sc, kernel) for sc in REPLAY_SCENARIOS}

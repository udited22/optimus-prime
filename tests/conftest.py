from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def configs_dir() -> Path:
    return REPO_ROOT / "configs"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return REPO_ROOT / "tests" / "fixtures"

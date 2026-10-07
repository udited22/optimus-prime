from __future__ import annotations

from pathlib import Path

import pytest

from project100c.costs import CostModel, load_brokerage_plans, load_charge_book
from project100c.economics import EconomicsConfig, load_economics_config


@pytest.fixture(scope="session")
def econ_path(configs_dir: Path) -> Path:
    return configs_dir / "economics" / "economics.toml"


@pytest.fixture(scope="session")
def cfg(econ_path: Path) -> EconomicsConfig:
    return load_economics_config(econ_path)


@pytest.fixture(scope="session")
def costs(configs_dir: Path) -> CostModel:
    return CostModel(
        load_charge_book(configs_dir / "costs" / "nse_fo_index_options.toml"),
        load_brokerage_plans(configs_dir / "costs" / "brokerage_plans.toml"),
    )

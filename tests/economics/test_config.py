"""Economics config: versioned, Decimal-only, labelled, and advisory by construction."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from project100c.economics import EconomicsConfig, Status, load_economics_config
from project100c.errors import ConfigError


def _write(tmp_path: Path, raw: str) -> Path:
    p = tmp_path / "economics.toml"
    p.write_text(raw)
    return p


def _mutate(econ_path: Path, tmp_path: Path, old: str, new: str) -> Path:
    raw = econ_path.read_text()
    assert old in raw, old
    return _write(tmp_path, raw.replace(old, new, 1))


def test_repo_config_loads_with_the_owner_inputs(cfg: EconomicsConfig) -> None:
    assert cfg.config_version == "ECON-2026-10-02.1"
    dhan = cfg.line("dhan-data-api")
    assert (dhan.amount, dhan.gst_rate, dhan.period_days, dhan.status) == (
        Decimal(499),
        Decimal("0.18"),
        30,
        Status.VERIFIED,
    )
    srv = cfg.line("cloud-server-mumbai")
    assert srv.status is Status.ASSUMED and (srv.range_low, srv.range_high) == (Decimal(5), Decimal(20))
    assert cfg.line("llm-api").status is Status.ASSUMED
    plus = cfg.line("upstox-plus")
    assert plus.enabled is False and plus.brokerage_plan_if_enabled == "upstox-plus-options"
    assert cfg.tax.status is Status.ASSUMED and cfg.tax.effective_rate == Decimal("0.3120")
    assert "OD-017" in cfg.tax.basis and "ASSUMED" in cfg.tax.basis  # no CA review (OD-017)
    assert cfg.tax.loss_carry_forward.status is Status.UNVERIFIED and cfg.tax.audit.status is Status.UNVERIFIED
    cj = cfg.cost_justification
    assert cj.advisory_only is True and cj.window_months == 3
    assert cj.min_net_return_over_costs == Decimal("0.005") and cj.fixed_cost_nav_threshold == Decimal("0.01")


def test_every_line_has_an_optimisation_hint(cfg: EconomicsConfig) -> None:
    assert all(len(f.optimisation) > 20 for f in cfg.fixed_cost)


def test_advisory_only_false_is_refused(econ_path: Path, tmp_path: Path) -> None:
    p = _mutate(econ_path, tmp_path, "advisory_only = true", "advisory_only = false")
    with pytest.raises(ConfigError, match="advisory"):
        load_economics_config(p)


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('amount = "499"', "amount = 499.0", "float"),
        ('slab_rate = "0.30"', 'slab_rate = "1.5"', "slab_rate"),
        ("period_days = 30", "", "period_days"),
        ('sources = ["S30"]', "sources = []", "VERIFIED"),
        ('range_low = "5"', 'range_low = "6"', "range_low"),
        ("figure is ASSUMED and is used", "figure is used", "ASSUMED"),
        ('status = "ASSUMED"\ntreatment', 'status = "VERIFIED"\ntreatment', "ASSUMED"),
        ('min_net_return_over_costs = "0.005"', 'min_net_return_over_costs = "2"', "monthly fraction"),
        ('fixed_cost_nav_threshold = "0.01"', 'fixed_cost_nav_threshold = "0"', "fixed_cost_nav_threshold"),
        ('usd_inr = "96"', 'usd_inr = "0"', "usd_inr"),
        ('days_per_month = "30.4375"', 'days_per_month = "45"', "days_per_month"),
        ('rule = "ALL_MONTHS_BELOW"', 'rule = "SOMETIMES"', "rule"),
    ],
)
def test_bad_configs_raise(econ_path: Path, tmp_path: Path, old: str, new: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_economics_config(_mutate(econ_path, tmp_path, old, new))


def test_unknown_keys_and_duplicate_lines_are_refused(econ_path: Path, tmp_path: Path) -> None:
    raw: dict[str, Any] = tomllib.loads(econ_path.read_text())
    raw["surprise"] = 1
    with pytest.raises(Exception, match="surprise"):
        EconomicsConfig.model_validate(raw)
    raw.pop("surprise")
    raw["fixed_cost"].append(dict(raw["fixed_cost"][0]))
    with pytest.raises(Exception, match="duplicate"):
        EconomicsConfig.model_validate(raw)


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_economics_config(tmp_path / "nope.toml")


def test_unknown_line_lookup_raises(cfg: EconomicsConfig) -> None:
    with pytest.raises(ConfigError):
        cfg.line("netflix")

"""StrategySpec schema rules (docs/architecture/strategyspec.md, OD-002/003/006)."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from project100c.costs.config import load_brokerage_plans, load_charge_book
from project100c.errors import SpecValidationError
from project100c.sessions import load_trading_windows
from project100c.spec import (
    ConfidenceLevel,
    Lifecycle,
    check_against_window,
    check_cost_reference,
    check_transition,
    load_authored_spec,
    load_spec,
    load_spec_file,
    parse_yaml,
)
from project100c.spec.io import json_schema

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "tests" / "fixtures" / "spec" / "S-ORB-001.yaml"
SCHEMA_FILE = REPO / "schemas" / "strategyspec.schema.json"
EV = {"artifact": "evidence/x.json", "sha256": "a" * 64, "produced_by": "pipeline.backtest"}


@pytest.fixture
def raw() -> dict[str, Any]:
    return parse_yaml(EXAMPLE.read_text())


def test_example_spec_loads_with_pipeline_defaults(raw: dict[str, Any]) -> None:
    spec = load_authored_spec(raw)
    assert spec.status is Lifecycle.RESEARCH
    assert spec.evidence.is_pending()
    assert spec.confidence.level is ConfidenceLevel.NONE
    assert spec.dependencies.min_capital_inr == "pending"
    assert spec.signal.params["theta_v"].value == Decimal("1.5")  # YAML float -> exact Decimal
    assert len(spec.spec_hash()) == 64
    assert load_spec_file(EXAMPLE) == spec


def test_spec_hash_is_stable_and_sensitive(raw: dict[str, Any]) -> None:
    a = load_authored_spec(raw)
    assert a.spec_hash() == load_authored_spec(copy.deepcopy(raw)).spec_hash()
    raw["signal"]["params"]["theta_v"]["value"] = "1.6"
    assert load_authored_spec(raw).spec_hash() != a.spec_hash()


def _bad(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(SpecValidationError, match=match):
        load_authored_spec(raw)


# ---- OD-006 long options only ----
@pytest.mark.parametrize("side", ["SELL", "sell", "SHORT"])
def test_sell_leg_rejected(raw: dict[str, Any], side: str) -> None:
    raw["legs"] = [{"side": side, "right": "PE"}]
    _bad(raw, "MANDATE_LONG_ONLY")


def test_spread_with_short_leg_rejected(raw: dict[str, Any]) -> None:
    raw["legs"] = [{"side": "BUY", "right": "CE"}, {"side": "SELL", "right": "CE"}]
    _bad(raw, "MANDATE_LONG_ONLY")


def test_two_same_right_buy_legs_rejected(raw: dict[str, Any]) -> None:
    raw["legs"] = [{"side": "BUY", "right": "CE"}, {"side": "BUY", "right": "CE"}]
    _bad(raw, "one CE and one PE")


def test_long_straddle_allowed(raw: dict[str, Any]) -> None:
    raw["legs"] = [{"side": "BUY", "right": "CE"}, {"side": "BUY", "right": "PE"}]
    assert len(load_authored_spec(raw).legs) == 2


def test_non_nifty_or_non_option_rejected(raw: dict[str, Any]) -> None:
    raw["legs"] = [{"side": "BUY", "right": "CE", "underlying": "BANKNIFTY"}]
    _bad(raw, "invalid StrategySpec")
    raw["legs"] = [{"side": "BUY", "right": "CE", "instrument": "FUTURE"}]
    _bad(raw, "invalid StrategySpec")


def test_no_legs_rejected(raw: dict[str, Any]) -> None:
    raw["legs"] = []
    _bad(raw, "invalid StrategySpec")


# ---- params must be ranged ----
def test_unranged_param_rejected(raw: dict[str, Any]) -> None:
    raw["signal"]["params"]["theta_v"] = {"value": "1.5"}
    _bad(raw, "min")


def test_param_outside_range_rejected(raw: dict[str, Any]) -> None:
    raw["signal"]["params"]["theta_v"]["value"] = "2.5"
    _bad(raw, "outside its range")


def test_param_float_from_json_rejected(raw: dict[str, Any]) -> None:
    raw["signal"]["params"]["theta_v"] = {"value": 1.5, "min": "1.2", "max": "2.0"}
    _bad(raw, "floats are not allowed")


def test_degenerate_range_rejected(raw: dict[str, Any]) -> None:
    raw["signal"]["params"]["theta_v"] = {"value": "1.5", "min": "1.5", "max": "1.5"}
    _bad(raw, "min < max")


# ---- time rules ----
@pytest.mark.parametrize("end", ["14:00:01", "14:01:00", "14:30:00", "14:45:00", "14:46:00"])
def test_entry_after_1400_rejected(raw: dict[str, Any], end: str) -> None:
    raw["entry"]["window_end"] = end  # OD-008: entries must end by 14:00
    _bad(raw, "ENTRY_AFTER_CUTOFF")


def test_entry_window_ending_exactly_1400_allowed(raw: dict[str, Any], configs_dir: Path) -> None:
    raw["entry"]["window_end"] = "14:00:00"
    spec = load_authored_spec(raw)
    check_against_window(spec, load_trading_windows(configs_dir / "sessions" / "trading_window.toml"))


def test_entry_before_open_rejected(raw: dict[str, Any]) -> None:
    raw["entry"]["window_start"] = "09:10:00"
    _bad(raw, "before order activity start")


def test_time_exit_after_1500_rejected(raw: dict[str, Any]) -> None:
    raw["exit"]["time_exit"] = "15:05:00"
    _bad(raw, "TIME_EXIT_AFTER_HARD_FLAT")


def test_time_exit_exactly_1500_allowed_by_schema(raw: dict[str, Any]) -> None:
    raw["exit"]["time_exit"] = "15:00:00"
    assert load_authored_spec(raw).exit.time_exit.hour == 15


# ---- orders ----
@pytest.mark.parametrize("otype", ["MARKET", "IOC", "SL_M"])
def test_non_limit_entry_rejected(raw: dict[str, Any], otype: str) -> None:
    raw["entry"]["order"]["type"] = otype
    _bad(raw, "invalid StrategySpec")


def test_stop_must_be_sl_limit_and_price_based(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["exit"]["stop"]["order"] = "SL_M"
    _bad(bad, "invalid StrategySpec")
    bad = copy.deepcopy(raw)
    bad["exit"]["stop"]["method"] = "TIME"
    _bad(bad, "invalid StrategySpec")
    bad = copy.deepcopy(raw)
    bad["exit"]["stop"]["value"] = 100
    _bad(bad, "not a stop")


def test_more_than_one_lot_rejected(raw: dict[str, Any]) -> None:
    raw["sizing"]["max_lots"] = 2
    _bad(raw, "invalid StrategySpec")


# ---- regimes ----
def test_regime_overlap_and_mandatory_prohibitions(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["eligible_regimes"].append("EVENT_REGIME")
    _bad(bad, "both eligible and prohibited")
    bad = copy.deepcopy(raw)
    bad["prohibited_regimes"] = ["EVENT_REGIME"]
    _bad(bad, "NO_EDGE")


# ---- evidence / confidence ownership and lifecycle gating ----
def test_authored_spec_cannot_set_evidence_or_confidence(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["evidence"] = {"in_sample": EV}
    _bad(bad, "EVIDENCE_HAND_SET")
    bad = copy.deepcopy(raw)
    bad["confidence"] = {"level": "HIGH", "rationale": "trust me"}
    _bad(bad, "EVIDENCE_HAND_SET")
    bad = copy.deepcopy(raw)
    bad["dependencies"]["min_capital_inr"] = "10000"
    _bad(bad, "EVIDENCE_HAND_SET")


def test_status_requires_evidence(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["status"] = "BACKTESTED"
    with pytest.raises(SpecValidationError, match="EVIDENCE_MISSING"):
        load_spec(bad)
    ok = copy.deepcopy(raw)
    ok["status"] = "BACKTESTED"
    ok["evidence"] = {"backtest_dataset": EV, "in_sample": EV}
    assert load_spec(ok).status is Lifecycle.BACKTESTED
    with pytest.raises(SpecValidationError, match="EVIDENCE_HAND_SET"):
        load_authored_spec(ok)


def test_validated_requires_confidence(raw: dict[str, Any]) -> None:
    raw["status"] = "VALIDATED"
    raw["evidence"] = {
        k: EV for k in ("backtest_dataset", "in_sample", "out_of_sample", "walk_forward", "holdout", "stress")
    }
    with pytest.raises(SpecValidationError, match="confidence"):
        load_spec(raw)
    raw["confidence"] = {"level": "LOW", "rationale": "validation report R-1"}
    assert load_spec(raw).status is Lifecycle.VALIDATED


def test_bad_evidence_ref_rejected(raw: dict[str, Any]) -> None:
    raw["evidence"] = {"in_sample": {**EV, "sha256": "not-a-hash"}}
    with pytest.raises(SpecValidationError):
        load_spec(raw)


def test_lifecycle_transitions() -> None:
    check_transition(Lifecycle.RESEARCH, Lifecycle.BACKTESTED)
    check_transition(Lifecycle.CANARY, Lifecycle.PRODUCTION)
    check_transition(Lifecycle.PRODUCTION, Lifecycle.DEGRADED)
    check_transition(Lifecycle.DEGRADED, Lifecycle.QUARANTINED)
    check_transition(Lifecycle.PAPER, Lifecycle.RESEARCH)
    for a, b in [
        (Lifecycle.RESEARCH, Lifecycle.CANARY),
        (Lifecycle.BACKTESTED, Lifecycle.PAPER),
        (Lifecycle.RETIRED, Lifecycle.RESEARCH),
        (Lifecycle.QUARANTINED, Lifecycle.PRODUCTION),
    ]:
        with pytest.raises(SpecValidationError, match="ILLEGAL_TRANSITION"):
            check_transition(a, b)


# ---- extra/unknown fields, file handling ----
def test_unknown_field_rejected(raw: dict[str, Any]) -> None:
    raw["leverage"] = 5
    _bad(raw, "invalid StrategySpec")


def test_bad_files(tmp_path: Path) -> None:
    p = tmp_path / "s.txt"
    p.write_text("x")
    with pytest.raises(SpecValidationError, match="unsupported"):
        load_spec_file(p)
    y = tmp_path / "s.yaml"
    y.write_text("- a\n- b\n")
    with pytest.raises(SpecValidationError, match="mapping"):
        load_spec_file(y)
    with pytest.raises(SpecValidationError, match="cannot read"):
        load_spec_file(tmp_path / "missing.yaml")


# ---- cross-checks with configs ----
def test_cost_reference(raw: dict[str, Any], configs_dir: Path) -> None:
    book = load_charge_book(configs_dir / "costs" / "nse_fo_index_options.toml")
    plans = load_brokerage_plans(configs_dir / "costs" / "brokerage_plans.toml")
    check_cost_reference(load_authored_spec(raw), book, plans)
    raw["cost_assumptions"]["cost_model_version"] = "CM-2019-01-01"
    with pytest.raises(SpecValidationError, match="UNKNOWN_COST_MODEL"):
        check_cost_reference(load_authored_spec(raw), book, plans)
    raw["cost_assumptions"] = {"cost_model_version": "CM-2026-04-01", "brokerage_plan": "nope"}
    with pytest.raises(SpecValidationError, match="UNKNOWN_BROKERAGE_PLAN"):
        check_cost_reference(load_authored_spec(raw), book, plans)


def test_window_reference(raw: dict[str, Any], configs_dir: Path) -> None:
    window = load_trading_windows(configs_dir / "sessions" / "trading_window.toml")
    check_against_window(load_authored_spec(raw), window)
    raw["exit"]["time_exit"] = "14:55:00"  # allowed by the schema (<= 15:00) but after flatten_start 14:50
    with pytest.raises(SpecValidationError, match="flatten_start"):
        check_against_window(load_authored_spec(raw), window)
    raw["exit"]["time_exit"] = "14:30:00"
    raw["entry"]["window_start"] = "09:15:00"  # before entry_start 09:20
    with pytest.raises(SpecValidationError, match="entry_start"):
        check_against_window(load_authored_spec(raw), window)


# ---- JSON schema export ----
def test_committed_json_schema_matches_models() -> None:
    expected = json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA_FILE.read_text() == expected, "run scripts/export_spec_schema.py to regenerate"


def test_schema_forbids_sell_leg() -> None:
    leg = json_schema()["$defs"]["Leg"]
    assert leg["properties"]["side"] == {"const": "BUY", "title": "Side", "type": "string"}
    assert leg["additionalProperties"] is False

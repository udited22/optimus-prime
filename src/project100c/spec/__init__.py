"""StrategySpec schema (docs/architecture/strategyspec.md)."""

from project100c.spec.checks import check_against_window, check_cost_reference
from project100c.spec.io import load_authored_spec, load_spec, load_spec_file, parse_yaml, write_json_schema
from project100c.spec.models import (
    ALLOWED_TRANSITIONS,
    ConfidenceLevel,
    EvidenceRef,
    Lifecycle,
    Regime,
    StrategySpec,
    check_transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ConfidenceLevel",
    "EvidenceRef",
    "Lifecycle",
    "Regime",
    "StrategySpec",
    "check_against_window",
    "check_cost_reference",
    "check_transition",
    "load_authored_spec",
    "load_spec",
    "load_spec_file",
    "parse_yaml",
    "write_json_schema",
]

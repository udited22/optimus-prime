"""Validation toolkit (S-02): the docs/research/validation.md V1-V18 gates over a strategy's trades. Seeded and
reproducible."""

from project100c.validation.gates import (
    GateConfig,
    GateResult,
    GateStatus,
    ValidationInputs,
    ValidationReport,
    Verdict,
    load_gate_config,
    validate,
)
from project100c.validation.trades import (
    RECENT_ERA,
    Era,
    Trade,
    era_of,
    paired_trades_from_library_run,
    trades_from_library_run,
)

__all__ = [
    "RECENT_ERA",
    "Era",
    "GateConfig",
    "GateResult",
    "GateStatus",
    "Trade",
    "ValidationInputs",
    "ValidationReport",
    "Verdict",
    "era_of",
    "load_gate_config",
    "paired_trades_from_library_run",
    "trades_from_library_run",
    "validate",
]

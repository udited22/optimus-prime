"""Cross-checks of a spec against loaded configs (cost model, trading window)."""

from __future__ import annotations

from project100c.costs.config import BrokeragePlan, ChargeBook
from project100c.errors import SpecValidationError
from project100c.sessions.model import TradingWindow
from project100c.spec.models import StrategySpec


def check_cost_reference(spec: StrategySpec, book: ChargeBook, plans: dict[str, BrokeragePlan]) -> None:
    versions = {s.version for s in book.schedule}
    cm = spec.cost_assumptions.cost_model_version
    if cm not in versions:
        raise SpecValidationError(f"UNKNOWN_COST_MODEL: {cm} not in {sorted(versions)}")
    if spec.cost_assumptions.brokerage_plan not in plans:
        raise SpecValidationError(f"UNKNOWN_BROKERAGE_PLAN: {spec.cost_assumptions.brokerage_plan}")


def check_against_window(spec: StrategySpec, window: TradingWindow) -> None:
    """The configured window may be tighter than the hard-coded OD limits; the spec must fit inside it."""
    e, x = spec.entry, spec.exit
    if e.window_start < window.entry_start:
        raise SpecValidationError(
            f"entry window start {e.window_start} before {window.version} entry_start {window.entry_start}"
        )
    if e.window_end > window.entry_cutoff:
        raise SpecValidationError(
            f"ENTRY_AFTER_CUTOFF: {e.window_end} > {window.version} entry_cutoff {window.entry_cutoff}"
        )
    if x.time_exit > window.flatten_start:
        raise SpecValidationError(
            f"time_exit {x.time_exit} after {window.version} flatten_start {window.flatten_start}"
        )

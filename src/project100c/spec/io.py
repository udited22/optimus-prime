"""Loading StrategySpecs from YAML/JSON/dicts, converting every failure to SpecValidationError.

Two entry points:
- load_authored_spec: for specs written by research agents/humans. Evidence, confidence and min_capital must be
  untouched ('pending'/NONE) and status must be RESEARCH (docs/architecture/strategyspec.md §7.2 rule 1).
- load_spec: for pipeline-managed specs (evidence may be present; lifecycle/evidence consistency still enforced).
  Signature verification of pipeline-written evidence is NOT implemented yet (backlog R-S1 follow-up).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from project100c.errors import SpecValidationError
from project100c.spec.models import PENDING, ConfidenceLevel, Lifecycle, StrategySpec


class _DecimalSafeLoader(yaml.SafeLoader):
    """SafeLoader that turns YAML floats into Decimal from their source text (no binary rounding)."""


def _decimal_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> Decimal:
    assert isinstance(node, yaml.ScalarNode)
    return Decimal(str(loader.construct_scalar(node)).replace("_", ""))


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _decimal_constructor)


def parse_yaml(text: str) -> dict[str, Any]:
    try:
        data = yaml.load(text, Loader=_DecimalSafeLoader)
    except yaml.YAMLError as e:
        raise SpecValidationError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise SpecValidationError("spec must be a mapping at top level")
    return data


def load_spec(data: dict[str, Any]) -> StrategySpec:
    try:
        return StrategySpec.model_validate(data)
    except ValidationError as e:
        raise SpecValidationError(f"invalid StrategySpec: {e}") from e


def load_authored_spec(data: dict[str, Any]) -> StrategySpec:
    spec = load_spec(data)
    problems: list[str] = []
    if spec.status is not Lifecycle.RESEARCH:
        problems.append(f"status {spec.status} (authored specs start at RESEARCH)")
    if not spec.evidence.is_pending():
        problems.append("evidence fields set")
    if spec.confidence.level is not ConfidenceLevel.NONE:
        problems.append("confidence set")
    if spec.dependencies.min_capital_inr != PENDING:
        problems.append("dependencies.min_capital_inr set")
    if problems:
        raise SpecValidationError(
            "EVIDENCE_HAND_SET: authored specs may not set pipeline-owned fields: " + "; ".join(problems)
        )
    return spec


def load_spec_file(path: Path, *, authored: bool = True) -> StrategySpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SpecValidationError(f"cannot read spec {path}: {e}") from e
    if path.suffix in {".yaml", ".yml"}:
        data = parse_yaml(text)
    elif path.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SpecValidationError(f"invalid JSON in {path}: {e}") from e
        if not isinstance(data, dict):
            raise SpecValidationError("spec must be an object at top level")
    else:
        raise SpecValidationError(f"unsupported spec file type {path.suffix!r}")
    return load_authored_spec(data) if authored else load_spec(data)


def json_schema() -> dict[str, Any]:
    return StrategySpec.model_json_schema()


def write_json_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

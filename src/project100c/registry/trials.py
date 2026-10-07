"""Registering real-data runs as trials (docs/research/validation.md): every strategy, parameter and regime
combination looked at on real data is one registered run, so V9's Deflated Sharpe sees the honest trial count."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from project100c.registry.ledger import ExperimentRegistry, RunPurpose, RunRegistration, RunResult, RunStatus

#: the shared registry of real-data trials (gitignored with the lake, on the box)
DEFAULT_REGISTRY = Path("lake/registry/experiments.sqlite")


def code_sha(repo: Path) -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


def digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def open_registry(path: Path = DEFAULT_REGISTRY) -> ExperimentRegistry:
    path.parent.mkdir(parents=True, exist_ok=True)
    return ExperimentRegistry(path)


def record_trial(
    reg: ExperimentRegistry,
    *,
    strategy_id: str,
    strategy_version: str,
    spec_hash: str,
    code: str,
    data_hash: str,
    cost_version: str,
    purpose: RunPurpose,
    params: Mapping[str, object],
    ledger_hash: str,
    n_trades: int,
    metrics: Mapping[str, object],
    registered_by: str,
    notes: str = "",
) -> str:
    """Register one completed run and its result (params and metrics are stored as strings; no floats)."""
    sp = {k: str(v) for k, v in params.items()}
    run_id = reg.register_run(
        RunRegistration(
            strategy_id, strategy_version, spec_hash, code, data_hash, cost_version, purpose, sp, 0, registered_by
        )
    )
    reg.record_result(run_id, RunResult(RunStatus.COMPLETED, ledger_hash, n_trades,
                                        {k: str(v) for k, v in metrics.items()}, notes))  # fmt: skip
    return run_id

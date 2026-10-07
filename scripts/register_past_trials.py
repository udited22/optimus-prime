#!/usr/bin/env python3
"""Register the real-data runs made before trial accounting existed (docs/research/validation.md).

Every H01/H01b pipeline-check directory under ``lake/runs`` (``h01*``, ``costs/h01b*``, ``od016/h01b*``) becomes one
EXPLORATION trial in the shared experiment registry, with its segments' ledger hashes and trade counts. Idempotent:
a directory already registered (``params.source``) is skipped.

  scripts/register_past_trials.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from project100c.registry import RunPurpose  # noqa: E402
from project100c.registry.trials import DEFAULT_REGISTRY, digest, open_registry, record_trial  # noqa: E402

RUNS = Path("lake/runs")


def main() -> int:
    dirs = sorted([*RUNS.glob("h01*"), *RUNS.glob("costs/h01*"), *RUNS.glob("od016/h01*")])
    done = 0
    with open_registry(DEFAULT_REGISTRY) as reg:
        seen = {str(r["params"].get("source")) for r in reg._registrations()}
        for d in dirs:
            src = str(d.relative_to(RUNS))
            if src in seen or not (d / "segments.json").exists():
                continue
            segs = json.loads((d / "segments.json").read_text())
            sid = "S-ORB-002" if d.name.startswith("h01b") else "S-ORB-001"
            record_trial(
                reg, strategy_id=sid, strategy_version="0.1.0", spec_hash="pipeline-check", code="pre-registry",
                data_hash=digest([s.get("data_fingerprints") for s in segs]), cost_version="as-run",
                purpose=RunPurpose.EXPLORATION, params={"source": src, "nav": d.name.rsplit("nav", 1)[-1]},
                ledger_hash=digest([s.get("ledger_hash") for s in segs]),
                n_trades=sum(len(s.get("closed") or []) for s in segs),
                metrics={"net_pnl": sum(float(s.get("net_pnl") or 0) for s in segs)},
                registered_by="scripts/register_past_trials.py",
                notes="H01/H01b real-data pipeline check run before trial accounting (registered 3-Oct-2026)",
            )  # fmt: skip
            done += 1
        print(json.dumps({"registered": done, "total_trials": reg.total_trials()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

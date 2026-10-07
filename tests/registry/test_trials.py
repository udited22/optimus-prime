"""Real-data trial registration (docs/research/validation.md)."""

from __future__ import annotations

from pathlib import Path

from project100c.registry import RunPurpose
from project100c.registry.trials import digest, open_registry, record_trial


def test_trials_are_counted_and_holdout_is_single_look(tmp_path: Path) -> None:
    with open_registry(tmp_path / "sub" / "r.sqlite") as reg:
        kw = dict(spec_hash="s", code="c", data_hash=digest([1, 2]), cost_version="CM", params={"cell": "UP|NORMAL"},
                  ledger_hash="h", n_trades=3, metrics={"mean": 1.5}, registered_by="test")  # fmt: skip
        for v in ("0.1.0", "0.1.0", "0.2.0"):
            record_trial(reg, strategy_id="S-X-001", strategy_version=v, purpose=RunPurpose.WALK_FORWARD, **kw)  # type: ignore[arg-type]
        record_trial(reg, strategy_id="REGIME-RC", strategy_version="1", purpose=RunPurpose.IN_SAMPLE, **kw)  # type: ignore[arg-type]
        assert reg.trial_count("S-X-001") == 3 and reg.total_trials() == 4 and reg.total_trials(prefix="S-") == 3
        assert not reg.holdout_used("S-X-001", "0.2.0")
        record_trial(reg, strategy_id="S-X-001", strategy_version="0.2.0", purpose=RunPurpose.HOLDOUT, **kw)  # type: ignore[arg-type]
        assert reg.holdout_used("S-X-001", "0.2.0")
        assert reg.verify_chain().events == 10

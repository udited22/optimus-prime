"""Experiment registry: append-only, hash-chained, one result per run, single holdout look (B-03,
docs/research/validation.md)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from project100c.errors import RegistryError
from project100c.registry import ExperimentRegistry, RunPurpose, RunRegistration, RunResult, RunStatus


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


REG = RunRegistration(
    strategy_id="S-ORB-001",
    strategy_version="0.1.0",
    spec_hash="a" * 64,
    code_sha="deadbeef",
    data_manifest_hash="b" * 64,
    cost_model_version="CM-2026-04-01",
    purpose=RunPurpose.IN_SAMPLE,
    params={"theta_v": "1.5"},
    seed=7,
    registered_by="research.momentum",
)
OK = RunResult(status=RunStatus.COMPLETED, ledger_hash="c" * 64, n_trades=42, metrics={"expectancy_inr": "-12.40"})


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "registry.sqlite"


@pytest.fixture
def reg(db: Path) -> Iterator[ExperimentRegistry]:
    with ExperimentRegistry(db, clock=FakeClock()) as r:
        yield r


def test_register_and_record(reg: ExperimentRegistry) -> None:
    rid = reg.register_run(REG)
    reg.record_result(rid, OK)
    registration, result = reg.get_run(rid)
    assert registration["params"] == {"theta_v": "1.5"} and registration["purpose"] == "IN_SAMPLE"
    assert result is not None and result["ledger_hash"] == "c" * 64
    report = reg.verify_chain()
    assert report.events == 2 and len(report.head_hash) == 64


def test_result_is_immutable_and_run_must_exist(reg: ExperimentRegistry) -> None:
    rid = reg.register_run(REG)
    reg.record_result(rid, OK)
    with pytest.raises(RegistryError, match="RESULT_ALREADY_RECORDED"):
        reg.record_result(rid, dataclasses.replace(OK, metrics={"expectancy_inr": "99"}))
    with pytest.raises(RegistryError, match="UNKNOWN_RUN"):
        reg.record_result("nope", OK)
    with pytest.raises(RegistryError, match="UNKNOWN_RUN"):
        reg.get_run("nope")


def test_trial_count_includes_failed_and_unfinished_runs(reg: ExperimentRegistry) -> None:
    a = reg.register_run(REG)
    b = reg.register_run(dataclasses.replace(REG, params={"theta_v": "1.8"}))
    reg.register_run(dataclasses.replace(REG, strategy_version="0.2.0"))
    reg.register_run(dataclasses.replace(REG, strategy_id="S-VOLX-001"))
    reg.record_result(a, OK)
    reg.record_result(b, RunResult(status=RunStatus.FAILED, ledger_hash=None, n_trades=0, metrics={}, notes="oom"))
    assert reg.trial_count("S-ORB-001") == 3
    assert reg.trial_count("S-VOLX-001") == 1


def test_holdout_once_per_version(reg: ExperimentRegistry) -> None:
    h = dataclasses.replace(REG, purpose=RunPurpose.HOLDOUT, registered_by="validation")
    reg.register_run(h)
    with pytest.raises(RegistryError, match="HOLDOUT_ALREADY_USED"):
        reg.register_run(h)
    reg.register_run(dataclasses.replace(h, strategy_version="0.2.0"))  # new version = new trial, allowed
    assert reg.holdout_used("S-ORB-001", "0.2.0")


def test_forward_paper_pre_registration_once_per_version(reg: ExperimentRegistry) -> None:
    f = dataclasses.replace(REG, purpose=RunPurpose.FORWARD_PAPER, registered_by="forward")
    run_id = reg.register_run(f)
    with pytest.raises(RegistryError, match="FORWARD_ALREADY_REGISTERED"):
        reg.register_run(f)
    reg.register_run(dataclasses.replace(f, strategy_version="0.2.0"))  # a new definition = a new trial
    reg.register_run(dataclasses.replace(f, purpose=RunPurpose.EXPLORATION))  # other purposes are unaffected
    registered, result = reg.get_run(run_id)
    assert registered["purpose"] == "FORWARD_PAPER" and result is None  # pre-registered: no result yet
    assert not reg.holdout_used(f.strategy_id, f.strategy_version)


def test_no_delete_or_update_api(reg: ExperimentRegistry) -> None:
    public = {n for n in dir(reg) if not n.startswith("_")}
    assert not any(w in n for n in public for w in ("delete", "remove", "update", "edit", "purge"))


def test_sql_update_and_delete_blocked_by_triggers(reg: ExperimentRegistry, db: Path) -> None:
    reg.register_run(REG)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET payload = '{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()
    assert reg.verify_chain().events == 1


def _tamper(db: Path, sql: str) -> None:
    conn = sqlite3.connect(db)
    conn.executescript("DROP TRIGGER events_no_update; DROP TRIGGER events_no_delete;" + sql)
    conn.commit()
    conn.close()


def _hash(db: Path, seq: int) -> str:
    conn = sqlite3.connect(db)
    try:
        return str(conn.execute("SELECT hash FROM events WHERE seq = ?", (seq,)).fetchone()[0])
    finally:
        conn.close()


def test_edit_bypassing_triggers_detected(reg: ExperimentRegistry, db: Path) -> None:
    rid = reg.register_run(REG)
    reg.record_result(rid, OK)
    _tamper(db, "UPDATE events SET payload = replace(payload, '-12.40', '12.40') WHERE seq = 2;")
    with pytest.raises(RegistryError, match="content hash mismatch at seq 2"):
        reg.verify_chain()


def test_deleted_row_detected(reg: ExperimentRegistry, db: Path) -> None:
    for _ in range(3):
        reg.register_run(REG)
    _tamper(db, "DELETE FROM events WHERE seq = 2;")
    with pytest.raises(RegistryError, match="expected seq 2"):
        reg.verify_chain()


def test_rehashed_row_still_breaks_chain(reg: ExperimentRegistry, db: Path) -> None:
    """An attacker who rewrites a row AND its own hash still breaks the next row's prev_hash link."""
    for _ in range(2):
        reg.register_run(REG)
    before = _hash(db, 1)
    # flip the first hex character to a guaranteed-different one (replacing it with 'f' was a no-op 1 time in 16)
    _tamper(db, "UPDATE events SET hash = (CASE WHEN substr(hash, 1, 1) = 'f' THEN '0' ELSE 'f' END) "
                "|| substr(hash, 2) WHERE seq = 1;")  # fmt: skip
    assert _hash(db, 1) != before
    with pytest.raises(RegistryError, match="CHAIN_BROKEN"):
        reg.verify_chain()


def test_chain_persists_across_reopen(db: Path) -> None:
    clock = FakeClock()
    with ExperimentRegistry(db, clock=clock) as r:
        r.register_run(REG)
    with ExperimentRegistry(db, clock=clock) as r:
        r.register_run(REG)
        assert r.verify_chain().events == 2


def test_input_validation(db: Path) -> None:
    with pytest.raises(RegistryError, match="empty"):
        dataclasses.replace(REG, code_sha="")
    with pytest.raises(RegistryError, match="no floats"):
        dataclasses.replace(REG, params={"theta_v": 1.5})  # type: ignore[dict-item]
    with pytest.raises(RegistryError, match="ledger hash"):
        RunResult(status=RunStatus.COMPLETED, ledger_hash=None, n_trades=1, metrics={})
    with ExperimentRegistry(db, clock=lambda: datetime(2026, 9, 30, 18, 0)) as r:
        with pytest.raises(RegistryError, match="naive"):
            r.register_run(REG)

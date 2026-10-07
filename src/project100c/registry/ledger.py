"""Experiment registry skeleton (backlog B-03): append-only, hash-chained, SQLite.

- Every run (including failed and abandoned ones) is registered BEFORE it executes, so the trial count used for
  multiple-testing adjustment (docs/research/validation.md) cannot be understated.
- Append-only twice over: SQLite triggers abort any UPDATE/DELETE, and each row carries
  sha256(prev_hash || canonical row) so edits made by bypassing the triggers are detected by verify_chain().
- There is no delete or update API. A run gets at most one result.
- Holdout (docs/research/validation.md): each strategy version may be evaluated on the holdout once.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from project100c.errors import RegistryError

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY,
    ts_utc    TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('RUN_REGISTERED', 'RUN_RESULT')),
    run_id    TEXT NOT NULL,
    payload   TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'experiment registry is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'experiment registry is append-only'); END;
"""


class RunPurpose(StrEnum):
    EXPLORATION = "EXPLORATION"
    IN_SAMPLE = "IN_SAMPLE"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    WALK_FORWARD = "WALK_FORWARD"
    STRESS = "STRESS"
    HOLDOUT = "HOLDOUT"
    FORWARD_PAPER = "FORWARD_PAPER"  # pre-registered paper forward test: registered before any forward data exists


class RunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class RunRegistration:
    strategy_id: str
    strategy_version: str
    spec_hash: str
    code_sha: str
    data_manifest_hash: str
    cost_model_version: str
    purpose: RunPurpose
    params: dict[str, str]  # Decimal values as strings; no floats
    seed: int
    registered_by: str

    def __post_init__(self) -> None:
        for name in (
            "strategy_id",
            "strategy_version",
            "spec_hash",
            "code_sha",
            "data_manifest_hash",
            "cost_model_version",
            "registered_by",
        ):
            if not getattr(self, name):
                raise RegistryError(f"RunRegistration.{name} is empty")
        for k, v in self.params.items():
            if not isinstance(v, str):
                raise RegistryError(f"param {k} must be a string (got {type(v).__name__}); no floats in the registry")


@dataclass(frozen=True, slots=True)
class RunResult:
    status: RunStatus
    ledger_hash: str | None  # None only for FAILED/ABORTED runs
    n_trades: int
    metrics: dict[str, str]
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status is RunStatus.COMPLETED and not self.ledger_hash:
            raise RegistryError("a COMPLETED run must record its ledger hash (reproducibility)")
        if self.n_trades < 0:
            raise RegistryError("n_trades must be >= 0")
        for k, v in self.metrics.items():
            if not isinstance(v, str):
                raise RegistryError(f"metric {k} must be a string (got {type(v).__name__})")


@dataclass(frozen=True, slots=True)
class ChainReport:
    events: int
    head_hash: str


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _row_hash(prev_hash: str, seq: int, ts_utc: str, kind: str, run_id: str, payload: str) -> str:
    material = _canonical([prev_hash, seq, ts_utc, kind, run_id, payload])
    return hashlib.sha256(material.encode()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ExperimentRegistry:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._path = path
        self._clock = clock
        try:
            self._conn = sqlite3.connect(path, isolation_level=None)
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as e:
            raise RegistryError(f"cannot open registry {path}: {e}") from e

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ExperimentRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- writes (append only) ----
    def _append(self, kind: str, run_id: str, payload: dict[str, Any]) -> None:
        now = self._clock()
        if now.tzinfo is None:
            raise RegistryError("registry clock returned a naive datetime")
        ts = now.astimezone(UTC).isoformat()
        body = _canonical(payload)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (1, GENESIS) if row is None else (int(row[0]) + 1, str(row[1]))
            h = _row_hash(prev, seq, ts, kind, run_id, body)
            self._conn.execute(
                "INSERT INTO events(seq, ts_utc, kind, run_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (seq, ts, kind, run_id, body, prev, h),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error as e:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise RegistryError(f"append failed: {e}") from e

    def register_run(self, reg: RunRegistration) -> str:
        if reg.purpose is RunPurpose.HOLDOUT and self.holdout_used(reg.strategy_id, reg.strategy_version):
            raise RegistryError(
                f"HOLDOUT_ALREADY_USED: {reg.strategy_id} {reg.strategy_version} has had its single holdout "
                "evaluation; a second look needs a new version (docs/research/validation.md)"
            )
        if reg.purpose is RunPurpose.FORWARD_PAPER and any(
            r["strategy_id"] == reg.strategy_id
            and r["strategy_version"] == reg.strategy_version
            and r["purpose"] == RunPurpose.FORWARD_PAPER.value
            for r in self._registrations()
        ):
            raise RegistryError(
                f"FORWARD_ALREADY_REGISTERED: {reg.strategy_id} {reg.strategy_version} already has its pre-registered "
                "forward test; a changed definition needs a new version"
            )
        run_id = str(uuid.uuid4())
        payload = {
            "strategy_id": reg.strategy_id,
            "strategy_version": reg.strategy_version,
            "spec_hash": reg.spec_hash,
            "code_sha": reg.code_sha,
            "data_manifest_hash": reg.data_manifest_hash,
            "cost_model_version": reg.cost_model_version,
            "purpose": reg.purpose.value,
            "params": dict(reg.params),
            "seed": reg.seed,
            "registered_by": reg.registered_by,
        }
        self._append("RUN_REGISTERED", run_id, payload)
        return run_id

    def record_result(self, run_id: str, result: RunResult) -> None:
        kinds = [k for (k,) in self._conn.execute("SELECT kind FROM events WHERE run_id = ?", (run_id,))]
        if "RUN_REGISTERED" not in kinds:
            raise RegistryError(f"UNKNOWN_RUN: {run_id}")
        if "RUN_RESULT" in kinds:
            raise RegistryError(f"RESULT_ALREADY_RECORDED: {run_id} (results are immutable)")
        payload = {
            "status": result.status.value,
            "ledger_hash": result.ledger_hash,
            "n_trades": result.n_trades,
            "metrics": dict(result.metrics),
            "notes": result.notes,
        }
        self._append("RUN_RESULT", run_id, payload)

    # ---- reads ----
    def _registrations(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT run_id, payload FROM events WHERE kind='RUN_REGISTERED' ORDER BY seq")
        out: list[dict[str, Any]] = []
        for run_id, payload in rows:
            d = json.loads(payload)
            d["run_id"] = run_id
            out.append(d)
        return out

    def holdout_used(self, strategy_id: str, strategy_version: str) -> bool:
        return any(
            r["strategy_id"] == strategy_id
            and r["strategy_version"] == strategy_version
            and r["purpose"] == RunPurpose.HOLDOUT.value
            for r in self._registrations()
        )

    def trial_count(self, strategy_id: str) -> int:
        """All registered runs for a strategy across versions, including failed/aborted/unfinished ones."""
        return sum(1 for r in self._registrations() if r["strategy_id"] == strategy_id)

    def total_trials(self, *, prefix: str = "") -> int:
        """Every registered run whose strategy id starts with ``prefix`` (all of them by default): the V9 count."""
        return sum(1 for r in self._registrations() if str(r["strategy_id"]).startswith(prefix))

    def get_run(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        rows = self._conn.execute(
            "SELECT kind, payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        reg = next((json.loads(p) for k, p in rows if k == "RUN_REGISTERED"), None)
        if reg is None:
            raise RegistryError(f"UNKNOWN_RUN: {run_id}")
        res = next((json.loads(p) for k, p in rows if k == "RUN_RESULT"), None)
        return reg, res

    def verify_chain(self) -> ChainReport:
        """Recompute every hash; raise RegistryError on any gap, reorder or edit."""
        prev = GENESIS
        expected_seq = 1
        n = 0
        for seq, ts, kind, run_id, payload, prev_hash, h in self._conn.execute(
            "SELECT seq, ts_utc, kind, run_id, payload, prev_hash, hash FROM events ORDER BY seq"
        ):
            if seq != expected_seq:
                raise RegistryError(f"CHAIN_BROKEN: expected seq {expected_seq}, found {seq}")
            if prev_hash != prev:
                raise RegistryError(f"CHAIN_BROKEN: prev_hash mismatch at seq {seq}")
            if _row_hash(prev_hash, seq, ts, kind, run_id, payload) != h:
                raise RegistryError(f"CHAIN_BROKEN: content hash mismatch at seq {seq} (row edited)")
            prev = h
            expected_seq += 1
            n += 1
        return ChainReport(events=n, head_hash=prev)

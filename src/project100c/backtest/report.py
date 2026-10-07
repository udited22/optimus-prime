"""Run artifacts (B-01/B-02): the hash-chained ledger as JSON lines, a per-component cost breakdown, and a
summary. The output is deterministic: identical runs produce byte-identical files. The ledger file can be
re-verified independently with ``verify_ledger_file``."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from project100c.backtest.engine import COST_COMPONENTS, RunResult
from project100c.backtest.ledger import GENESIS
from project100c.errors import BacktestError
from project100c.journal.codec import encode

REPORT_VERSION = "BT-REPORT-2026-10-01.1"


def _plain(v: Any) -> Any:
    """Readable JSON: Decimals as exact strings, datetimes as ISO-8601 with offset."""
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise BacktestError("naive datetime in report")
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, Mapping):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if v is None or isinstance(v, (str, int, bool)):
        return v
    raise BacktestError(f"cannot serialise {type(v).__name__} in report")


def _dump(obj: Any) -> bytes:
    return (json.dumps(_plain(obj), sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def cost_breakdown(result: RunResult) -> dict[str, Any]:
    per_fill = []
    for f in result.fills:
        if f.breakdown is None:  # pragma: no cover - the engine always records it
            raise BacktestError(f"fill {f.order_id} lacks a charge breakdown")
        per_fill.append(
            {
                "order_id": f.order_id,
                "ts": f.ts,
                "key": f.instrument_key,
                "side": f.side,
                "qty": f.qty,
                "price": f.price,
                "turnover": f.price * f.qty,
                **{k: getattr(f.breakdown, k) for k in COST_COMPONENTS[:-1]},
                "total": f.charges,
                "schedules": list(f.breakdown.schedule_versions),
                "uses_unverified": f.breakdown.uses_unverified,
                "assumed_half_spread": f.half_spread,
                "tag": f.tag,
            }
        )
    return {
        "report_version": REPORT_VERSION,
        "ledger_hash": result.ledger_hash,
        "totals": result.cost_breakdown(),
        "gross_pnl": result.gross_pnl,
        "net_pnl": result.net_pnl,
        "charges_use_unverified_rates": result.uses_unverified_costs,
        "fills": per_fill,
    }


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    ledger: Path
    costs: Path
    summary: Path


def write_run_artifacts(
    result: RunResult, out_dir: Path, *, strategy_report: Mapping[str, Any] | None = None
) -> RunArtifacts:
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / "ledger.jsonl"
    _write(ledger, "".join(encode(dict(e)) + "\n" for e in result.ledger.entries).encode())
    costs = out_dir / "cost_breakdown.json"
    _write(costs, _dump(cost_breakdown(result)))
    summary = out_dir / "summary.json"
    buys = sum(1 for f in result.fills if f.side.value == "BUY")
    _write(
        summary,
        _dump(
            {
                "report_version": REPORT_VERSION,
                "ledger_hash": result.ledger_hash,
                "ledger_entries": len(result.ledger.entries),
                "fills": len(result.fills),
                "entry_fills": buys,
                "rejects": result.rejects,
                "flat_at_end": result.flat_at_end,
                "starting_cash": result.starting_cash,
                "ending_cash": result.cash,
                "gross_pnl": result.gross_pnl,
                "total_charges": result.total_charges,
                "net_pnl": result.net_pnl,
                "metadata": result.metadata,
                "strategy": dict(strategy_report or {}),
            }
        ),
    )
    return RunArtifacts(ledger, costs, summary)


def verify_ledger_file(path: Path) -> str:
    """Recompute the hash chain from a ledger.jsonl file; returns the final hash."""
    h = GENESIS
    for i, line in enumerate(path.read_text().splitlines()):
        if json.loads(line).get("seq") != i:
            raise BacktestError(f"{path}: entry {i} out of sequence")
        h = hashlib.sha256((h + line).encode()).hexdigest()
    return h

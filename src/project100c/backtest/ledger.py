"""Append-only, hash-chained backtest ledger. Identical inputs -> identical ``ledger_hash`` (B-01 AT)."""

from __future__ import annotations

import hashlib
from typing import Any

from project100c.journal.codec import encode

GENESIS = "0" * 64


class Ledger:
    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._hash = GENESIS

    def append(self, kind: str, **fields: Any) -> str:
        entry = {"seq": len(self._entries), "kind": kind, **fields}
        text = encode(entry)  # lossless canonical JSON; rejects floats and naive datetimes
        self._hash = hashlib.sha256((self._hash + text).encode()).hexdigest()
        self._entries.append(entry)
        return self._hash

    @property
    def hash(self) -> str:
        return self._hash

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._entries)

    def kinds(self) -> list[str]:
        return [str(e["kind"]) for e in self._entries]

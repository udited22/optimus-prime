"""Canonical, lossless JSON encoding for journal payloads.

Allowed values: None, bool, int, str, Decimal, aware datetime, date, StrEnum members, list/tuple, dict[str, ...].
Floats are rejected (money and prices are Decimal). Decimal/datetime/date are tagged so they round-trip exactly.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from project100c.errors import JournalError

JsonValue = Any  # recursive JSON-compatible structure (validated at runtime by encode())


def _enc(v: Any, path: str) -> JsonValue:
    if v is None or isinstance(v, bool | str):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        raise JournalError(f"float at {path}: use Decimal")
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise JournalError(f"non-finite Decimal at {path}")
        return {"$dec": str(v)}
    if isinstance(v, datetime):
        if v.tzinfo is None or v.utcoffset() is None:
            raise JournalError(f"naive datetime at {path}")
        return {"$dt": v.isoformat()}
    if isinstance(v, date):
        return {"$date": v.isoformat()}
    if isinstance(v, Enum):
        return _enc(v.value, path)
    if isinstance(v, list | tuple):
        return [_enc(x, f"{path}[{i}]") for i, x in enumerate(v)]
    if isinstance(v, dict):
        out: dict[str, JsonValue] = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise JournalError(f"non-string key at {path}")
            if k.startswith("$"):
                raise JournalError(f"reserved key {k!r} at {path}")
            out[k] = _enc(x, f"{path}.{k}")
        return out
    raise JournalError(f"unsupported type {type(v).__name__} at {path}")


def encode(payload: dict[str, Any]) -> str:
    return json.dumps(_enc(payload, "$"), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _dec(v: JsonValue) -> Any:
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            ((k, x),) = v.items()
            if k == "$dec":
                return Decimal(x)
            if k == "$dt":
                return datetime.fromisoformat(x)
            if k == "$date":
                return date.fromisoformat(x)
        return {k: _dec(x) for k, x in v.items()}
    return v


def decode(text: str) -> dict[str, Any]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise JournalError(f"corrupt journal payload: {e}") from e
    out = _dec(raw)
    if not isinstance(out, dict):
        raise JournalError("journal payload is not an object")
    return out

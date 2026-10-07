"""Deterministic, idempotent client order IDs (docs/architecture/execution-engine.md §10.3).

The same intent, leg and attempt always give the same ID, so a retry after a timeout can never create a second
order: the gateway (and the broker, by its tag) recognises the ID. A new attempt number is the only way to get a new
order for the same intent. ``broker_tag`` maps any client order ID to a short alphanumeric tag for brokers whose
order tag is length-limited (the exact Upstox tag limit is UNVERIFIED; 20 characters is used to stay well inside).
"""

from __future__ import annotations

import base64
import hashlib
import re

_LEG = re.compile(r"^[A-Z0-9_]{1,12}$")
TAG_LEN = 20


def client_order_id(intent_id: str, leg: str, attempt: int) -> str:
    """``<intent_id>.<leg>.<attempt>`` -- readable, unique per (intent, leg, attempt), and stable across restarts."""
    if not intent_id or any(c in intent_id for c in ".\n\r\t "):
        raise ValueError(f"intent_id must be non-empty with no '.' or whitespace: {intent_id!r}")
    if not _LEG.match(leg):
        raise ValueError(f"leg must match {_LEG.pattern}: {leg!r}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 99:
        raise ValueError(f"attempt must be an int in 1..99: {attempt!r}")
    return f"{intent_id}.{leg}.{attempt}"


def broker_tag(client_order_id: str, length: int = TAG_LEN) -> str:
    """A deterministic alphanumeric tag (base32 of SHA-256) of ``length`` characters, prefixed P1 for this system."""
    if not client_order_id:
        raise ValueError("client_order_id is required")
    if not 8 <= length <= 40:
        raise ValueError("tag length must be in 8..40")
    digest = base64.b32encode(hashlib.sha256(client_order_id.encode()).digest()).decode().rstrip("=")
    return ("P1" + digest)[:length]

"""RiskTicket signatures: the gateway accepts an entry only with an untampered, unexpired ticket for that exact
order (K-07, docs/architecture/execution-engine.md §10.6).

The Governor signs each approved ticket with an HMAC-SHA256 key that lives only in the memory of the kernel
process (a new random key per process; never on disk, in the journal or in a log). The ``ExecutionGateway`` holds
the same ``TicketSigner`` and, before an ENTRY order leaves for the broker, checks that the ticket

* carries a valid signature over every field (so no field can be edited after approval),
* is valid at the gateway's own clock (approved_at <= now < expires_at),
* names the same instrument and side, the same quantity, and a limit price no higher than the approved one,
* is not a simulate-only ticket, and is used once.

This defends against a code path that builds an ``OrderRequest`` without going through the Governor, or changes
it afterwards. It is not a defence against an attacker inside the process, who could read the key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _rand
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from project100c.broker.types import OrderRequest
    from project100c.kernel.governor import RiskTicket


def _message(t: RiskTicket) -> bytes:
    parts = (
        "v1",
        t.ticket_id,
        t.intent_id,
        t.approved_at.isoformat(),
        t.expires_at.isoformat(),
        t.instrument_key,
        t.side,
        str(t.qty),
        "" if t.price_ceiling is None else str(t.price_ceiling),
        "" if t.risk_at_stop is None else str(t.risk_at_stop),
        "" if t.budget is None else str(t.budget),
        str(t.simulate_only),
        t.limits_version,
        t.window_version,
        str(t.is_exit),
    )
    return "\x1f".join(parts).encode()


class TicketSigner:
    """One per kernel process; pass the same object to the Governor and to the gateway."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes | None = None) -> None:
        if key is not None and len(key) < 32:
            raise ValueError("ticket key must be at least 32 bytes")
        self._key = key if key is not None else _rand.token_bytes(32)

    def __repr__(self) -> str:
        return "TicketSigner(<key hidden>)"

    def sign(self, t: RiskTicket) -> str:
        return hmac.new(self._key, _message(t), hashlib.sha256).hexdigest()

    def verify(self, t: RiskTicket) -> bool:
        return bool(t.signature) and hmac.compare_digest(t.signature, self.sign(t))


def ticket_mismatch(signer: TicketSigner, t: RiskTicket, req: OrderRequest, now: datetime) -> str:
    """Why this ticket does not authorise this order ("" if it does)."""
    if not signer.verify(t):
        return "signature invalid (ticket forged, edited or unsigned)"
    if t.simulate_only:
        return "simulate-only ticket"
    if t.is_exit:
        return "exit ticket used for an entry"
    if not t.valid_at(now):
        return f"ticket not valid at {now.isoformat()} (window {t.approved_at.isoformat()}..{t.expires_at.isoformat()})"
    if t.instrument_key != req.instrument_key:
        return f"instrument {req.instrument_key} != ticket {t.instrument_key}"
    if t.side != str(req.side):
        return f"side {req.side} != ticket {t.side}"
    if t.qty != req.qty:
        return f"qty {req.qty} != ticket {t.qty}"
    if t.price_ceiling is None or req.price > t.price_ceiling:
        return f"price {req.price} above the approved {t.price_ceiling}"
    return ""

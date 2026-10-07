"""Pure reconciliation of the journal's view against the broker's (docs/architecture/execution-engine.md §10.5). The
broker is the truth for
positions; any discrepancy is reported, and the caller latches POSITION_RECONCILIATION_KILL. No I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from project100c.broker.types import TERMINAL, BrokerOrder, BrokerPosition


class Discrepancy(StrEnum):
    POSITION_MISMATCH = "POSITION_MISMATCH"  # both sides long, different quantities
    UNEXPECTED_POSITION = "UNEXPECTED_POSITION"  # the broker holds a long the journal does not know
    MISSING_POSITION = "MISSING_POSITION"  # the journal holds a long the broker does not show
    BROKER_NET_SHORT = "BROKER_NET_SHORT"  # never allowed (long options only, OD-006)
    UNKNOWN_BROKER_ORDER = "UNKNOWN_BROKER_ORDER"  # a live broker order the journal never submitted
    MISSING_BROKER_ORDER = "MISSING_BROKER_ORDER"  # a journal-open order the broker does not have


@dataclass(frozen=True, slots=True)
class Finding:
    kind: Discrepancy
    key: str  # instrument key or client order id
    detail: str


@dataclass(frozen=True, slots=True)
class ReconReport:
    findings: tuple[Finding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        return "; ".join(f"{f.kind} {f.key}: {f.detail}" for f in self.findings) or "clean"


def reconcile(
    journal_positions: Mapping[str, int],
    journal_open_orders: Sequence[str],
    broker_positions: Sequence[BrokerPosition],
    broker_orders: Sequence[BrokerOrder],
    *,
    adoptable_tags: frozenset[str] = frozenset({"EXIT_ALL"}),
) -> ReconReport:
    """Compare net quantities per instrument and the set of live orders. Orders tagged in ``adoptable_tags`` (the
    broker's own Exit-All orders) are expected without a journal entry."""
    out: list[Finding] = []
    broker_net = {p.instrument_key: p.net_qty for p in broker_positions if p.net_qty != 0}
    ours = {k: q for k, q in journal_positions.items() if q != 0}
    for key in sorted(set(broker_net) | set(ours)):
        b, j = broker_net.get(key, 0), ours.get(key, 0)
        if b == j:
            continue
        if b < 0:
            out.append(Finding(Discrepancy.BROKER_NET_SHORT, key, f"broker {b}, journal {j}"))
        elif j == 0:
            out.append(Finding(Discrepancy.UNEXPECTED_POSITION, key, f"broker {b}, journal 0"))
        elif b == 0:
            out.append(Finding(Discrepancy.MISSING_POSITION, key, f"broker 0, journal {j}"))
        else:
            out.append(Finding(Discrepancy.POSITION_MISMATCH, key, f"broker {b}, journal {j}"))
    by_cid = {o.request.client_order_id: o for o in broker_orders}
    journal_set = set(journal_open_orders)
    for cid, o in sorted(by_cid.items()):
        if o.status not in TERMINAL and cid not in journal_set and o.request.tag not in adoptable_tags:
            out.append(Finding(Discrepancy.UNKNOWN_BROKER_ORDER, cid, f"{o.status} {o.request.instrument_key}"))
    for cid in sorted(journal_set - set(by_cid)):
        out.append(Finding(Discrepancy.MISSING_BROKER_ORDER, cid, "open in the journal, absent at the broker"))
    return ReconReport(tuple(out))

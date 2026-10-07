"""Execution: the order state machine, idempotent client order IDs, reconciliation and the gateway (K-05..K-07).

Venue-agnostic (OD-017): everything here speaks the ``broker.BrokerAdapter`` protocol; broker specifics live in
the adapters under ``broker/``.
"""

from project100c.execution.gateway import ExecutionGateway
from project100c.execution.ids import broker_tag, client_order_id
from project100c.execution.order_fsm import TERMINAL_STATES, TRANSITIONS, OrderEvt, OrderFSM, OrderState
from project100c.execution.reconcile import Discrepancy, Finding, ReconReport, reconcile
from project100c.execution.reconciler import ReconcilerService

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Discrepancy",
    "ExecutionGateway",
    "Finding",
    "OrderEvt",
    "OrderFSM",
    "OrderState",
    "ReconReport",
    "ReconcilerService",
    "broker_tag",
    "client_order_id",
    "reconcile",
]

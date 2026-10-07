"""Project 100C 'Jarvis' control-room dashboard (docs/architecture/observability.md observability; prototype).

READ-ONLY BY CONSTRUCTION. The HTTP server has no order endpoints. It can only (a) stream events and serve
replays, and (b) forward ONE owner control, MANUAL_MASTER_KILL, through a two-step arm/confirm to a narrow
kill-only port. Everything it shows in this prototype is SIMULATED: synthetic prices, the fake broker, and
simulated lifecycle stages.
"""

from project100c.observability.dashboard.events import SIMULATED_LABEL, DashEvent, EventBus, EventKind, Flow

__all__ = ["SIMULATED_LABEL", "DashEvent", "EventBus", "EventKind", "Flow"]

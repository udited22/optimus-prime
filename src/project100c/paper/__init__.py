"""Paper loop (P-01 groundwork): library strategies -> allocator -> Risk Governor -> kernel runtime -> fake broker,
on a SIMULATED feed. Everything it produces is SIMULATED; nothing in it is an edge."""

from project100c.paper.live import LivePaperSession, StepReport
from project100c.paper.loop import (
    PaperDay,
    PaperDecision,
    PaperKernel,
    PaperLoop,
    PaperRun,
    PaperTrade,
)
from project100c.paper.report import owner_report

__all__ = [
    "LivePaperSession",
    "PaperDay",
    "PaperDecision",
    "PaperKernel",
    "PaperLoop",
    "PaperRun",
    "PaperTrade",
    "StepReport",
    "owner_report",
]

"""The 'brain' graph: agents and kernel components (nodes) and the paths events travel (edges).

The frontend draws exactly this graph, and the simulator may only send a pulse along a declared edge (tested)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    label: str
    role: str  # agent | kernel | external | strategy
    blurb: str


@dataclass(frozen=True, slots=True)
class SimStrategy:
    """A strategy on the simulated roster. The stages are SIMULATED for display; every real spec is RESEARCH."""

    id: str
    hypothesis: str
    name: str
    stage: str

    @property
    def node(self) -> str:
        return f"strat:{self.id}"


STRATEGIES: tuple[SimStrategy, ...] = (
    SimStrategy("S-ORB-001", "H01", "Opening-range breakout", "CANARY"),
    SimStrategy("S-VWAPC-001", "H03", "VWAP pullback continuation", "SHADOW"),
    SimStrategy("S-FBO-001", "H02", "Failed breakout reversal", "PAPER"),
    SimStrategy("S-VOLX-001", "H05", "Volatility-compression breakout", "BACKTESTED"),
    SimStrategy("S-EXP0-001", "H07", "Expiry-day OTM momentum", "RESEARCH"),
)

NODES: tuple[Node, ...] = (
    Node(
        "risk_governor", "RISK GOVERNOR", "kernel", "Central gatekeeper: every TradeIntent is approved or rejected here"
    ),
    Node("cio", "TONY", "agent", "Trading CIO (orchestrator): sets the day's posture and budgets; explains itself"),
    Node("market_intel", "MARKET INTELLIGENCE", "agent", "Ticks in, regime tags out"),
    Node("data_quality", "DATA QUALITY", "agent", "DQ gate on every tick"),
    Node("strategy_factory", "STRATEGY FACTORY", "agent", "Researches and authors specs"),
    Node("validation", "VALIDATION", "agent", "Promotes or demotes lifecycle stages"),
    Node("allocator", "PORTFOLIO ALLOCATOR", "agent", "Sizes and routes intents (1 lot max)"),
    Node("execution", "EXECUTION GATEWAY", "kernel", "Only path to the broker; re-checks window and sell-to-close"),
    Node("broker", "BROKER", "external", "Fake broker (SIMULATED); no real broker is connected"),
    Node("post_trade", "POST-TRADE", "agent", "Fills, costs, slippage, attribution"),
    *(Node(s.node, s.id, "strategy", f"{s.hypothesis} {s.name} [{s.stage}]") for s in STRATEGIES),
)

_S = tuple(s.node for s in STRATEGIES)
EDGES: tuple[tuple[str, str], ...] = (
    ("broker", "market_intel"),
    ("market_intel", "data_quality"),
    ("market_intel", "cio"),
    ("data_quality", "risk_governor"),
    ("cio", "allocator"),
    ("cio", "strategy_factory"),
    ("strategy_factory", "validation"),
    ("allocator", "risk_governor"),
    ("risk_governor", "allocator"),
    ("risk_governor", "execution"),
    ("risk_governor", "cio"),
    ("execution", "broker"),
    ("broker", "execution"),
    ("execution", "post_trade"),
    ("post_trade", "cio"),
    ("post_trade", "validation"),
    *(("data_quality", s) for s in _S),
    *((s, "allocator") for s in _S),
    *(("validation", s) for s in _S),
    *(("strategy_factory", s) for s in _S),
)
EDGE_SET = frozenset(EDGES)
NODE_IDS = frozenset(n.id for n in NODES)


def topology_json() -> dict[str, Any]:
    return {
        "nodes": [{"id": n.id, "label": n.label, "role": n.role, "blurb": n.blurb} for n in NODES],
        "edges": [list(e) for e in EDGES],
        "strategies": [
            {"id": s.id, "hypothesis": s.hypothesis, "name": s.name, "stage": s.stage, "node": s.node}
            for s in STRATEGIES
        ],
    }

"""Paper mode on live quotes (P-02): one host loop step that wires the quote feed, the daily token gate, the
owner's Telegram commands, the strategies' intents and the kernel runtime over the gateway and `PaperBroker`.

Venue-agnostic: the quote source, command source and alert sink are protocols. The same step is what a live host
runs with a real broker in place of `PaperBroker`; only the composition root differs. Every fill is SIMULATED.

Fail closed: no new intent is submitted unless the token gate (when one is configured) allows trading. If the gate
closes with positions open (``/deny``), each position is asked to exit, repeated every `exit_retry` (the runtime
re-prices a working exit and the 14:50 forced flatten and 15:00 Exit-All still apply).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from project100c.broker.paper import PaperBroker
from project100c.kernel.governor import Decision, MarketSnapshot, TradeIntent
from project100c.kernel.runtime import AlertSink, KernelRuntime
from project100c.market_types import Quote
from project100c.ops.daily_gate import TokenGate


class QuoteSource(Protocol):
    def poll(self) -> Mapping[str, Quote]: ...


class Command(Protocol):
    @property
    def command(self) -> str: ...
    @property
    def arg(self) -> str: ...


class CommandSource(Protocol):
    def poll_commands(self) -> Sequence[Command]: ...


Decide = Callable[[datetime, Mapping[str, Quote]], list[tuple[TradeIntent, MarketSnapshot]]]


@dataclass(frozen=True, slots=True)
class StepReport:
    at: datetime
    trading_allowed: bool
    quotes: int
    decisions: tuple[Decision, ...]
    commands: tuple[str, ...]
    exits_requested: tuple[str, ...]


@dataclass
class LivePaperSession:
    runtime: KernelRuntime
    broker: PaperBroker
    quotes: QuoteSource
    decide: Decide
    clock: Callable[[], datetime]
    alerts: AlertSink
    gate: TokenGate | None = None
    commands: CommandSource | None = None
    exit_retry: timedelta = timedelta(seconds=30)
    _last_exit: dict[str, datetime] = field(default_factory=dict)

    def _allowed(self, now: datetime) -> bool:
        return self.gate is None or self.gate.trading_allowed(now)

    def _handle(self, c: Command, now: datetime) -> str:
        name = str(c.command)
        if name == "/kill":
            self.runtime.manual_master_kill("owner /kill on Telegram", requested_by="owner via Telegram")
            return name
        if name == "/deny" and self.gate is not None:
            self.gate.deny(c.arg, now)
            return name
        if name == "/status":
            st = self.runtime.state
            gate = self.gate.state if self.gate is not None else "none"
            self.alerts.send(
                "INFO",
                f"PAPER (SIMULATED). Gate {gate}; positions {len(st.positions)}; open orders {len(st.open_orders)}; "
                f"kills {len(st.kills)}; realised today {st.realised_today}.",
            )
            return name
        return f"ignored {name}"

    def step(self) -> StepReport:
        now = self.clock()
        if self.gate is not None:
            self.gate.tick(now)
        handled = tuple(self._handle(c, now) for c in (self.commands.poll_commands() if self.commands else []))
        q = dict(self.quotes.poll())
        self.broker.on_quotes(q)
        self.runtime.step(q)
        allowed = self._allowed(now)
        decisions: list[Decision] = []
        exits: list[str] = []
        if allowed:
            self._last_exit.clear()
            for intent, market in self.decide(now, q):
                decisions.append(self.runtime.submit(intent, market))
        else:
            for key in list(self.runtime.state.positions):
                last = self._last_exit.get(key)
                if last is None or now - last >= self.exit_retry:
                    if self.runtime.request_exit(key, "token gate closed: no trading today"):
                        exits.append(key)
                    self._last_exit[key] = now
        return StepReport(now, allowed, len(q), tuple(decisions), handled, tuple(exits))

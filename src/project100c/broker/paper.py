"""PaperBroker: the paper venue on live executable quotes (docs/research/paper-trading.md §14.1, P-02). Every fill is
SIMULATED.

It is the deterministic fake broker's matching engine fed from a live quote stream instead of a script, with the
docs/research/paper-trading.md conservatism:

* buys fill only at the ask and sells only at the bid, and a limit fills only when the quote reaches it;
* an order is acknowledged after `ack_latency` and cannot fill until `fill_latency` after that (measured broker
  latency goes here once K-B4 has it; the defaults are ASSUMED);
* only `depth_haircut` of the displayed size is available to us (queue position), and what we take is used up
  until the next quote;
* a quote older than `max_quote_age`, or with a missing size, gives no fill at all on that instrument.

It declares ``paper_venue = True`` so that a paper-venue Risk Governor may route PAPER intents to it; the kernel
refuses that Governor with any broker that does not. It runs behind the same `ExecutionGateway` and
`KernelRuntime` as a live venue.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from project100c.broker.fake import FakeBroker, FaultPlan, _Order
from project100c.broker.fake import Quote as _Book
from project100c.core_types import OrderSide
from project100c.errors import BrokerError
from project100c.market_types import Quote


class PaperBroker(FakeBroker):
    paper_venue = True

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        funds: Decimal,
        ack_latency: timedelta = timedelta(milliseconds=300),
        fill_latency: timedelta = timedelta(milliseconds=500),
        depth_haircut: Decimal = Decimal("0.5"),
        max_quote_age: timedelta = timedelta(seconds=3),
    ) -> None:
        if not Decimal(0) < depth_haircut <= Decimal(1):
            raise BrokerError("depth_haircut must be in (0, 1]")
        if ack_latency < timedelta(0) or fill_latency < timedelta(0) or max_quote_age <= timedelta(0):
            raise BrokerError("latencies must be >= 0 and max_quote_age > 0")
        super().__init__(clock, funds=funds, faults=FaultPlan(ack_delay=ack_latency, fill_delay=fill_latency))
        self.depth_haircut = depth_haircut
        self.max_quote_age = max_quote_age
        self.stale: set[str] = set()

    def _avail(self, qty: int | None) -> int:
        if qty is None or qty <= 0:
            return 0
        return int((Decimal(qty) * self.depth_haircut).to_integral_value(rounding=ROUND_FLOOR))

    def _match(self, o: _Order, q: _Book, now: datetime) -> None:
        """Fill as the fake broker does, then use up the depth we took: until the next quote arrives, a second
        order (or the rest of this one) cannot fill against the same displayed size again."""
        before = o.filled
        super()._match(o, q, now)
        got = o.filled - before
        if got > 0:
            key = o.req.instrument_key
            cur = self._quotes[key]
            if o.req.side is OrderSide.BUY:
                self._quotes[key] = replace(cur, ask_qty=max(0, cur.ask_qty - got))
            else:
                self._quotes[key] = replace(cur, bid_qty=max(0, cur.bid_qty - got))

    def on_quotes(self, quotes: Mapping[str, Quote]) -> None:
        """Feed one batch of live quotes. Instruments not in the batch keep their last book until it ages out."""
        now = self._clock()
        for key, q in quotes.items():
            age = now - q.receive_ts
            if age > self.max_quote_age or age < -self.max_quote_age:
                self.stale.add(key)
                self.set_quote(key, None, None, bid_qty=0, ask_qty=0)
                continue
            self.stale.discard(key)
            self.set_quote(key, q.bid, q.ask, bid_qty=self._avail(q.bid_qty), ask_qty=self._avail(q.ask_qty))
        self.tick()

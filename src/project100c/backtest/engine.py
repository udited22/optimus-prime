"""Event-driven backtest engine (B-01) sharing kernel code:

* the long-only mandate (``kernel.mandate.check_long_only``, OD-006) screens every order;
* the trading window (``calendar.MarketClock``: entries 09:20-14:00, forced flatten from 14:50, no order activity
  from 15:00; OD-002/OD-008/OD-009) gates orders, and the engine itself flattens residual longs;
* charges come from the dated ``costs.CostModel`` (D-05) on every fill;
* at most ``max_lots`` open + pending on the BUY side (the 1-lot canary rule).

Per event (in availability order): advance the clock -> match live orders against the new market data ->
make the event visible -> release reports whose latency has elapsed -> forced-flatten check -> strategy ->
validate and accept/reject its intents. Everything is appended to a hash-chained ledger, so a re-run with
the same inputs gives the same ``ledger_hash``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from project100c.backtest.feed import BarEvent, Event, MarketView, QuoteEvent, ReplayFeed
from project100c.backtest.fills import BarFillModel, LatencyModel, Match, QuoteFillModel
from project100c.backtest.ledger import Ledger
from project100c.backtest.types import (
    CancelOrder,
    Fill,
    Intent,
    OrderReport,
    OrderStatus,
    PlaceOrder,
    SimOrder,
    SimOrderType,
)
from project100c.calendar.market import MarketClock
from project100c.core_types import OrderSide
from project100c.costs.model import ChargeBreakdown, CostModel, Side
from project100c.errors import BacktestError
from project100c.kernel.mandate import MandateOrder, PositionSnapshot, check_long_only
from project100c.sessions.model import IST, WindowPhase

FLATTEN_TAG = "ENGINE_FLATTEN"
COST_COMPONENTS = ("brokerage", "stt", "exchange_txn", "sebi_fee", "stamp_duty", "gst", "total")


def _breakdown_dict(ch: ChargeBreakdown) -> dict[str, object]:
    return {k: getattr(ch, k) for k in COST_COMPONENTS[:-1]} | {"schedules": list(ch.schedule_versions)}


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    plan_id: str = "upstox-options"
    tick_size: Decimal = Decimal("0.05")
    max_lots: int = 1
    flatten_discount: Decimal = Decimal("0.10")  # flatten limit = last price x (1 - discount), floored to tick
    starting_cash: Decimal = Decimal("10000")


class StrategyContext:
    """The strategy's own view of its state: only reports whose latency has elapsed."""

    def __init__(self) -> None:
        self.now: datetime | None = None
        self.fills: list[Fill] = []
        self.reports: list[OrderReport] = []
        # synchronous placement acknowledgement (a broker returns the order id on placement): tag -> order id,
        # and tags the engine refused. Only filled for non-empty tags; tags should be unique per order.
        self.accepted: dict[str, str] = {}
        self.rejected: dict[str, list[str]] = {}
        self._pos: dict[str, int] = {}

    def position(self, key: str) -> int:
        return self._pos.get(key, 0)

    def _see(self, item: Fill | OrderReport) -> None:
        if isinstance(item, Fill):
            self.fills.append(item)
            sign = 1 if item.side is OrderSide.BUY else -1
            self._pos[item.instrument_key] = self._pos.get(item.instrument_key, 0) + sign * item.qty
        else:
            self.reports.append(item)


class Strategy(Protocol):
    name: str

    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]: ...


@dataclass(slots=True)
class RunResult:
    ledger_hash: str
    ledger: Ledger
    fills: tuple[Fill, ...]
    cash: Decimal
    total_charges: Decimal
    positions: dict[str, int]
    rejects: int
    flat_at_end: bool
    starting_cash: Decimal
    orders: dict[str, SimOrder] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def net_pnl(self) -> Decimal:
        return self.cash - self.starting_cash

    @property
    def gross_pnl(self) -> Decimal:
        """P&L before charges (cash flow of prices only)."""
        return self.net_pnl + self.total_charges

    def cost_breakdown(self) -> dict[str, Decimal]:
        """Charges by component over all fills (brokerage, STT, exchange txn, SEBI fee, stamp duty, GST, total)."""
        out = dict.fromkeys(COST_COMPONENTS, Decimal(0))
        for f in self.fills:
            if f.breakdown is None:  # pragma: no cover - the engine always records it
                raise BacktestError(f"fill {f.order_id} has no charge breakdown")
            for k in COST_COMPONENTS[:-1]:
                out[k] += getattr(f.breakdown, k)
            out["total"] += f.charges
        if out["total"] != self.total_charges:  # pragma: no cover - invariant
            raise BacktestError("charge components do not add up to the run total")
        return out

    @property
    def uses_unverified_costs(self) -> bool:
        return any(f.breakdown is not None and f.breakdown.uses_unverified for f in self.fills)


class BacktestEngine:
    def __init__(
        self,
        *,
        clock: MarketClock,
        costs: CostModel,
        bar_model: BarFillModel,
        quote_model: QuoteFillModel | None = None,
        latency: LatencyModel = LatencyModel(),  # noqa: B008 - immutable dataclass
        config: BacktestConfig = BacktestConfig(),  # noqa: B008
    ) -> None:
        self._mclock = clock
        self._costs = costs
        self._bar = bar_model
        self._quote = quote_model or QuoteFillModel()
        self._lat = latency
        self._cfg = config

    # ------------------------------------------------------------------ helpers
    def _on_tick(self, p: Decimal) -> bool:
        return p > 0 and p % self._cfg.tick_size == 0

    def _floor_tick(self, p: Decimal) -> Decimal:
        t = self._cfg.tick_size
        return max(t, (p / t).to_integral_value(rounding=ROUND_FLOOR) * t)

    def run(self, feed: ReplayFeed, strategy: Strategy, *, metadata: Mapping[str, object] | None = None) -> RunResult:
        """``metadata`` (data lineage fingerprint, assumptions, spec id) is written into the START entry and so is
        covered by the ledger hash."""
        led = Ledger()
        view = MarketView()
        ctx = StrategyContext()
        orders: dict[str, SimOrder] = {}
        active: list[SimOrder] = []  # live orders in id order (pruned lazily); keeps each event O(live orders)
        pos: dict[str, int] = {}
        pending: list[Fill | OrderReport] = []
        fills: list[Fill] = []
        cash = self._cfg.starting_cash
        charges_total = Decimal(0)
        last_px: dict[str, Decimal] = {}
        rejects = 0
        residual_logged: set[tuple[str, str]] = set()
        meta = dict(sorted((metadata or {}).items()))
        led.append(
            "START",
            strategy=strategy.name,
            config=_cfg_dict(self._cfg),
            fill_model=self._bar.describe(),
            events=len(feed),
            metadata=meta,
        )

        def report(o: SimOrder, status: OrderStatus, ts: datetime, reason: str = "") -> None:
            pending.append(OrderReport(o.order_id, status, o.filled_qty, ts, ts + self._lat.report_to_strategy, reason))

        def accept(spec: PlaceOrder, now: datetime, *, by_strategy: bool = False) -> SimOrder:
            oid = f"O{len(orders):06d}"
            o = SimOrder(oid, spec, now, now + self._lat.order_to_exchange)
            orders[oid] = o
            active.append(o)
            if by_strategy and spec.tag:
                ctx.accepted[spec.tag] = oid
            led.append(
                "ORDER_ACCEPTED",
                order_id=oid,
                ts=now,
                active_at=o.active_at,
                key=spec.instrument_key,
                side=spec.side,
                qty=spec.qty,
                type=spec.order_type,
                limit=spec.limit_price,
                trigger=spec.trigger_price,
                tag=spec.tag,
            )
            return o

        def cancel(o: SimOrder, now: datetime, why: str) -> None:
            if o.live and o.cancel_at is None:
                o.cancel_at = now + self._lat.order_to_exchange
                led.append("CANCEL_SENT", order_id=o.order_id, ts=now, effective_at=o.cancel_at, reason=why)

        def reject(spec: PlaceOrder, now: datetime, reasons: list[str]) -> None:
            nonlocal rejects
            rejects += 1
            led.append("ORDER_REJECTED", ts=now, key=spec.instrument_key, side=spec.side, qty=spec.qty, reasons=reasons)
            if spec.tag:
                ctx.rejected[spec.tag] = list(reasons)

        for ev in feed:
            if any(not o.live for o in active):
                active[:] = [o for o in active if o.live]
            now = ev.available_at
            view._advance(now)
            ctx.now = now
            # 1) match resting orders against the new market data (exchange side)
            for o in [o for o in active if o.live and o.spec.instrument_key == ev.key]:
                # conservative: a bar can only fill an order that was live for the WHOLE bar, so a cancel that takes
                # effect before the bar ends wins (intra-bar timing is unknown)
                if o.cancel_at is not None and (ev.exchange_start >= o.cancel_at or ev.exchange_end > o.cancel_at):
                    o.status = OrderStatus.CANCELLED
                    led.append("CANCELLED", order_id=o.order_id, ts=o.cancel_at, filled=o.filled_qty)
                    report(o, OrderStatus.CANCELLED, o.cancel_at)
                    continue
                m: Match | None = (
                    self._bar.match(o, ev.bar) if isinstance(ev, BarEvent) else self._quote.match(o, ev.quote)
                )
                if m is None:
                    continue
                if m.triggered and not o.triggered:
                    o.triggered = True
                    led.append("TRIGGERED", order_id=o.order_id, ts=m.ts)
                if m.qty <= 0:
                    continue
                side = Side.BUY if o.spec.side is OrderSide.BUY else Side.SELL
                ch = self._costs.order_charges(side, m.price, m.qty, m.ts.astimezone(IST).date(), self._cfg.plan_id)
                amt = ch.total
                f = Fill(
                    o.order_id,
                    o.spec.instrument_key,
                    o.spec.side,
                    m.qty,
                    m.price,
                    m.ts,
                    m.ts + self._lat.report_to_strategy,
                    amt,
                    o.spec.tag,
                    ch,
                    m.half_spread,
                )
                fills.append(f)
                pending.append(f)
                o.filled_qty += m.qty
                o.status = OrderStatus.FILLED if o.remaining == 0 else OrderStatus.PARTIAL
                sign = 1 if o.spec.side is OrderSide.BUY else -1
                pos[f.instrument_key] = pos.get(f.instrument_key, 0) + sign * m.qty
                if pos[f.instrument_key] < 0:  # pragma: no cover - mandate + qty caps make this unreachable
                    raise BacktestError(f"net short in {f.instrument_key}: engine invariant broken")
                cash += -sign * m.price * m.qty - amt
                charges_total += amt
                led.append(
                    "FILL",
                    order_id=o.order_id,
                    ts=m.ts,
                    key=f.instrument_key,
                    side=f.side,
                    qty=m.qty,
                    price=m.price,
                    charges=amt,
                    charge_parts=_breakdown_dict(ch),
                    uses_unverified=ch.uses_unverified,
                    half_spread=m.half_spread,
                    cash=cash,
                )
            # 2) the event becomes visible
            view._add(ev)
            if isinstance(ev, BarEvent):
                last_px[ev.key] = ev.bar.close
            elif ev.quote.ltp is not None:
                last_px[ev.key] = ev.quote.ltp
            # 3) release reports whose latency elapsed
            still: list[Fill | OrderReport] = []
            for item in pending:
                (ctx._see(item) if item.visible_at <= now else still.append(item))
            pending = still
            # 4) forced flatten (engine-owned, not optional for strategies)
            trading = self._mclock.is_trading_day(now)
            phase = self._mclock.phase(now) if trading else WindowPhase.CLOSED
            if phase in (WindowPhase.FLATTENING, WindowPhase.CLOSED):
                for key in sorted(k for k, q in pos.items() if q > 0):
                    if phase is WindowPhase.CLOSED:
                        tag = (key, now.astimezone(IST).date().isoformat())
                        if tag not in residual_logged:
                            residual_logged.add(tag)
                            led.append(
                                "RESIDUAL_AT_CLOSE",
                                ts=now,
                                key=key,
                                qty=pos[key],
                                note="order activity closed; live system would use broker Exit-All (OD-007)",
                            )
                        continue
                    px = last_px.get(key)
                    if px is None:
                        continue
                    target = self._floor_tick(px * (1 - self._cfg.flatten_discount))
                    live = [o for o in active if o.live and o.spec.instrument_key == key]
                    fl = [o for o in live if o.spec.tag == FLATTEN_TAG and o.cancel_at is None]
                    if fl and all(o.spec.limit_price <= target for o in fl):
                        continue
                    for o in live:
                        cancel(o, now, "forced flatten")
                    # a sell whose cancel is not yet effective can still fill: flatten only what no live sell
                    # covers, else wait for the cancels (the next event re-checks), so the book never goes short
                    covered = sum(o.remaining for o in live if o.spec.side is OrderSide.SELL)
                    qty = pos[key] - covered
                    if qty <= 0:
                        continue
                    spec0 = fl[0].spec if fl else next(o.spec for o in orders.values() if o.spec.instrument_key == key)
                    accept(
                        PlaceOrder(
                            key,
                            spec0.underlying,
                            spec0.right,
                            OrderSide.SELL,
                            qty,
                            spec0.lot_size,
                            SimOrderType.LIMIT,
                            target,
                            None,
                            FLATTEN_TAG,
                        ),
                        now,
                    )
            # 5) strategy
            intents = strategy.on_event(view, ctx)
            for it in intents:
                if isinstance(it, CancelOrder):
                    target_o = orders.get(it.order_id)
                    if target_o is None:
                        rejects += 1
                        led.append("CANCEL_REJECTED", ts=now, order_id=it.order_id, reason="unknown order")
                    else:
                        cancel(target_o, now, "strategy")
                    continue
                if not isinstance(it, PlaceOrder):
                    raise BacktestError(f"unknown intent {type(it).__name__}")
                reasons: list[str] = []
                if not trading or not self._mclock.order_activity_allowed(now):
                    reasons.append("OUTSIDE_ORDER_WINDOW")
                elif it.side is OrderSide.BUY and not self._mclock.entry_allowed(now):
                    reasons.append("ENTRY_WINDOW_CLOSED")
                if not self._on_tick(it.limit_price) or (
                    it.trigger_price is not None and not self._on_tick(it.trigger_price)
                ):
                    reasons.append("OFF_TICK")
                live_sells: dict[str, int] = {}
                for o in active:
                    if o.live and o.spec.side is OrderSide.SELL:
                        live_sells[o.spec.instrument_key] = live_sells.get(o.spec.instrument_key, 0) + o.remaining
                snap = PositionSnapshot(
                    open_qty={k: v for k, v in pos.items() if v > 0},
                    pending_sell_qty={k: min(v, pos.get(k, 0)) for k, v in live_sells.items()},
                )
                dec = check_long_only(
                    MandateOrder(it.instrument_key, it.underlying, it.right, it.side, it.qty, it.lot_size), snap
                )
                reasons += [r.value for r in dec.reasons]
                if it.side is OrderSide.SELL and live_sells.get(it.instrument_key, 0) + it.qty > pos.get(
                    it.instrument_key, 0
                ):
                    if "MANDATE_LONG_ONLY" not in reasons:
                        reasons.append("MANDATE_LONG_ONLY")
                if it.side is OrderSide.BUY:
                    pend_buy = sum(o.remaining for o in active if o.live and o.spec.side is OrderSide.BUY)
                    open_units = sum(pos.values())
                    if open_units + pend_buy + it.qty > self._cfg.max_lots * it.lot_size:
                        reasons.append("MAX_LOTS")
                if reasons:
                    reject(it, now, reasons)
                else:
                    accept(it, now, by_strategy=True)
        flat = all(q == 0 for q in pos.values())
        led.append("END", positions=dict(sorted(pos.items())), cash=cash, charges=charges_total, flat=flat)
        return RunResult(
            led.hash,
            led,
            tuple(fills),
            cash,
            charges_total,
            dict(pos),
            rejects,
            flat,
            self._cfg.starting_cash,
            orders,
            meta,
        )


def _cfg_dict(c: BacktestConfig) -> dict[str, object]:
    return {
        "plan_id": c.plan_id,
        "tick_size": c.tick_size,
        "max_lots": c.max_lots,
        "flatten_discount": c.flatten_discount,
        "starting_cash": c.starting_cash,
    }


__all__ = [
    "COST_COMPONENTS",
    "BacktestConfig",
    "BacktestEngine",
    "Event",
    "QuoteEvent",
    "RunResult",
    "Strategy",
    "StrategyContext",
]

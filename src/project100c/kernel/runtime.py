"""Minimal kernel runtime harness: Governor + broker adapter + journal, with write-ahead journalling.

This is the deterministic loop the failure-injection tests drive against the fake broker. It is NOT the
production OMS (K-03..K-07). Its rules:

* Every state change goes through the journal first (write-ahead), then into KernelState via the same reducer
  used on restart, so a restart reproduces kills/halts/positions exactly.
* A journal write failure latches SYSTEM_INTEGRITY in memory (it cannot be journalled), refuses every new entry,
  alerts URGENT, and keeps only exits working.
* Broker faults never pass silently: rejects are journalled as terminal, timeouts become UNKNOWN orders that are
  resolved by client_order_id on the next sync, disconnects are timed and escalate to BROKER_CONNECTIVITY_KILL.
* Reconciliation: broker positions are the truth. Any mismatch latches POSITION_RECONCILIATION_KILL; an
  unexpected broker long is adopted (so it can be flattened), a journal position the broker lacks is alerted.
* 15:00 hard flat with a residual position (OD-007): call exit_all() once, alert URGENT, re-read positions; any
  failure or residual -> EXIT_ALL_FAILED halt (all trading halted) + POSITION_RECONCILIATION_KILL + keep alerting.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from project100c.broker import BrokerAdapter, BrokerOrder, OrderRequest, OrderStatus, OrderType
from project100c.core_types import OrderSide
from project100c.costs import CostModel, Side
from project100c.errors import (
    BrokerDisconnectedError,
    BrokerError,
    BrokerRejectError,
    BrokerTimeoutError,
    GovernorError,
    JournalError,
    KernelInvariantError,
    TicketRejectedError,
)
from project100c.journal import Journal
from project100c.kernel.governor import (
    ActionKind,
    Decision,
    MarketSnapshot,
    Reason,
    RequiredAction,
    RiskGovernor,
    TradeIntent,
    Verdict,
    modelled_slippage,
)
from project100c.kernel.health import HealthObservation, detect_kills
from project100c.kernel.kills import HaltKind, KillSwitch
from project100c.kernel.state import UNEXPECTED_STRATEGY, KernelState, OrderKind, apply
from project100c.market_types import Quote
from project100c.sessions import IST

_INVALID = frozenset(
    {
        Reason.MANDATE_LONG_ONLY,
        Reason.MANDATE_UNDERLYING,
        Reason.MANDATE_INSTRUMENT_TYPE,
        Reason.INVALID_QUANTITY,
        Reason.STOP_INVALID,
        Reason.TICK_SIZE,
    }
)


class AlertSink(Protocol):
    def send(self, severity: str, message: str) -> None: ...


@dataclass
class MemoryAlerts:
    """Alert channel is an open owner decision; tests and the harness record alerts in memory."""

    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, severity: str, message: str) -> None:
        self.sent.append((severity, message))

    def urgent(self) -> list[str]:
        return [m for s, m in self.sent if s == "URGENT"]


@dataclass
class _Protect:
    trigger: Decimal
    limit: Decimal
    lot_size: int
    tick: Decimal
    strategy_id: str


class KernelRuntime:
    def __init__(
        self,
        journal: Journal,
        broker: BrokerAdapter,
        governor: RiskGovernor,
        costs: CostModel,
        plan_id: str,
        clock: Callable[[], datetime],
        alerts: AlertSink,
    ) -> None:
        if governor.paper_venue and getattr(broker, "paper_venue", False) is not True:
            raise KernelInvariantError("a paper-venue Governor needs a broker declaring paper_venue (no real money)")
        self._j = journal
        self._b = broker
        self._g = governor
        self._costs = costs
        self._plan = plan_id
        self._clock = clock
        self._alerts = alerts
        self.state = KernelState.from_journal(journal)  # restart-safe: kills/halts survive
        self.integrity_failed = False
        self._broker_down_since: datetime | None = None
        self._broker_errors: list[datetime] = []
        # OD-017: order/API errors in a row; the order path (place/cancel) and the read path (sync) count separately
        # and each resets on its own next success, so healthy reads cannot hide a failing order path
        self._consec_order_errors = 0
        self._consec_read_errors = 0
        # OD-017 slippage: client_order_id -> (strategy, side, reference mid at decision, modelled per unit)
        self._slip_ref: dict[str, tuple[str, OrderSide, Decimal, Decimal]] = {}
        self._unknown: dict[str, datetime] = {}
        self._oid: dict[str, str] = {}  # client_order_id -> broker_order_id
        self._seen_trades: set[str] = set()
        self._protect: dict[str, _Protect] = {}
        self._protective_cid: dict[str, str] = {}
        self._exit_cid: dict[str, str] = {}
        self._seq = 0
        self._exit_all_done_for: date | None = None
        self._last_quotes: dict[str, Quote] = {}
        self._rebuild_after_restart(journal)

    def _rebuild_after_restart(self, journal: Journal) -> None:
        """K-12 restart: restore what the journal knows but the in-memory maps would forget. Without this a new
        process re-applies broker fills it has already journalled (a doubled position) and cannot find its own
        working protective stop or exit (broker order IDs come back from the order book on the next sync)."""
        for rec in journal.records():
            if rec.event_type == "FILL":
                self._seen_trades.add(str(rec.payload["trade_id"]))
        for cid, oo in self.state.open_orders.items():
            if oo.kind is OrderKind.PROTECTIVE:
                self._protective_cid[oo.instrument_key] = cid
            elif oo.kind is OrderKind.EXIT:
                self._exit_cid[oo.instrument_key] = cid

    # ------------------------------------------------------------------ journal
    def _record(self, event_type: str, payload: dict[str, Any], corr: str = "") -> bool:
        try:
            rec = self._j.append(event_type, payload, correlation_id=corr)
        except JournalError as e:
            self._integrity_failure(f"journal write failed on {event_type}: {e}")
            return False
        self.state = apply(self.state, rec)
        return True

    def _integrity_failure(self, why: str) -> None:
        if not self.integrity_failed:
            self.integrity_failed = True
        self._alerts.send("URGENT", f"SYSTEM_INTEGRITY_KILL (in-memory, journal unavailable): {why}")

    def _alert(self, severity: str, msg: str) -> None:
        self._alerts.send(severity, msg)
        if not self.integrity_failed:
            self._record("ALERT", {"severity": severity, "message": msg})

    def _latch_kill(self, sw: KillSwitch, reason: str, scope: str = "") -> None:
        if (sw, scope) in self.state.kills:
            return
        self._record("KILL_LATCHED", {"switch": str(sw), "scope": scope, "reason": reason})
        self._alert("URGENT", f"{sw}{'(' + scope + ')' if scope else ''} latched: {reason}")

    def _latch_halt(self, hk: HaltKind, reason: str) -> None:
        if hk in self.state.halts:
            return
        self._record("HALT_LATCHED", {"kind": str(hk), "reason": reason})
        self._alert("URGENT", f"{hk} latched: {reason}")

    def _cid(self, stem: str) -> str:
        self._seq += 1
        return f"{stem}-{self.state.last_seq}-{self._seq}"

    # ------------------------------------------------------------------ day
    def start_day(
        self, trading_date: date, sod_nav: Decimal, *, week_start: bool, sow_nav: Decimal | None = None
    ) -> None:
        payload: dict[str, Any] = {"trading_date": trading_date, "sod_nav": sod_nav, "week_start": week_start}
        if week_start:
            payload["sow_nav"] = sow_nav if sow_nav is not None else sod_nav
        self._record("DAY_START", payload)
        for (sw, scope), k in list(self.state.kills.items()):  # DAILY_LOSS auto-resets at a later pre-flight
            if sw is KillSwitch.DAILY_LOSS and k.trading_date < trading_date:
                self._record("KILL_RESET", {"switch": str(sw), "scope": scope, "actor": "SYSTEM"})

    # ------------------------------------------------------------------ owner controls
    def manual_master_kill(self, reason: str, *, requested_by: str) -> bool:
        """The owner's MANUAL_MASTER_KILL (docs/risk/risk-engine.md §9.4). It is latched through the journal, so it
        survives a
        restart and resets only by the owner. Cancel/flatten/halt follow on the next ``step`` via the Governor's
        required actions. Returns False if it was already latched. It never places an entry, by construction."""
        if not reason.strip() or not requested_by.strip():
            raise GovernorError("MANUAL_MASTER_KILL needs a reason and who requested it")
        if (KillSwitch.MANUAL_MASTER, "") in self.state.kills:
            return False
        self._latch_kill(KillSwitch.MANUAL_MASTER, f"{reason} (requested by {requested_by})")
        return True

    def report_reconciliation_mismatch(self, reason: str) -> bool:
        """The standalone reconciler (``execution.reconciler``) found a persistent journal-vs-broker mismatch:
        latch POSITION_RECONCILIATION_KILL (flatten and halt follow on the next ``step``). False if already latched."""
        if (KillSwitch.POSITION_RECONCILIATION, "") in self.state.kills:
            return False
        self._latch_kill(KillSwitch.POSITION_RECONCILIATION, f"reconciler: {reason}")
        return True

    def request_exit(self, instrument_key: str, reason: str) -> bool:
        """A strategy's own exit (target, time exit): sell-to-close through the same path the Governor's FLATTEN
        uses (cancel entry/protective, then a sell limit near the bid; a repeat call re-prices a working exit to the
        sanity-band floor). It can never open or add. Returns False if there is nothing to exit or the window is
        shut."""
        if instrument_key not in self.state.positions or not self._g_clock_allows(self._clock()):
            return False
        if self._exit_cid.get(instrument_key) not in self.state.open_orders:
            self._alert("INFO", f"strategy exit requested for {instrument_key}: {reason}")
        self._flatten(instrument_key)
        return True

    def cancel_entries(self, instrument_key: str) -> int:
        """Cancel this instrument's working ENTRY orders (an entry the strategy no longer wants, e.g. not filled in
        time). It never touches a protective stop or an exit. Returns how many cancels were sent."""
        n = 0
        for cid, o in list(self.state.open_orders.items()):
            if o.instrument_key == instrument_key and o.kind is OrderKind.ENTRY and self._cancel(cid):
                n += 1
        return n

    # ------------------------------------------------------------------ orders
    def submit(self, intent: TradeIntent, market: MarketSnapshot) -> Decision:
        if self.integrity_failed:
            return Decision(Verdict.REJECT, (Reason.SYSTEM_INTEGRITY_FAILURE,), None)
        d = self._g.evaluate(intent, self.state, market)
        self._record(
            "RISK_DECISION",
            {"intent_id": intent.intent_id, "verdict": str(d.verdict), "reasons": [str(r) for r in d.reasons]},
            intent.intent_id,
        )
        if self.integrity_failed:
            return Decision(Verdict.REJECT, (Reason.SYSTEM_INTEGRITY_FAILURE,), None)
        if not d.approved:
            if _INVALID & set(d.reasons):
                self._record("INTENT_REJECTED", {"strategy_id": intent.strategy_id, "invalid": True})
            return d
        if d.ticket is None or d.ticket.simulate_only:
            return d
        now = self._clock()
        if not d.ticket.valid_at(now):
            return Decision(Verdict.REJECT, (Reason.TICKET_EXPIRED,), None)
        kind = intent.exit_kind or OrderKind.EXIT if intent.side is OrderSide.SELL else OrderKind.ENTRY
        if intent.side is OrderSide.BUY and intent.stop_trigger is not None and intent.stop_limit is not None:
            self._protect[intent.contract.instrument_key] = _Protect(
                intent.stop_trigger,
                intent.stop_limit,
                intent.contract.lot_size,
                intent.contract.tick_size,
                intent.strategy_id,
            )
        req = OrderRequest(
            self._cid(intent.intent_id),
            intent.contract.instrument_key,
            intent.side,
            intent.qty,
            OrderType.SL if kind is OrderKind.PROTECTIVE else OrderType.LIMIT,
            intent.limit_price,
            intent.stop_trigger if kind is OrderKind.PROTECTIVE else None,
            tag=str(kind),
        )
        q = market.quote
        if q is not None and q.bid is not None and q.ask is not None:
            modelled = modelled_slippage(market, intent.contract.tick_size, self._g.limits)
            if modelled > 0:
                ref = (q.bid + q.ask) / 2
                self._slip_ref[req.client_order_id] = (intent.strategy_id, intent.side, ref, modelled)
        attach = getattr(self._b, "attach_ticket", None)
        if callable(attach):
            attach(req.client_order_id, d.ticket)  # K-07: the gateway checks it before the order leaves
        self._send(req, intent.strategy_id, kind, intent.contract.lot_size, counts_as_entry=d.counts_as_entry)
        return d

    def _order_error(self, now: datetime) -> None:
        self._broker_errors.append(now)
        self._consec_order_errors += 1

    def _send(
        self, req: OrderRequest, strategy_id: str, kind: OrderKind, lot_size: int, *, counts_as_entry: bool = True
    ) -> bool:
        # Gateway: independent re-check of the window (OD-002) and, for sells, sell-to-close only (OD-006).
        now = self._clock()
        if not self._g.clock.order_activity_allowed(now):
            self._alert("URGENT", f"gateway refused {req.client_order_id}: outside trading window")
            return False
        if req.side is OrderSide.SELL:
            closable = self.state.open_qty().get(req.instrument_key, 0) - self.state.pending_sell_qty().get(
                req.instrument_key, 0
            )
            if req.qty > closable:
                self._alert("URGENT", f"gateway refused {req.client_order_id}: sell {req.qty} > closable {closable}")
                return False
        payload: dict[str, object] = {
            "client_order_id": req.client_order_id,
            "strategy_id": strategy_id,
            "instrument_key": req.instrument_key,
            "side": str(req.side),
            "qty": req.qty,
            "price": req.price,
            "kind": str(kind),
            "lot_size": lot_size,
        }
        if kind is OrderKind.ENTRY and not counts_as_entry:
            payload["counts_as_entry"] = False  # OD-013 straddle: the second leg of one entry
        ok = self._record("ORDER_SUBMITTED", payload, req.client_order_id)
        if not ok:
            return False  # never send an order the journal does not know about
        try:
            self._oid[req.client_order_id] = self._b.place(req)
        except TicketRejectedError as e:
            self._record("ORDER_TERMINAL", {"client_order_id": req.client_order_id, "status": f"REFUSED: {e}"})
            self._latch_kill(KillSwitch.SYSTEM_INTEGRITY, str(e))
            return False
        except BrokerRejectError as e:
            self._record("ORDER_TERMINAL", {"client_order_id": req.client_order_id, "status": f"REJECTED: {e}"})
            self._alert("WARN", f"order {req.client_order_id} rejected: {e}")
            self._order_error(now)
            return False
        except BrokerTimeoutError as e:
            self._unknown[req.client_order_id] = now
            self._order_error(now)
            self._alert("WARN", f"order {req.client_order_id} UNKNOWN after timeout: {e}")
            return False
        except BrokerDisconnectedError as e:
            self._record("ORDER_TERMINAL", {"client_order_id": req.client_order_id, "status": f"NOT_SENT: {e}"})
            self._mark_down(now)
            self._order_error(now)
            return False
        self._consec_order_errors = 0
        if kind is OrderKind.PROTECTIVE:
            self._protective_cid[req.instrument_key] = req.client_order_id
        elif kind is OrderKind.EXIT:
            self._exit_cid[req.instrument_key] = req.client_order_id
        return True

    def _mark_down(self, now: datetime) -> None:
        if self._broker_down_since is None:
            self._broker_down_since = now

    # ------------------------------------------------------------------ sync / reconcile
    def _sync(self, now: datetime) -> bool:
        try:
            orders = self._b.orders()
            fills = self._b.trades()
            positions = self._b.positions()
        except BrokerDisconnectedError:
            self._mark_down(now)
            return False
        except BrokerError as e:
            self._broker_errors.append(now)
            self._consec_read_errors += 1
            self._alert("WARN", f"broker read failed: {e}")
            return False
        self._broker_down_since = None
        self._consec_read_errors = 0
        by_cid: dict[str, BrokerOrder] = {o.request.client_order_id: o for o in orders}
        for cid, o in by_cid.items():  # after a restart: our own orders' broker IDs, from the book
            if cid in self.state.known_orders and cid not in self._unknown:
                self._oid.setdefault(cid, o.broker_order_id)
        for cid in list(self._unknown):
            if cid in by_cid:
                self._oid[cid] = by_cid[cid].broker_order_id
                del self._unknown[cid]
                self._alert("INFO", f"order {cid} resolved by client_order_id after timeout")
                if by_cid[cid].request.tag == str(OrderKind.PROTECTIVE):
                    self._protective_cid[by_cid[cid].request.instrument_key] = cid
                elif by_cid[cid].request.tag == str(OrderKind.EXIT):
                    self._exit_cid[by_cid[cid].request.instrument_key] = cid
        # adopt broker Exit-All orders (placed by the broker, not by us) before applying their fills
        for o in orders:
            cid = o.request.client_order_id
            if cid not in self.state.known_orders and o.request.tag == "EXIT_ALL":
                pos = self.state.positions.get(o.request.instrument_key)
                self._record(
                    "ORDER_SUBMITTED",
                    {
                        "client_order_id": cid,
                        "strategy_id": pos.strategy_id if pos else UNEXPECTED_STRATEGY,
                        "instrument_key": o.request.instrument_key,
                        "side": str(o.request.side),
                        "qty": o.request.qty,
                        "price": o.request.price,
                        "kind": str(OrderKind.EXIT_ALL),
                        "lot_size": pos.lot_size if pos else 1,
                    },
                )
                self._oid[cid] = o.broker_order_id
        for f in fills:
            if f.trade_id in self._seen_trades:
                continue
            if f.client_order_id not in self.state.known_orders:
                self._latch_kill(KillSwitch.POSITION_RECONCILIATION, f"fill {f.trade_id} for unknown order")
                self._seen_trades.add(f.trade_id)
                continue
            if f.client_order_id not in self.state.open_orders:
                self._alert("WARN", f"late fill {f.trade_id} on {f.client_order_id} after cancel (race); applied")
            side = Side.BUY if f.side is OrderSide.BUY else Side.SELL
            charges = self._costs.order_charges(side, f.price, f.qty, f.ts.astimezone(IST).date(), self._plan).total
            self._seen_trades.add(f.trade_id)
            self._record(
                "FILL",
                {
                    "client_order_id": f.client_order_id,
                    "trade_id": f.trade_id,
                    "qty": f.qty,
                    "price": f.price,
                    "charges": charges,
                },
            )
            self._observe_slippage(f.client_order_id, f.price)
        for cid, oo in list(self.state.open_orders.items()):
            bo = by_cid.get(cid)
            if bo is not None and bo.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                self._record("ORDER_TERMINAL", {"client_order_id": cid, "status": str(bo.status)})
                if bo.status is OrderStatus.REJECTED and oo.kind is OrderKind.PROTECTIVE:
                    self._alert("URGENT", f"protective stop rejected for {oo.instrument_key}: {bo.reject_reason}")
        # protective confirmation
        for key, p in self.state.positions.items():
            pcid = self._protective_cid.get(key)
            if pcid and not p.protective_confirmed:
                bo = by_cid.get(pcid)
                if bo is not None and bo.status in (OrderStatus.TRIGGER_PENDING, OrderStatus.OPEN):
                    self._record("PROTECTIVE_CONFIRMED", {"instrument_key": key, "client_order_id": pcid})
        # reconciliation: broker positions are the truth
        broker_net = {p.instrument_key: p.net_qty for p in positions if p.net_qty != 0}
        ours = self.state.open_qty()
        for key in sorted(set(broker_net) | set(ours)):
            bq, oq = broker_net.get(key, 0), ours.get(key, 0)
            if bq == oq:
                continue
            self._latch_kill(KillSwitch.POSITION_RECONCILIATION, f"{key}: broker {bq} vs journal {oq}")
            if bq > 0 and bq > oq:
                bp = next(p for p in positions if p.instrument_key == key)
                avg = bp.buy_value / bp.buy_qty if bp.buy_qty else Decimal(0)
                self._record("POSITION_ADOPTED", {"instrument_key": key, "qty": bq, "avg_price": avg, "lot_size": 1})
            elif bq < 0:
                self._alert("URGENT", f"{key}: broker shows NET SHORT {bq} (OD-006 violation): owner must act")
            else:
                self._alert("URGENT", f"{key}: journal long {oq} not at broker ({bq}); owner review")
        return True

    def _observe_slippage(self, cid: str, price: Decimal) -> None:
        """OD-017: journal the realised slippage of a fill (adverse only; a better price counts as 0) against the
        decision-time mid of a submitted intent. Orders without that reference (no two-sided decision quote, forced
        flatten, Exit-All) are not measured, nor are protective stops: an SL-limit cannot fill below its limit, and the
        risk budget already assumes a fill at that limit plus the modelled slippage."""
        ref = self._slip_ref.get(cid)
        if ref is None:
            return
        sid, side, mid, modelled = ref
        realised = max(Decimal(0), price - mid if side is OrderSide.BUY else mid - price)
        self._record(
            "SLIPPAGE_OBSERVED",
            {"strategy_id": sid, "client_order_id": cid, "realised": realised, "modelled": modelled},
            cid,
        )

    def _ensure_protective(self) -> None:
        for key, p in self.state.positions.items():
            spec = self._protect.get(key)
            if spec is None or self._exit_cid.get(key) in self.state.open_orders:
                continue  # an exit is working for this key (a finished exit from an earlier trade does not count)
            pcid = self._protective_cid.get(key)
            cur = self.state.open_orders.get(pcid) if pcid else None
            if cur is not None and cur.remaining == p.qty:
                continue
            if cur is not None:  # partial entry fill grew the position: replace the stop for the full qty
                if not self._cancel(cur.client_order_id):
                    continue
            req = OrderRequest(
                self._cid(f"{key}-SL"),
                key,
                OrderSide.SELL,
                p.qty,
                OrderType.SL,
                spec.limit,
                spec.trigger,
                tag=str(OrderKind.PROTECTIVE),
            )
            self._send(req, p.strategy_id, OrderKind.PROTECTIVE, spec.lot_size)

    def _cancel(self, cid: str) -> bool:
        oid = self._oid.get(cid)
        if oid is None:
            return False
        try:
            self._b.cancel(oid)
        except BrokerRejectError as e:
            self._alert("WARN", f"cancel {cid} rejected: {e}")
            return False
        except BrokerError as e:
            self._order_error(self._clock())
            self._alert("WARN", f"cancel {cid} failed: {e}")
            return False
        self._consec_order_errors = 0
        self._record("ORDER_TERMINAL", {"client_order_id": cid, "status": "CANCELLED"})
        return True

    # ------------------------------------------------------------------ step
    def step(
        self, quotes: Mapping[str, Quote] | None = None, health: HealthObservation | None = None
    ) -> list[RequiredAction]:
        now = self._clock()
        if quotes:
            self._last_quotes.update(quotes)
        halted = HaltKind.EXIT_ALL_FAILED in self.state.halts or HaltKind.RESIDUAL_AT_HARD_FLAT in self.state.halts
        synced = self._sync(now)
        order_ok = self._g_clock_allows(now) and not halted
        self._mark()
        if synced and order_ok and not self.integrity_failed:
            self._ensure_protective()
        # observation-driven kills
        obs = health or HealthObservation()
        window = timedelta(seconds=float(self._g.limits.broker_error_window_s))
        self._broker_errors = [t for t in self._broker_errors if now - t <= window]
        down = Decimal(str((now - self._broker_down_since).total_seconds())) if self._broker_down_since else Decimal(0)
        unknown_age = max((Decimal(str((now - t).total_seconds())) for t in self._unknown.values()), default=Decimal(0))
        obs = HealthObservation(
            **{
                **{f: getattr(obs, f) for f in obs.__dataclass_fields__},
                "broker_errors_in_window": max(obs.broker_errors_in_window, len(self._broker_errors)),
                "broker_consecutive_errors": max(
                    obs.broker_consecutive_errors, self._consec_order_errors, self._consec_read_errors
                ),
                "ws_down_s": max(obs.ws_down_s, down),
                "unknown_order_age_s": max(obs.unknown_order_age_s, unknown_age),
                "journal_write_failed": obs.journal_write_failed or self.integrity_failed,
            }
        )
        for sw, why in detect_kills(obs, self._g.limits):
            if sw is KillSwitch.SYSTEM_INTEGRITY and self.integrity_failed:
                self._alerts.send("URGENT", f"SYSTEM_INTEGRITY_KILL: {why}")
                continue
            self._latch_kill(sw, why)
        actions = self._g.required_actions(self.state, now)
        for a in actions:
            self._execute(a, now, order_ok)
        if any(a.kind in (ActionKind.LATCH_KILL, ActionKind.LATCH_HALT) for a in actions):
            # a new latch changes what must happen now (e.g. DAILY_LOSS -> flatten): act in the same step
            again = [
                a
                for a in self._g.required_actions(self.state, now)
                if a.kind not in (ActionKind.LATCH_KILL, ActionKind.LATCH_HALT)
            ]
            for a in again:
                self._execute(a, now, order_ok)
            actions = actions + again
        return actions

    def _mark(self) -> None:
        """Mark open longs to the bid (docs/risk/risk-engine.md §9.3); a position without a usable bid is marked at
        zero."""
        if self.integrity_failed:
            return
        u = Decimal(0)
        for key, p in self.state.positions.items():
            q = self._last_quotes.get(key)
            bid = q.bid if q is not None and q.bid is not None and q.bid > 0 else Decimal(0)
            u += bid * p.qty - p.cost_value
        if u != self.state.unrealised:
            self._record("MARK", {"unrealised": u})

    def _g_clock_allows(self, now: datetime) -> bool:
        return self._g.clock.order_activity_allowed(now)

    def _execute(self, a: RequiredAction, now: datetime, order_ok: bool) -> None:
        k = a.kind
        if k is ActionKind.LATCH_KILL and a.kill is not None:
            self._latch_kill(a.kill, a.reason, a.scope)
        elif k is ActionKind.LATCH_HALT and a.halt is not None:
            self._latch_halt(a.halt, a.reason)
        elif k is ActionKind.ALERT:
            self._alerts.send("WARN", a.reason)
        elif k is ActionKind.ALERT_URGENT:
            self._alerts.send("URGENT", a.reason)  # repeated every step while the condition holds ("keep alerting")
        elif k is ActionKind.CANCEL_ALL and order_ok:
            for cid, o in list(self.state.open_orders.items()):
                if o.kind is OrderKind.ENTRY:
                    self._cancel(cid)
        elif k is ActionKind.FLATTEN and order_ok:
            keys = [a.instrument_key] if a.instrument_key else list(self.state.positions)
            for key in keys:
                self._flatten(key)
        elif k is ActionKind.EXIT_ALL:
            self._exit_all(now, a.reason)
        elif k is ActionKind.HALT:
            self._latch_halt(HaltKind.RESIDUAL_AT_HARD_FLAT, a.reason)
        elif k is ActionKind.VERIFY_PROTECTIVE:
            for key, p in self.state.positions.items():
                pcid = self._protective_cid.get(key)
                if pcid is None or pcid not in self.state.open_orders:
                    self._alerts.send("URGENT", f"{a.reason}: no protective stop resting for {key} ({p.qty})")

    def _flatten(self, key: str) -> None:
        p = self.state.positions.get(key)
        if p is None:
            return
        q = self._last_quotes.get(key)
        for cid, o in list(self.state.open_orders.items()):
            if o.instrument_key == key and o.kind in (OrderKind.ENTRY, OrderKind.PROTECTIVE):
                self._cancel(cid)
        if q is None or q.bid is None or q.bid <= 0:
            self._alerts.send("URGENT", f"cannot flatten {key}: no bid; protective/owner action needed")
            return
        tick = self._protect[key].tick if key in self._protect else Decimal("0.05")
        ecid = self._exit_cid.get(key)
        if ecid in self.state.open_orders:  # escalate the working exit to the floor of the sanity band
            floor = max(tick, q.bid - self._g.limits.limit_below_bid_ticks * tick)
            oid = self._oid.get(ecid) if ecid else None
            if oid is not None:
                try:
                    self._b.modify(oid, price=floor)
                except BrokerError as e:
                    self._alert("WARN", f"exit re-price failed for {key}: {e}")
            return
        pending = self.state.pending_sell_qty().get(key, 0)
        qty = p.qty - pending
        if qty <= 0:
            return
        price = max(tick, q.bid - 2 * tick)
        req = OrderRequest(
            self._cid(f"{key}-FLAT"), key, OrderSide.SELL, qty, OrderType.LIMIT, price, tag=str(OrderKind.EXIT)
        )
        self._send(req, p.strategy_id, OrderKind.EXIT, p.lot_size)

    def _exit_all(self, now: datetime, reason: str) -> None:
        today = now.astimezone(IST).date()
        if self._exit_all_done_for == today:
            return  # once per day; any residual after it is handled by the EXIT_ALL_FAILED halt
        self._exit_all_done_for = today
        self._record("EXIT_ALL_REQUESTED", {"reason": reason, "pricing_verified": False})
        self._alerts.send("URGENT", f"OD-007 Exit-All triggered: {reason} (Exit-All pricing UNVERIFIED)")
        failure: str | None = None
        try:
            res = self._b.exit_all()
            if res.failed_instruments:
                failure = f"broker reported failures: {list(res.failed_instruments)}"
        except BrokerError as e:
            failure = f"exit_all raised {type(e).__name__}: {e}"
        if not self._sync(now):
            failure = (failure + "; " if failure else "") + "positions unreadable after Exit-All"
        residual = self.state.open_qty()
        if residual and failure is None:
            failure = f"residual after Exit-All: {residual}"
        self._record("EXIT_ALL_RESULT", {"ok": failure is None, "detail": failure or "flat"})
        if failure is not None:
            self._latch_halt(HaltKind.EXIT_ALL_FAILED, failure)
            self._latch_kill(KillSwitch.POSITION_RECONCILIATION, f"Exit-All failed: {failure}")
            self._alerts.send("URGENT", f"OD-007 Exit-All FAILED: {failure}. ALL trading halted; owner must act")
        else:
            self._alert("URGENT", "OD-007 Exit-All completed: broker positions flat")

    # expose for tests
    @property
    def unknown_orders(self) -> dict[str, datetime]:
        return dict(self._unknown)

    def check_invariants(self) -> None:
        for key, q in self.state.open_qty().items():
            if q < 0:
                raise KernelInvariantError(f"net short {key}")

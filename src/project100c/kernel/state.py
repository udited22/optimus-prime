"""KernelState: the Capital Preservation Kernel's state, rebuilt ONLY from journal records (docs/risk/risk-engine.md
§9.3).

`apply(state, record)` is a pure reducer; `KernelState.from_journal(j)` replays (and hash-verifies) the journal.
Unknown event types and malformed payloads raise KernelInvariantError: the kernel never guesses.

Event vocabulary (payload keys):
  DAY_START           trading_date, sod_nav, sow_nav, week_start (bool)
  ORDER_SUBMITTED     client_order_id, strategy_id, instrument_key, side, qty, price, kind, lot_size
                      [counts_as_entry = false: a straddle's second leg, not a new entry (OD-013/OD-014)]
  ORDER_TERMINAL      client_order_id, status
  FILL                client_order_id, trade_id, qty, price, charges
  PROTECTIVE_CONFIRMED instrument_key, client_order_id
  POSITION_ADOPTED    instrument_key, qty, avg_price, lot_size  (broker is truth; reconciliation kill latched)
  MARK                unrealised (mark-to-bid P&L of all open longs, net of nothing)
  INTENT_REJECTED     strategy_id, reasons
  SLIPPAGE_BREACH     strategy_id  (legacy: one breach counted toward strategy_slippage_breach_trades)
  SLIPPAGE_OBSERVED   strategy_id, client_order_id, realised, modelled  (per unit, rupees; OD-017 rules)
  KILL_LATCHED        switch, scope, reason
  KILL_RESET          switch, scope, actor
  HALT_LATCHED        kind, reason
  HALT_RESET          kind, actor
  ALERT               severity, message  (no state change; kept for audit)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from project100c.core_types import OrderSide
from project100c.errors import KernelInvariantError
from project100c.journal import Journal, JournalRecord
from project100c.kernel.kills import HaltKind, HaltLatch, KillLatch, KillSwitch

UNEXPECTED_STRATEGY = "__UNEXPECTED__"  # owner of positions/orders adopted from broker truth
SLIPPAGE_KEEP = 20  # fills kept per strategy for the slippage rules (>= any slippage_window_fills in use)


class OrderKind(StrEnum):
    ENTRY = "ENTRY"
    PROTECTIVE = "PROTECTIVE"  # the resting SL-limit protecting a long
    EXIT = "EXIT"  # target / time / forced-flatten LIMIT exit
    EXIT_ALL = "EXIT_ALL"  # broker Exit-All (OD-007)


@dataclass(frozen=True, slots=True)
class OpenOrder:
    client_order_id: str
    strategy_id: str
    instrument_key: str
    side: OrderSide
    qty: int
    filled: int
    price: Decimal
    kind: OrderKind
    lot_size: int
    submitted_at: datetime

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


@dataclass(frozen=True, slots=True)
class Position:
    instrument_key: str
    strategy_id: str
    qty: int
    cost_value: Decimal  # exact sum of fill price * qty still held (no rounding from averaging)
    lot_size: int
    opened_at: datetime
    protective_confirmed: bool
    entry_charges: Decimal  # buy-side charges not yet attributed to a closing fill
    peak_qty: int = 0
    trade_pnl: Decimal = Decimal(0)  # realised so far on this position (partial exits)

    @property
    def avg_price(self) -> Decimal:
        return self.cost_value / self.qty


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    strategy_id: str
    instrument_key: str
    lots: int
    pnl: Decimal  # net of charges
    stopped_out: bool
    closed_at: datetime


def _freeze[K, V](d: Mapping[K, V]) -> Mapping[K, V]:
    return MappingProxyType(dict(d))


@dataclass(frozen=True, slots=True)
class KernelState:
    trading_date: date | None = None
    sod_nav: Decimal = Decimal(0)
    sow_nav: Decimal = Decimal(0)
    hwm: Decimal = Decimal(0)
    realised_today: Decimal = Decimal(0)  # net of charges
    unrealised: Decimal = Decimal(0)
    positions: Mapping[str, Position] = field(default_factory=lambda: _freeze({}))
    open_orders: Mapping[str, OpenOrder] = field(default_factory=lambda: _freeze({}))
    known_orders: frozenset[str] = frozenset()
    closed_orders: Mapping[str, OpenOrder] = field(default_factory=lambda: _freeze({}))  # for late fills
    entries_today: int = 0
    entries_by_strategy: Mapping[str, int] = field(default_factory=lambda: _freeze({}))  # ENTRY orders today
    closed_trades: tuple[ClosedTrade, ...] = ()
    consecutive_stops: Mapping[str, int] = field(default_factory=lambda: _freeze({}))
    last_stop_out: Mapping[str, datetime] = field(default_factory=lambda: _freeze({}))
    invalid_intents: Mapping[str, int] = field(default_factory=lambda: _freeze({}))
    slippage_breaches: Mapping[str, int] = field(default_factory=lambda: _freeze({}))
    # the latest SLIPPAGE_KEEP fills per strategy as (realised, modelled) per unit; the Governor applies OD-017
    slippage_fills: Mapping[str, tuple[tuple[Decimal, Decimal], ...]] = field(default_factory=lambda: _freeze({}))
    kills: Mapping[tuple[KillSwitch, str], KillLatch] = field(default_factory=lambda: _freeze({}))
    halts: Mapping[HaltKind, HaltLatch] = field(default_factory=lambda: _freeze({}))
    last_seq: int = 0

    # ---- derived ----
    @property
    def nav(self) -> Decimal:
        return self.sod_nav + self.realised_today + self.unrealised

    @property
    def daily_loss(self) -> Decimal:
        return max(Decimal(0), self.sod_nav - self.nav)

    @property
    def weekly_loss(self) -> Decimal:
        return max(Decimal(0), self.sow_nav - self.nav)

    @property
    def drawdown_frac(self) -> Decimal:
        hwm = max(self.hwm, self.nav)
        return Decimal(0) if hwm <= 0 else (hwm - self.nav) / hwm

    def open_qty(self) -> dict[str, int]:
        return {k: p.qty for k, p in self.positions.items() if p.qty}

    def pending_sell_qty(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.open_orders.values():
            if o.side is OrderSide.SELL:
                out[o.instrument_key] = out.get(o.instrument_key, 0) + o.remaining
        return out

    def global_kills(self) -> list[KillLatch]:
        return [k for (sw, scope), k in self.kills.items() if scope == ""]

    def strategy_killed(self, strategy_id: str) -> bool:
        return (KillSwitch.STRATEGY, strategy_id) in self.kills

    @classmethod
    def from_journal(cls, journal: Journal) -> KernelState:
        return journal.replay(apply, cls())


def _dec(p: Mapping[str, Any], k: str) -> Decimal:
    v = p.get(k)
    if not isinstance(v, Decimal):
        raise KernelInvariantError(f"payload[{k!r}] must be Decimal, got {type(v).__name__}")
    return v


def _int(p: Mapping[str, Any], k: str) -> int:
    v = p.get(k)
    if isinstance(v, bool) or not isinstance(v, int):
        raise KernelInvariantError(f"payload[{k!r}] must be int")
    return v


def _str(p: Mapping[str, Any], k: str) -> str:
    v = p.get(k)
    if not isinstance(v, str):
        raise KernelInvariantError(f"payload[{k!r}] must be str")
    return v


def apply(s: KernelState, r: JournalRecord) -> KernelState:
    p, t = r.payload, r.event_type
    s = replace(s, last_seq=r.seq)
    if t == "DAY_START":
        td = p.get("trading_date")
        if not isinstance(td, date):
            raise KernelInvariantError("DAY_START.trading_date must be a date")
        if s.positions:
            raise KernelInvariantError("DAY_START with open positions: overnight position violates OD-002")
        sod = _dec(p, "sod_nav")
        sow = _dec(p, "sow_nav") if p.get("week_start") else (s.sow_nav or sod)
        return replace(
            s,
            trading_date=td,
            sod_nav=sod,
            sow_nav=sow,
            hwm=max(s.hwm, sod),
            realised_today=Decimal(0),
            unrealised=Decimal(0),
            entries_today=0,
            entries_by_strategy=_freeze({}),
        )
    if t == "ORDER_SUBMITTED":
        cid = _str(p, "client_order_id")
        if cid in s.known_orders:
            raise KernelInvariantError(f"duplicate client_order_id {cid}")
        o = OpenOrder(
            cid,
            _str(p, "strategy_id"),
            _str(p, "instrument_key"),
            OrderSide(_str(p, "side")),
            _int(p, "qty"),
            0,
            _dec(p, "price"),
            OrderKind(_str(p, "kind")),
            _int(p, "lot_size"),
            r.ts,
        )
        oo = dict(s.open_orders)
        oo[cid] = o
        counted = p.get("counts_as_entry", True)
        if not isinstance(counted, bool):
            raise KernelInvariantError(f"{cid}: counts_as_entry must be a bool")
        new_entry = o.kind is OrderKind.ENTRY and counted
        return replace(
            s,
            open_orders=_freeze(oo),
            known_orders=s.known_orders | {cid},
            entries_today=s.entries_today + (1 if new_entry else 0),
            entries_by_strategy=_freeze(
                {**s.entries_by_strategy, o.strategy_id: s.entries_by_strategy.get(o.strategy_id, 0) + 1}
                if new_entry
                else s.entries_by_strategy
            ),
        )
    if t == "ORDER_TERMINAL":
        cid = _str(p, "client_order_id")
        if cid not in s.known_orders:
            raise KernelInvariantError(f"terminal status for unknown order {cid}")
        oo = dict(s.open_orders)
        gone = oo.pop(cid, None)
        co = dict(s.closed_orders)
        if gone is not None:
            co[cid] = gone
        return replace(s, open_orders=_freeze(oo), closed_orders=_freeze(co))
    if t == "FILL":
        return _apply_fill(s, p, r.ts)
    if t == "PROTECTIVE_CONFIRMED":
        key = _str(p, "instrument_key")
        pos = s.positions.get(key)
        if pos is None:
            raise KernelInvariantError(f"protective confirmed for {key} with no position")
        ps = dict(s.positions)
        ps[key] = replace(pos, protective_confirmed=True)
        return replace(s, positions=_freeze(ps))
    if t == "POSITION_ADOPTED":
        key = _str(p, "instrument_key")
        qty = _int(p, "qty")
        ps = dict(s.positions)
        cur = ps.get(key)
        if qty <= 0:
            raise KernelInvariantError("adopted position must be a long (OD-006)")
        if cur is None:
            avg = _dec(p, "avg_price")
            ps[key] = Position(
                key, UNEXPECTED_STRATEGY, qty, avg * qty, _int(p, "lot_size"), r.ts, False, Decimal(0), qty
            )
        else:
            ps[key] = replace(cur, qty=qty, cost_value=cur.avg_price * qty, peak_qty=max(cur.peak_qty, qty))
        return replace(s, positions=_freeze(ps))
    if t == "MARK":
        return replace(
            s, unrealised=_dec(p, "unrealised"), hwm=max(s.hwm, s.sod_nav + s.realised_today + _dec(p, "unrealised"))
        )
    if t == "INTENT_REJECTED":
        sid = _str(p, "strategy_id")
        if p.get("invalid"):
            d = dict(s.invalid_intents)
            d[sid] = d.get(sid, 0) + 1
            return replace(s, invalid_intents=_freeze(d))
        return s
    if t == "SLIPPAGE_BREACH":
        sid = _str(p, "strategy_id")
        d = dict(s.slippage_breaches)
        d[sid] = d.get(sid, 0) + 1
        return replace(s, slippage_breaches=_freeze(d))
    if t == "SLIPPAGE_OBSERVED":
        sid = _str(p, "strategy_id")
        _str(p, "client_order_id")
        realised, modelled = _dec(p, "realised"), _dec(p, "modelled")
        if realised < 0 or modelled <= 0:
            raise KernelInvariantError(f"SLIPPAGE_OBSERVED needs realised >= 0 and modelled > 0, got {p}")
        sf = dict(s.slippage_fills)
        sf[sid] = (*sf.get(sid, ()), (realised, modelled))[-SLIPPAGE_KEEP:]
        return replace(s, slippage_fills=_freeze(sf))
    if t == "KILL_LATCHED":
        sw, scope = KillSwitch(_str(p, "switch")), _str(p, "scope")
        if (sw, scope) in s.kills:
            return s  # already latched: first reason/time is kept
        if s.trading_date is None:
            raise KernelInvariantError("kill latched before any DAY_START")
        k = dict(s.kills)
        k[(sw, scope)] = KillLatch(sw, scope, _str(p, "reason"), r.ts, s.trading_date)
        return replace(s, kills=_freeze(k))
    if t == "KILL_RESET":
        kkey = (KillSwitch(_str(p, "switch")), _str(p, "scope"))
        if kkey not in s.kills:
            raise KernelInvariantError(f"reset of a kill that is not latched: {kkey}")
        k = dict(s.kills)
        del k[kkey]
        d_stop = dict(s.consecutive_stops)
        d_inv = dict(s.invalid_intents)
        d_slip = dict(s.slippage_breaches)
        d_fills = dict(s.slippage_fills)
        if kkey[0] is KillSwitch.STRATEGY:  # validation report reviewed: counters restart
            for d in (d_stop, d_inv, d_slip):
                d.pop(kkey[1], None)
            d_fills.pop(kkey[1], None)
        return replace(
            s,
            kills=_freeze(k),
            consecutive_stops=_freeze(d_stop),
            invalid_intents=_freeze(d_inv),
            slippage_breaches=_freeze(d_slip),
            slippage_fills=_freeze(d_fills),
        )
    if t == "HALT_LATCHED":
        hk = HaltKind(_str(p, "kind"))
        if hk in s.halts:
            return s
        h = dict(s.halts)
        h[hk] = HaltLatch(hk, _str(p, "reason"), r.ts)
        return replace(s, halts=_freeze(h))
    if t == "HALT_RESET":
        hk = HaltKind(_str(p, "kind"))
        if hk not in s.halts:
            raise KernelInvariantError(f"reset of a halt that is not latched: {hk}")
        h = dict(s.halts)
        del h[hk]
        return replace(s, halts=_freeze(h))
    if t in ("ALERT", "RISK_DECISION", "RECONCILIATION", "EXIT_ALL_REQUESTED", "EXIT_ALL_RESULT"):
        return s
    raise KernelInvariantError(f"unknown journal event type {t!r}")


def _apply_fill(s: KernelState, p: Mapping[str, Any], ts: datetime) -> KernelState:
    cid = _str(p, "client_order_id")
    o = s.open_orders.get(cid)
    late = False
    if o is None:
        o = s.closed_orders.get(cid)  # cancel/fill race: the exchange filled before our cancel landed
        late = True
    if o is None:
        raise KernelInvariantError(f"fill for an unknown order: {cid}")
    qty, px, charges = _int(p, "qty"), _dec(p, "price"), _dec(p, "charges")
    if qty <= 0 or qty > o.remaining:
        raise KernelInvariantError(f"fill qty {qty} invalid for {cid} (remaining {o.remaining})")
    oo = dict(s.open_orders)
    co = dict(s.closed_orders)
    nf = replace(o, filled=o.filled + qty)
    if late:
        co[cid] = nf
    elif nf.remaining == 0:
        del oo[cid]
    else:
        oo[cid] = nf
    ps = dict(s.positions)
    pos = ps.get(o.instrument_key)
    realised = s.realised_today
    closed = s.closed_trades
    stops, last_stop = dict(s.consecutive_stops), dict(s.last_stop_out)
    if o.side is OrderSide.BUY:
        if pos is None:
            ps[o.instrument_key] = Position(
                o.instrument_key, o.strategy_id, qty, px * qty, o.lot_size, ts, False, charges, qty
            )
        else:
            nq = pos.qty + qty
            ps[o.instrument_key] = replace(
                pos,
                qty=nq,
                cost_value=pos.cost_value + px * qty,
                peak_qty=max(pos.peak_qty, nq),
                entry_charges=pos.entry_charges + charges,
            )
    else:
        if pos is None or qty > pos.qty:
            raise KernelInvariantError(f"sell fill would go net short in {o.instrument_key} (OD-006)")
        nq = pos.qty - qty
        # exact on a full exit; proportional (Decimal context precision) on a partial exit
        share = pos.entry_charges if nq == 0 else pos.entry_charges * qty / pos.qty
        basis = pos.cost_value if nq == 0 else pos.cost_value * qty / pos.qty
        pnl = px * qty - basis - charges - share
        realised += pnl
        if nq == 0:
            del ps[o.instrument_key]
            total = pos.trade_pnl + pnl
            stopped = o.kind is OrderKind.PROTECTIVE
            closed = (
                *closed,
                ClosedTrade(
                    pos.strategy_id,
                    o.instrument_key,
                    max(pos.peak_qty, pos.qty) // max(pos.lot_size, 1) or 1,
                    total,
                    stopped,
                    ts,
                ),
            )
            if stopped:
                stops[pos.strategy_id] = stops.get(pos.strategy_id, 0) + 1
                last_stop[pos.strategy_id] = ts
            else:
                stops[pos.strategy_id] = 0
        else:
            ps[o.instrument_key] = replace(
                pos,
                qty=nq,
                cost_value=pos.cost_value - basis,
                entry_charges=pos.entry_charges - share,
                trade_pnl=pos.trade_pnl + pnl,
            )
    return replace(
        s,
        open_orders=_freeze(oo),
        closed_orders=_freeze(co),
        positions=_freeze(ps),
        realised_today=realised,
        closed_trades=closed[-100:],
        consecutive_stops=_freeze(stops),
        last_stop_out=_freeze(last_stop),
    )

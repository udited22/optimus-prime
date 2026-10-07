"""Trading days from the kernel's event journal (K-01), repriced per executed order with the D-05 cost model.

The journal is the source of truth for trades. For each trading day (DAY_START .. next DAY_START) we take every
FILL, group fills by order, and price each executed order once with ``CostModel.order_charges`` at its VWAP.
That charges brokerage once per executed order, as brokers bill it; the kernel's own FILL records price each fill
separately (conservative on partial fills), and that figure is kept as ``recorded_charges`` for comparison.

Every day must end flat (OD-002); a day that does not, an unknown order, or a fill crossing days raises
EconomicsError instead of producing a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from project100c.costs import CostModel, Side
from project100c.economics.model import ChargeComponents, TradingDay
from project100c.errors import EconomicsError
from project100c.journal import Journal, JournalRecord
from project100c.sessions.model import IST

ZERO = Decimal(0)


@dataclass
class _Order:
    side: Side
    instrument_key: str
    qty: int = 0
    value: Decimal = ZERO
    day: date | None = None


@dataclass
class _Day:
    day: date
    opening_nav: Decimal
    orders: dict[str, _Order] = field(default_factory=dict)
    net_qty: dict[str, int] = field(default_factory=dict)
    cash: dict[str, Decimal] = field(default_factory=dict)  # sell value - buy value, per instrument
    round_trips: int = 0
    recorded_charges: Decimal = ZERO


def _dec(p: dict[str, object], k: str, rec: JournalRecord) -> Decimal:
    v = p.get(k)
    if not isinstance(v, Decimal):
        raise EconomicsError(f"journal seq {rec.seq}: {rec.event_type}.{k} must be Decimal")
    return v


def _int(p: dict[str, object], k: str, rec: JournalRecord) -> int:
    v = p.get(k)
    if isinstance(v, bool) or not isinstance(v, int):
        raise EconomicsError(f"journal seq {rec.seq}: {rec.event_type}.{k} must be int")
    return v


def _str(p: dict[str, object], k: str, rec: JournalRecord) -> str:
    v = p.get(k)
    if not isinstance(v, str):
        raise EconomicsError(f"journal seq {rec.seq}: {rec.event_type}.{k} must be str")
    return v


def _close(d: _Day, costs: CostModel, plan_id: str, simulated: bool) -> TradingDay:
    open_ = {k: q for k, q in d.net_qty.items() if q}
    if open_:
        raise EconomicsError(f"{d.day}: not flat at the end of the day {open_} (OD-002); economics needs flat days")
    charges = ChargeComponents()
    executed = 0
    for cid, o in d.orders.items():
        if o.qty == 0:
            continue  # submitted but never filled: no charge
        if o.day != d.day:
            raise EconomicsError(f"order {cid} filled on {o.day}, outside its trading day {d.day}")
        vwap = o.value / o.qty
        charges = charges + ChargeComponents.from_breakdown(costs.order_charges(o.side, vwap, o.qty, d.day, plan_id))
        executed += 1
    gross = sum(d.cash.values(), ZERO)
    return TradingDay(
        d.day,
        d.opening_nav,
        gross,
        charges,
        executed,
        d.round_trips,
        sum((abs(v) for v in d.cash.values()), ZERO),
        d.recorded_charges,
        simulated,
    )


def trading_days_from_journal(
    journal: Journal, costs: CostModel, plan_id: str, *, simulated: bool
) -> tuple[TradingDay, ...]:
    """One TradingDay per DAY_START in the journal (hash chain verified first)."""
    journal.verify()
    out: list[TradingDay] = []
    cur: _Day | None = None
    for rec in journal.records():
        p = dict(rec.payload)
        t = rec.event_type
        if t == "DAY_START":
            if cur is not None:
                out.append(_close(cur, costs, plan_id, simulated))
            td = p.get("trading_date")
            if not isinstance(td, date):
                raise EconomicsError(f"journal seq {rec.seq}: DAY_START.trading_date must be a date")
            cur = _Day(td, _dec(p, "sod_nav", rec))
        elif t == "ORDER_SUBMITTED":
            if cur is None:
                raise EconomicsError(f"journal seq {rec.seq}: order before any DAY_START")
            cid = _str(p, "client_order_id", rec)
            if cid in cur.orders:
                raise EconomicsError(f"duplicate client_order_id {cid}")
            cur.orders[cid] = _Order(Side(_str(p, "side", rec)), _str(p, "instrument_key", rec))
        elif t == "FILL":
            if cur is None:
                raise EconomicsError(f"journal seq {rec.seq}: fill before any DAY_START")
            cid = _str(p, "client_order_id", rec)
            o = cur.orders.get(cid)
            if o is None:
                raise EconomicsError(f"journal seq {rec.seq}: fill for an order not submitted today: {cid}")
            qty, px = _int(p, "qty", rec), _dec(p, "price", rec)
            if qty <= 0 or px <= 0:
                raise EconomicsError(f"journal seq {rec.seq}: fill qty/price must be > 0")
            fill_day = rec.ts.astimezone(IST).date()
            if o.day is not None and o.day != fill_day:
                raise EconomicsError(f"order {cid} has fills on {o.day} and {fill_day}")
            o.day = fill_day
            o.qty += qty
            o.value += px * qty
            cur.recorded_charges += _dec(p, "charges", rec)
            k = o.instrument_key
            before = cur.net_qty.get(k, 0)
            sign = 1 if o.side is Side.BUY else -1
            after = before + sign * qty
            if after < 0:
                raise EconomicsError(f"{k}: fills go net short (OD-006); journal inconsistent")
            cur.net_qty[k] = after
            cur.cash[k] = cur.cash.get(k, ZERO) - sign * px * qty
            if before > 0 and after == 0:
                cur.round_trips += 1
        elif t == "POSITION_ADOPTED":
            raise EconomicsError(
                f"journal seq {rec.seq}: adopted broker position (unknown cost basis); reconcile before economics"
            )
    if cur is not None:
        out.append(_close(cur, costs, plan_id, simulated))
    return tuple(out)

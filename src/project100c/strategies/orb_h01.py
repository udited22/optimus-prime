"""H01 opening-range breakout (spec ``S-ORB-001``) for the bar backtester.

Parameters are read from the authored spec, never hard-coded. The spec is written for quote data and tooling we do
not have yet, so this implementation departs from it in the ways listed in ``H01_DEVIATIONS``. The list is written
into each run's metadata (and so into the ledger hash), which means no result can be quoted without its caveats.

Signal (spec ``signal``): NIFTY futures 1-minute bars are aggregated into 5-minute closes. The opening range (OR)
is the high/low of the first ``or_window_minutes``. A 5-minute close inside the entry window qualifies when
``close > OR_high`` (BUY CE) or ``close < OR_low`` (BUY PE), and ``vol_ratio >= theta_v`` (5-minute futures
volume against the median of the same slot over the previous 20 trading days), and India VIX has not fallen over
30 minutes. Only the first qualifying close of a day is traded.

Execution: the nearest expiry with DTE >= 1 is used, at the strike nearest the index. The order is a LIMIT at the
proxy mid + 1 tick, with up to ``max_chase_ticks`` re-quotes, capped at the first limit + that many ticks. Sizing
is 1 lot only if ``risk_at_stop`` fits the per-trade risk budget, otherwise NO TRADE. Protection is an SL-LIMIT at
a 30% premium stop with a 4-tick limit offset. Exits are the 1.5 R target, a time exit at 14:30 and a 75-minute
maximum hold. From 14:50 the engine's forced flatten takes over.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from project100c.backtest.engine import StrategyContext
from project100c.backtest.feed import MarketView
from project100c.backtest.types import (
    CancelOrder,
    Intent,
    OptionContract,
    OrderStatus,
    PlaceOrder,
    SimOrderType,
)
from project100c.core_types import OptionRight, OrderSide
from project100c.costs.model import CostModel, Side
from project100c.errors import BacktestError
from project100c.market_types import Bar
from project100c.sessions.model import IST
from project100c.spec.models import LegRight, StopMethod, StrategySpec

H01_DEVIATIONS: tuple[str, ...] = (
    "QUOTES: the spec needs 1-second option quotes (bid/ask); none exist yet. Mid is proxied by the last 1-minute "
    "option close, and fills use the bar model with the ASSUMED synthetic spread.",
    "STRIKE: delta 0.40-0.55 by own IV calc is not implemented; the strike nearest the index spot (ATM) is used.",
    "SPREAD_FILTER: 'spread <= 2 ticks' is not evaluated (no measured spreads; the synthetic ones are assumptions).",
    "TTL: a 10 s entry TTL cannot be represented on 1-minute bars; each entry order gets exactly one bar, then it is "
    "cancelled and re-quoted up to max_chase_ticks times.",
    "PROFIT: the 1.5 R target is checked on 1-minute option closes; the 15-minute swing trailing exit is not "
    "implemented.",
    "EXITS: an exit cancels the stop and then places a sell limit, so the position has no stop for up to one bar.",
    "REGIMES: eligible and prohibited regimes are not gated (no regime classifier yet).",
    "UNUSED_PARAM: signal param stop_or_fraction is not used (exit.stop is PREMIUM_PCT).",
)

_TARGET_R = re.compile(r"(\d+(?:\.\d+)?)\s*R\b")


@dataclass(frozen=True, slots=True)
class OrbParams:
    spec_id: str
    spec_version: str
    or_minutes: int
    theta_v: Decimal
    entry_start: time
    entry_end: time
    time_exit: time
    max_hold: timedelta
    stop_pct: Decimal
    stop_offset_ticks: int
    max_chase_ticks: int
    target_r: Decimal
    entry_slip_ticks: int
    stop_slip_ticks: int
    lookback_days: int = 20
    vix_minutes: int = 30

    @classmethod
    def from_spec(cls, spec: StrategySpec) -> OrbParams:
        p = spec.signal.params
        for name in ("or_window_minutes", "theta_v"):
            if name not in p:
                raise BacktestError(f"{spec.id}: signal param {name!r} missing (not an H01 spec)")
        if len(spec.legs) != 1 or spec.legs[0].right is not LegRight.SIGNAL:
            raise BacktestError(f"{spec.id}: H01 needs exactly one BUY leg with right SIGNAL")
        if spec.exit.stop.method is not StopMethod.PREMIUM_PCT:
            raise BacktestError(f"{spec.id}: this implementation supports PREMIUM_PCT stops only")
        m = _TARGET_R.search(spec.exit.profit_taking)
        if m is None:
            raise BacktestError(f"{spec.id}: cannot read an 'N R' target from profit_taking")
        orw = p["or_window_minutes"].value
        if orw != orw.to_integral_value() or orw <= 0:
            raise BacktestError("or_window_minutes must be a positive whole number")
        return cls(
            spec_id=spec.id,
            spec_version=spec.version,
            or_minutes=int(orw),
            theta_v=p["theta_v"].value,
            entry_start=spec.entry.window_start,
            entry_end=spec.entry.window_end,
            time_exit=spec.exit.time_exit,
            max_hold=timedelta(minutes=spec.exit.max_holding_minutes),
            stop_pct=spec.exit.stop.value,
            stop_offset_ticks=spec.exit.stop.limit_offset_ticks,
            max_chase_ticks=spec.entry.order.max_chase_ticks,
            target_r=Decimal(m.group(1)),
            entry_slip_ticks=spec.expected_slippage.entry_ticks,
            stop_slip_ticks=spec.expected_slippage.stop_ticks,
        )


@dataclass(slots=True)
class _Trade:
    day: date
    key: str
    contract: OptionContract
    signal_at: datetime
    first_limit: Decimal
    state: str = "ENTRY_WORKING"
    tag: str = ""
    submitted_at: datetime | None = None
    attempts: int = 0
    fill_price: Decimal | None = None
    fill_ts: datetime | None = None
    stop_trigger: Decimal | None = None
    stop_limit: Decimal | None = None
    stop_tag: str = ""
    target: Decimal | None = None
    exit_reason: str | None = None
    exit_attempts: int = 0
    record: dict[str, Any] = field(default_factory=dict)


class OrbH01Strategy:
    def __init__(
        self,
        params: OrbParams,
        *,
        fut_key: str,
        index_key: str,
        vix_key: str,
        contracts: Mapping[str, OptionContract],
        costs: CostModel,
        plan_id: str,
        risk_budget: Decimal,
        tick: Decimal = Decimal("0.05"),
        interval: timedelta = timedelta(minutes=1),
        hands_off_from: time = time(14, 50),
    ) -> None:
        if risk_budget <= 0:
            raise BacktestError("risk_budget must be > 0")
        self.p = params
        self.name = f"{params.spec_id}@{params.spec_version}"
        self._fut, self._idx, self._vix = fut_key, index_key, vix_key
        self._costs, self._plan, self._budget = costs, plan_id, risk_budget
        self._tick, self._iv, self._hands_off = tick, interval, hands_off_from
        self._contracts = contracts
        self._chain: dict[tuple[date, OptionRight], list[OptionContract]] = {}
        for c in contracts.values():
            self._chain.setdefault((c.expiry, c.right), []).append(c)
        self._expiries = sorted({c.expiry for c in contracts.values()})
        self._vol_hist: dict[time, list[tuple[date, int]]] = {}
        self._day: date | None = None
        self._or_bars: list[Bar] = []
        self._or: tuple[Decimal, Decimal] | None = None
        self._done_for_day = False
        self._last_fut: datetime | None = None
        self._pending: tuple[datetime, Decimal, int] | None = None  # (5-min end, close, volume)
        self._trade: _Trade | None = None
        self._status: dict[str, OrderStatus] = {}
        self._n_reports = 0
        self.counters: dict[str, int] = {}
        self.trades: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def _count(self, k: str) -> None:
        self.counters[k] = self.counters.get(k, 0) + 1

    def _floor(self, p: Decimal) -> Decimal:
        return (p / self._tick).to_integral_value(rounding=ROUND_FLOOR) * self._tick

    def _ceil(self, p: Decimal) -> Decimal:
        return (p / self._tick).to_integral_value(rounding=ROUND_CEILING) * self._tick

    def _new_day(self, d: date) -> None:
        if self._trade is not None:
            self._finish(self._trade, "SESSION_END")
        self._day, self._or_bars, self._or = d, [], None
        self._done_for_day, self._pending = False, None

    def _finish(self, t: _Trade, reason: str) -> None:
        t.state = "DONE"
        t.record.setdefault("outcome", reason)
        self.trades.append(t.record)
        self._trade = None

    def _order(
        self,
        c: OptionContract,
        side: OrderSide,
        qty: int,
        typ: SimOrderType,
        lim: Decimal,
        trig: Decimal | None,
        tag: str,
    ) -> PlaceOrder:
        return PlaceOrder(c.instrument_key, c.underlying, c.right, side, qty, c.lot_size, typ, lim, trig, tag)

    # ------------------------------------------------------------------ strategy protocol
    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
        now = view.now
        d = now.astimezone(IST).date()
        if d != self._day:
            self._new_day(d)
        for r in ctx.reports[self._n_reports :]:
            self._status[r.order_id] = r.status
        self._n_reports = len(ctx.reports)
        out: list[Intent] = []
        self._ingest_futures(view)
        if self._pending is not None:
            self._evaluate(view, ctx, out)
        if self._trade is not None:
            self._manage(view, ctx, out)
        return out

    # ------------------------------------------------------------------ signal
    def _ingest_futures(self, view: MarketView) -> None:
        b = view.last_bar(self._fut)
        if b is None or b.start == self._last_fut:
            return
        self._last_fut = b.start
        st = b.start.astimezone(IST)
        assert self._day is not None
        open_ = datetime.combine(self._day, time(9, 15), IST)
        if open_ <= st < open_ + timedelta(minutes=self.p.or_minutes):
            self._or_bars.append(b)
            if st == open_ + timedelta(minutes=self.p.or_minutes) - self._iv:
                if len(self._or_bars) == self.p.or_minutes:
                    self._or = (max(x.high for x in self._or_bars), min(x.low for x in self._or_bars))
                else:
                    self._count("OR_INCOMPLETE")
            return
        end = st + self._iv
        if end.minute % 5 or not (self.p.entry_start <= end.time() <= self.p.entry_end):
            return
        five = [x for x in view.bars(self._fut, 5) if end - timedelta(minutes=5) <= x.start < end]
        if len(five) != 5:
            self._count("FIVE_MIN_INCOMPLETE")
            return
        vol = sum(x.volume for x in five)
        slot = end.time()
        if not self._done_for_day and self._trade is None and self._or is not None:
            self._pending = (end, b.close, vol)
        self._vol_hist.setdefault(slot, []).append((self._day, vol))

    def _vol_ratio(self, slot: time, vol: int) -> Decimal | None:
        hist = [v for dd, v in self._vol_hist.get(slot, []) if dd != self._day][-self.p.lookback_days :]
        if len(hist) < self.p.lookback_days:
            self._count("INSUFFICIENT_HISTORY")
            return None
        med = Decimal(statistics.median(hist))
        if med <= 0:
            self._count("ZERO_MEDIAN_VOLUME")
            return None
        return Decimal(vol) / med

    def _evaluate(self, view: MarketView, ctx: StrategyContext, out: list[Intent]) -> None:
        assert self._pending is not None and self._or is not None and self._day is not None
        end, close, vol = self._pending
        last_start = end - self._iv
        if view.now >= end + self._iv:  # inputs for this close never all arrived
            self._count("INPUTS_MISSING")
            self._pending = None
            return
        vix_bars = view.bars(self._vix, self.p.vix_minutes + 2)
        idx = view.last_bar(self._idx)
        if not vix_bars or vix_bars[-1].start != last_start or idx is None or idx.start != last_start:
            return  # wait for the same-minute index/VIX bars (same availability time, later in feed order)
        or_high, or_low = self._or
        right = OptionRight.CE if close > or_high else OptionRight.PE if close < or_low else None
        if right is None:
            self._count("NO_BREAK")
            self._pending = None
            return
        ratio = self._vol_ratio(end.time(), vol)
        if ratio is None:
            self._pending = None
            return
        if ratio < self.p.theta_v:
            self._count("LOW_VOLUME")
            self._pending = None
            return
        ago = [x for x in vix_bars if x.start == last_start - timedelta(minutes=self.p.vix_minutes)]
        if not ago:
            self._count("VIX_HISTORY_MISSING")
            self._pending = None
            return
        dvix = vix_bars[-1].close - ago[0].close
        if dvix < 0:
            self._count("VIX_FALLING")
            self._pending = None
            return
        # qualifying close: the day's only attempt from here on
        spot = idx.close
        exp = next((e for e in self._expiries if e > self._day), None)
        rec: dict[str, Any] = {
            "day": self._day,
            "signal_at": end,
            "right": right.value,
            "or_high": or_high,
            "or_low": or_low,
            "close_5m": close,
            "vol_ratio": ratio.quantize(Decimal("0.001")),
            "dvix_30m": dvix,
            "spot": spot,
        }
        if exp is None:
            self._reject_signal(rec, "NO_CONTRACT_DTE_GE_1")
            return
        chain = sorted(self._chain.get((exp, right), []), key=lambda c: (abs(c.strike - spot), c.strike))
        if not chain:
            self._reject_signal(rec, "NO_CONTRACT_DTE_GE_1")
            return
        c = chain[0]
        ob = view.last_bar(c.instrument_key)
        if ob is None or ob.start != last_start:
            return  # the option bar of this minute is later in feed order; retried until stale
        mid = ob.close
        lim = self._ceil(mid) + self._tick
        rec.update(contract=c.instrument_key, expiry=exp, strike=c.strike, proxy_mid=mid, entry_limit=lim)
        risk = self._risk_at_stop(lim, c)
        if risk is None:
            self._reject_signal(rec, "NO_TRADE_STOP_BELOW_TICK")
            return
        rec["risk_at_stop"] = risk
        if risk > self._budget:
            rec["risk_budget"] = self._budget
            self._reject_signal(rec, "NO_TRADE_RISK")
            return
        self._pending = None
        self._done_for_day = True
        t = _Trade(self._day, c.instrument_key, c, end, lim, record=rec)
        self._trade = t
        self._count("ENTRY_SENT")
        self._send_entry(t, lim, view.now, out)

    def _reject_signal(self, rec: dict[str, Any], why: str) -> None:
        self._count(why)
        rec["outcome"] = why
        self.trades.append(rec)
        self._pending = None
        self._done_for_day = True

    def _stops(self, entry: Decimal) -> tuple[Decimal, Decimal] | None:
        trig = self._floor(entry * (1 - self.p.stop_pct / 100))
        lim = trig - self.p.stop_offset_ticks * self._tick
        return None if lim < self._tick else (trig, lim)

    def _risk_at_stop(self, entry: Decimal, c: OptionContract) -> Decimal | None:
        st = self._stops(entry)
        if st is None:
            return None
        _, stop_lim = st
        assert self._day is not None
        rt = (
            self._costs.order_charges(Side.BUY, entry, c.lot_size, self._day, self._plan).total
            + self._costs.order_charges(Side.SELL, stop_lim, c.lot_size, self._day, self._plan).total
        )
        slip = (self.p.entry_slip_ticks + self.p.stop_slip_ticks) * self._tick * c.lot_size
        return (entry - stop_lim) * c.lot_size + rt + slip

    # ------------------------------------------------------------------ order management
    def _send_entry(self, t: _Trade, lim: Decimal, now: datetime, out: list[Intent]) -> None:
        t.attempts += 1
        t.tag = f"H01|{t.day.isoformat()}|ENTRY{t.attempts}"
        t.submitted_at = now
        t.state = "ENTRY_WORKING"
        t.record["entry_attempts"] = t.attempts
        t.record["last_entry_limit"] = lim
        out.append(self._order(t.contract, OrderSide.BUY, t.contract.lot_size, SimOrderType.LIMIT, lim, None, t.tag))

    def _send_exit(self, t: _Trade, view: MarketView, now: datetime, out: list[Intent], qty: int) -> None:
        last = view.last_bar(t.key)
        assert last is not None  # we hold it, so we saw its bars
        t.exit_attempts += 1
        px = max(self._tick, self._floor(last.close) - (1 + 2 * (t.exit_attempts - 1)) * self._tick)
        t.tag = f"H01|{t.day.isoformat()}|EXIT{t.exit_attempts}"
        t.submitted_at = now
        t.state = "EXIT_WORKING"
        out.append(self._order(t.contract, OrderSide.SELL, qty, SimOrderType.LIMIT, px, None, t.tag))

    def _fill_of(self, ctx: StrategyContext, tag: str) -> tuple[Decimal, datetime] | None:
        for f in reversed(ctx.fills):
            if f.tag == tag:
                return f.price, f.ts
        return None

    def _manage(self, view: MarketView, ctx: StrategyContext, out: list[Intent]) -> None:
        t = self._trade
        assert t is not None
        now = view.now
        pos = ctx.position(t.key)
        if t.tag in ctx.rejected:
            t.record["rejected"] = ctx.rejected[t.tag]
            if pos == 0:
                self._finish(t, "ORDER_REJECTED")
            return
        if now.astimezone(IST).time() >= self._hands_off:
            if pos > 0 and t.state != "ENGINE_FLATTEN":
                t.state = "ENGINE_FLATTEN"
                t.record["exit_reason"] = t.exit_reason or "ENGINE_FLATTEN"
            elif pos == 0:
                self._finish(t, "CLOSED" if t.fill_price is not None else "ENTRY_UNFILLED")
            return
        oid = ctx.accepted.get(t.tag)
        if t.state in ("ENTRY_WORKING", "ENTRY_CANCELLING"):
            if pos > 0:
                fp = self._fill_of(ctx, t.tag)
                assert fp is not None
                t.fill_price, t.fill_ts = fp
                stops = self._stops(t.fill_price)
                assert stops is not None  # the entry limit passed the same check
                t.stop_trigger, t.stop_limit = stops
                t.target = t.fill_price + self.p.target_r * (t.fill_price - t.stop_trigger)
                t.record.update(
                    entry_fill=t.fill_price,
                    entry_ts=t.fill_ts,
                    stop_trigger=t.stop_trigger,
                    stop_limit=t.stop_limit,
                    target=t.target,
                )
                t.stop_tag = t.tag = f"H01|{t.day.isoformat()}|STOP"
                t.state = "PROTECTED"
                out.append(
                    self._order(
                        t.contract, OrderSide.SELL, pos, SimOrderType.SL_LIMIT, t.stop_limit, t.stop_trigger, t.tag
                    )
                )
                return
            if t.state == "ENTRY_WORKING" and oid is not None and t.submitted_at is not None:
                if now >= t.submitted_at + 2 * self._iv:  # one full bar was eligible
                    t.state = "ENTRY_CANCELLING"
                    out.append(CancelOrder(oid))
                return
            if t.state == "ENTRY_CANCELLING" and oid is not None and self._status.get(oid) is OrderStatus.CANCELLED:
                last = view.last_bar(t.key)
                in_window = now.astimezone(IST).time() <= self.p.entry_end
                if t.attempts > self.p.max_chase_ticks or last is None or not in_window:
                    self._finish(t, "ENTRY_UNFILLED")
                    return
                cap = t.first_limit + self.p.max_chase_ticks * self._tick
                lim = min(self._ceil(last.close) + self._tick, cap)
                risk = self._risk_at_stop(lim, t.contract)
                if risk is None or risk > self._budget:
                    self._finish(t, "ENTRY_UNFILLED_RISK")
                    return
                self._send_entry(t, lim, now, out)
            return
        if pos == 0:  # a sell filled (stop or exit)
            reason = t.exit_reason if t.state != "PROTECTED" else "STOP"
            t.record["exit_reason"] = reason
            fp = self._fill_of(ctx, t.tag)
            if fp is not None:
                t.record.update(exit_fill=fp[0], exit_ts=fp[1], exit_attempts=t.exit_attempts)
            self._finish(t, "CLOSED")
            return
        if t.state == "PROTECTED":
            assert t.fill_ts is not None and t.target is not None
            last = view.last_bar(t.key)
            reason = None
            if last is not None and last.close >= t.target:
                reason = "TARGET"
            elif now.astimezone(IST).time() >= self.p.time_exit:
                reason = "TIME_EXIT"
            elif now >= t.fill_ts + self.p.max_hold:
                reason = "MAX_HOLD"
            if reason is not None and oid is not None:
                t.exit_reason = reason
                t.record["exit_decided_at"] = now
                t.state = "EXIT_CANCEL_STOP"
                out.append(CancelOrder(oid))
            return
        if t.state in ("EXIT_CANCEL_STOP", "EXIT_CANCELLING"):
            if oid is not None and self._status.get(oid) is OrderStatus.CANCELLED:
                self._send_exit(t, view, now, out, pos)
            return
        if t.state == "EXIT_WORKING" and oid is not None and t.submitted_at is not None:
            if now >= t.submitted_at + 2 * self._iv:
                t.state = "EXIT_CANCELLING"
                out.append(CancelOrder(oid))


def h01_metadata(params: OrbParams) -> dict[str, object]:
    return {
        "spec": params.spec_id,
        "spec_version": params.spec_version,
        "params": {
            "or_minutes": params.or_minutes,
            "theta_v": params.theta_v,
            "stop_pct": params.stop_pct,
            "target_r": params.target_r,
            "lookback_days": params.lookback_days,
        },
        "deviations": list(H01_DEVIATIONS),
    }

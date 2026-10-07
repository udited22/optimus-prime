"""Shared machinery for the long-option strategy library (docs/research/strategy-hypotheses.md "Strategy library").

A library strategy is a *signal plug-in* plus this base. The base owns everything that must be the same for every
strategy and that the mandate cares about:

* it reads its parameters from the authored StrategySpec (never hard-coded): entry window, stop, target, time exit,
  maximum hold, chase ticks, slippage allowance, the legs and the signal params;
* it runs the K-11 regime classifier on the index bars and, when ``gate_regime`` is on, refuses a signal whose regime
  the spec does not permit (``spec.regime_policy.regime_permits``). The classifier is UNVALIDATED, so in research
  runs the labels are used as they are and the run metadata says so;
* entries are LIMIT orders at the last option close + 1 tick, re-quoted up to ``max_chase_ticks`` times (one bar each,
  then cancel); sizing is one lot per leg, and only when the summed ``risk_at_stop`` fits the risk budget;
* every filled leg immediately gets a protective SL-LIMIT at the spec's premium stop;
* exits: the plug-in's invalidation (PRICE_LEVEL / TIME / VOLATILITY rules), the spec's REGIME_CHANGE invalidation
  rules, the "N R" target on the combined premium, the time exit and the maximum hold. An exit cancels the stop and
  sends a sell-to-close LIMIT, re-priced lower each bar until filled;
* from 14:50 the engine's forced flatten owns any open position (hands off).

Long only (OD-006): the only SELL orders are the protective stop and sell-to-close exits of quantity we hold.
Decisions are taken once per closed 1-minute index bar; the feed delivers that minute's option bars first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Protocol

from project100c.backtest.engine import StrategyContext
from project100c.backtest.feed import MarketView
from project100c.backtest.types import CancelOrder, Intent, OptionContract, OrderStatus, PlaceOrder, SimOrderType
from project100c.calendar import ExpiryCalendar
from project100c.core_types import OptionRight, OrderSide
from project100c.costs.model import CostModel, Side
from project100c.errors import BacktestError
from project100c.market_types import Bar
from project100c.regime import EventCalendar, RegimeClassifier, RegimeConfig, RegimeLabel
from project100c.sessions.model import IST
from project100c.spec.models import InvalidationAction, LegRight, StopMethod, StrategySpec
from project100c.spec.regime_policy import RegimePolicy, regime_permits, triggered_regime_invalidations

_TARGET_R = re.compile(r"(\d+(?:\.\d+)?)\s*R\b")
_TARGET_PTS = re.compile(r"(\d+(?:\.\d+)?)\s*PTS\b")
ONE_MIN = timedelta(minutes=1)
SESSION_OPEN = time(9, 15)


@dataclass(frozen=True, slots=True)
class LibraryParams:
    spec_id: str
    spec_version: str
    entry_start: time
    entry_end: time
    time_exit: time
    max_hold: timedelta
    stop_pct: Decimal  # PREMIUM_PCT: % of the entry; PREMIUM_POINTS: premium points below the entry
    stop_offset_ticks: int
    max_chase_ticks: int
    target_r: Decimal | None
    entry_slip_ticks: int
    stop_slip_ticks: int
    legs: tuple[LegRight, ...]
    signal: dict[str, Decimal]
    dte_min: int
    otm_steps: int
    max_trades_per_day: int
    stop_points: bool = False  # the spec stop is PREMIUM_POINTS (stop_pct then holds the points)
    target_pts: Decimal | None = None  # "N PTS": exit when the held premium is N points above the entry
    # premium-aware stop (signal param budget_stop_min_pct): the widest stop, at most the spec's %, that keeps the
    # summed risk_at_stop inside the per-trade budget; narrower than budget_stop_min_pct -> NO TRADE
    budget_stop_min_pct: Decimal | None = None
    max_losses_per_day: int = 0  # signal param: stand down for the day after N losing trades (0 = no such rule)

    @classmethod
    def from_spec(cls, spec: StrategySpec) -> LibraryParams:
        if spec.exit.stop.method not in (StopMethod.PREMIUM_PCT, StopMethod.PREMIUM_POINTS):
            raise BacktestError(f"{spec.id}: the library base supports PREMIUM_PCT and PREMIUM_POINTS stops only")
        m = _TARGET_R.search(spec.exit.profit_taking)
        mp = _TARGET_PTS.search(spec.exit.profit_taking)
        sig = {k: v.value for k, v in spec.signal.params.items()}
        points = spec.exit.stop.method is StopMethod.PREMIUM_POINTS
        if points and "budget_stop_min_pct" in sig:
            raise BacktestError(f"{spec.id}: a premium-aware stop needs a PREMIUM_PCT stop")
        return cls(
            spec_id=spec.id,
            spec_version=spec.version,
            entry_start=spec.entry.window_start,
            entry_end=spec.entry.window_end,
            time_exit=spec.exit.time_exit,
            max_hold=timedelta(minutes=spec.exit.max_holding_minutes),
            stop_pct=spec.exit.stop.value,
            stop_offset_ticks=spec.exit.stop.limit_offset_ticks,
            max_chase_ticks=spec.entry.order.max_chase_ticks,
            target_r=Decimal(m.group(1)) if m else None,
            entry_slip_ticks=spec.expected_slippage.entry_ticks,
            stop_slip_ticks=spec.expected_slippage.stop_ticks,
            legs=tuple(leg.right for leg in spec.legs),
            signal=sig,
            dte_min=0 if spec.selection.allow_zero_dte else 1,
            otm_steps=int(sig.get("otm_steps", Decimal(0))),
            # a multi-leg trade (the OD-013 long straddle) is ONE entry: the Governor does not count its second leg
            max_trades_per_day=spec.entry.max_entries_per_day,
            stop_points=points,
            target_pts=Decimal(mp.group(1)) if mp else None,
            budget_stop_min_pct=sig.get("budget_stop_min_pct"),
            max_losses_per_day=int(sig.get("max_losses_per_day", Decimal(0))),
        )


@dataclass(slots=True)
class Session:
    """What a plug-in may look at: today's closed bars (nothing later), the regime label and the day flags."""

    day: date
    prev_close: Decimal | None
    idx: list[Bar] = field(default_factory=list)
    fut: list[Bar | None] = field(default_factory=list)  # aligned with idx (None = no futures bar that minute)
    vix: list[Bar | None] = field(default_factory=list)
    label: RegimeLabel | None = None
    expiry_day: bool = False
    event_day: bool = False
    state: dict[str, Any] = field(default_factory=dict)  # the plug-in's own per-day memory
    # option access for plug-ins that read the chain (H19, H22): the market view of the current minute, the
    # contracts by (expiry, right), the listed expiries and the day's pre-computed point-in-time features
    view: MarketView | None = None
    chain: Mapping[tuple[date, OptionRight], Sequence[OptionContract]] = field(default_factory=dict)
    expiries: Sequence[date] = ()
    features: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def open(self) -> Decimal:
        return self.idx[0].open

    @property
    def last(self) -> Bar:
        return self.idx[-1]

    @property
    def end(self) -> datetime:
        """When the latest bar closed (= now, at decision time)."""
        return self.idx[-1].start + ONE_MIN

    @property
    def minutes(self) -> int:
        return len(self.idx)

    def at(self, hh: int, mm: int) -> datetime:
        return datetime.combine(self.day, time(hh, mm), IST)

    def window(self, start: datetime, end: datetime) -> list[Bar]:
        return [b for b in self.idx if start <= b.start < end]

    def hi_lo(self, bars: Sequence[Bar]) -> tuple[Decimal, Decimal]:
        return max(b.high for b in bars), min(b.low for b in bars)

    def vix_now(self) -> Decimal | None:
        return next((v.close for v in reversed(self.vix) if v is not None), None)

    def vix_ago(self, minutes: int) -> Decimal | None:
        i = len(self.vix) - 1 - minutes
        v = self.vix[i] if 0 <= i < len(self.vix) else None
        return None if v is None else v.close

    def option_bar(self, key: str) -> Bar | None:
        """The option's bar of the CURRENT minute (closed with the index bar), else None (no stale prices)."""
        if self.view is None:
            return None
        b = self.view.last_bar(key)
        return b if b is not None and b.start == self.last.start else None

    def expiry(self, dte_min: int, nth: int = 0) -> date | None:
        """The ``nth`` listed expiry with at least ``dte_min`` calendar days to go (0 = nearest, 1 = next)."""
        live = [e for e in self.expiries if (e - self.day).days >= dte_min]
        return live[nth] if nth < len(live) else None

    def near(self, expiry: date, right: OptionRight, spot: Decimal, k: int = 0) -> list[OptionContract]:
        """Contracts of one expiry and right sorted by strike, the ``k`` strikes either side of the nearest to spot
        (k = 0: just the nearest)."""
        chain = sorted(self.chain.get((expiry, right), ()), key=lambda c: c.strike)
        if not chain:
            return []
        i = min(range(len(chain)), key=lambda j: (abs(chain[j].strike - spot), chain[j].strike))
        return chain[max(0, i - k) : i + k + 1]

    def vwap(self, *, use_futures_volume: bool) -> tuple[Decimal, str]:
        """Session VWAP of the index typical price. Weighted by same-minute futures volume when asked and present
        (the futures-volume dependency), otherwise an equal-weight TWAP proxy (the substitute)."""
        pv, vol, tw = Decimal(0), 0, Decimal(0)
        for b, f in zip(self.idx, self.fut, strict=True):
            tp = (b.high + b.low + b.close) / 3
            tw += tp
            if use_futures_volume and f is not None and f.volume > 0:
                pv += tp * f.volume
                vol += f.volume
        if use_futures_volume and vol > 0:
            return pv / vol, "FUT_VOLUME"
        return tw / len(self.idx), "TWAP_PROXY"


@dataclass(frozen=True, slots=True)
class EntrySignal:
    rights: tuple[OptionRight, ...]
    reason: str
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OpenTrade:
    """Read-only view a plug-in gets when asked whether its thesis still holds."""

    signal: EntrySignal
    signal_at: datetime
    spot_at_signal: Decimal
    first_fill_at: datetime | None


class SignalPlugin(Protocol):
    code: str
    uses_futures_volume: bool
    deviations: tuple[str, ...]

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None: ...

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None: ...


class BasePlugin:
    """Defaults for plug-ins: no futures volume, no invalidation beyond the spec's generic exits."""

    code = "BASE"
    uses_futures_volume = False
    deviations: tuple[str, ...] = ()

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        return None


@dataclass(slots=True)
class _Leg:
    contract: OptionContract
    first_limit: Decimal
    state: str = "ENTRY_WORKING"
    tag: str = ""
    submitted_at: datetime | None = None
    attempts: int = 0
    fill_price: Decimal | None = None
    fill_ts: datetime | None = None
    stop_trigger: Decimal | None = None
    stop_limit: Decimal | None = None
    stop_pct: Decimal | None = None  # the premium-aware stop of this trade (None = the spec's)
    exit_attempts: int = 0
    exit_fill: Decimal | None = None
    outcome: str = ""

    @property
    def key(self) -> str:
        return self.contract.instrument_key

    @property
    def terminal(self) -> bool:
        return self.state in ("CLOSED", "UNFILLED", "REJECTED")


@dataclass(slots=True)
class _Trade:
    day: date
    n: int
    signal: EntrySignal
    signal_at: datetime
    spot: Decimal
    legs: list[_Leg]
    exit_reason: str | None = None
    exit_decided_at: datetime | None = None
    stand_down: bool = False
    record: dict[str, Any] = field(default_factory=dict)


class LongOptionStrategy:
    def __init__(
        self,
        spec: StrategySpec,
        plugin: SignalPlugin,
        *,
        index_key: str,
        fut_key: str,
        vix_key: str,
        contracts: Mapping[str, OptionContract],
        costs: CostModel,
        plan_id: str,
        risk_budget: Decimal,
        regime: RegimeConfig,
        expiries: ExpiryCalendar | None = None,
        events: EventCalendar | None = None,
        gate_regime: bool = True,
        tick: Decimal = Decimal("0.05"),
        strike_step: int = 50,
        hands_off_from: time = time(14, 50),
        prev_close: Decimal | None = None,  # the close before the first session in the feed (for its gap)
        daily_features: Mapping[date, Mapping[str, Decimal]] | None = None,  # point-in-time, e.g. H22's gamma rank
    ) -> None:
        if risk_budget <= 0:
            raise BacktestError("risk_budget must be > 0")
        self.spec = spec
        self.p = LibraryParams.from_spec(spec)
        self.plugin = plugin
        self.name = f"{spec.id}@{spec.version}"
        self._idx, self._fut, self._vix = index_key, fut_key, vix_key
        self._costs, self._plan, self._budget = costs, plan_id, risk_budget
        self._tick, self._step, self._hands_off = tick, strike_step, hands_off_from
        self._policy = RegimePolicy.from_spec(spec)
        self._gate = gate_regime
        self._clf = RegimeClassifier(regime, expiries=expiries, events=events)
        self._expiries_cal = expiries
        self._chain: dict[tuple[date, OptionRight], list[OptionContract]] = {}
        for c in contracts.values():
            self._chain.setdefault((c.expiry, c.right), []).append(c)
        self._expiries = sorted({c.expiry for c in contracts.values()})
        self._features = daily_features or {}
        self._s: Session | None = None
        self._prev_close: Decimal | None = prev_close
        self._last_idx: datetime | None = None
        self._trade: _Trade | None = None
        self._trades_today = 0
        self._losses_today = 0
        self._stand_down = False
        self._status: dict[str, OrderStatus] = {}
        self._n_reports = 0
        self.counters: dict[str, int] = {}
        self.trades: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def _count(self, k: str) -> None:
        self.counters[k] = self.counters.get(k, 0) + 1

    def _floor(self, p: Decimal) -> Decimal:
        return (p / self._tick).to_integral_value(rounding=ROUND_FLOOR) * self._tick

    def _ceil(self, p: Decimal) -> Decimal:
        return (p / self._tick).to_integral_value(rounding=ROUND_CEILING) * self._tick

    def _stops(self, entry: Decimal, pct: Decimal | None = None) -> tuple[Decimal, Decimal] | None:
        if self.p.stop_points:
            trig = self._floor(entry - self.p.stop_pct)
        else:
            trig = self._floor(entry * (1 - (self.p.stop_pct if pct is None else pct) / 100))
        lim = trig - self.p.stop_offset_ticks * self._tick
        return None if lim < self._tick else (trig, lim)

    def _risk_at_stop(self, entry: Decimal, c: OptionContract, d: date, pct: Decimal | None = None) -> Decimal | None:
        st = self._stops(entry, pct)
        if st is None:
            return None
        _, stop_lim = st
        rt = (
            self._costs.order_charges(Side.BUY, entry, c.lot_size, d, self._plan).total
            + self._costs.order_charges(Side.SELL, stop_lim, c.lot_size, d, self._plan).total
        )
        slip = (self.p.entry_slip_ticks + self.p.stop_slip_ticks) * self._tick * c.lot_size
        return (entry - stop_lim) * c.lot_size + rt + slip

    def _order(self, c: OptionContract, side: OrderSide, qty: int, typ: SimOrderType, lim: Decimal,
               trig: Decimal | None, tag: str) -> PlaceOrder:  # fmt: skip
        return PlaceOrder(c.instrument_key, c.underlying, c.right, side, qty, c.lot_size, typ, lim, trig, tag)

    def _tag(self, t: _Trade, leg: _Leg, what: str) -> str:
        return f"{self.plugin.code}|{t.day.isoformat()}|T{t.n}|{leg.contract.right.value}|{what}"

    # ------------------------------------------------------------------ day / bars
    def _new_day(self, d: date) -> None:
        if self._trade is not None:
            self._finish(self._trade, "SESSION_END")
        if self._s is not None and self._s.idx:
            self._prev_close = self._s.idx[-1].close
        self._s = Session(d, self._prev_close, chain=self._chain, expiries=self._expiries,
                          features=self._features.get(d, {}))  # fmt: skip
        self._clf.start_session(d, self._prev_close)
        if self._expiries_cal is not None:
            self._s.expiry_day = self._expiries_cal.is_expiry_day(d)
        self._trades_today = 0
        self._losses_today = 0
        self._stand_down = False

    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
        for r in ctx.reports[self._n_reports :]:
            self._status[r.order_id] = r.status
        self._n_reports = len(ctx.reports)
        b = view.last_bar(self._idx)
        if b is None or b.start == self._last_idx or view.now < b.start + ONE_MIN:
            return []
        self._last_idx = b.start
        d = b.start.astimezone(IST).date()
        if self._s is None or d != self._s.day:
            self._new_day(d)
        s = self._s
        assert s is not None
        f = view.last_bar(self._fut)
        v = view.last_bar(self._vix)
        f = f if f is not None and f.start == b.start else None
        v = v if v is not None and v.start == b.start else None
        s.view = view
        s.idx.append(b)
        s.fut.append(f)
        s.vix.append(v)
        s.label = self._clf.on_bar(b, vix=None if v is None else v.close, volume=None if f is None else f.volume)
        s.event_day = s.label.event_day
        out: list[Intent] = []
        if self._trade is not None:
            self._manage(view, ctx, out)
        sig = self.plugin.on_bar(s, self.p.signal)
        if sig is not None:
            self._on_signal(sig, view, out, ctx)
        return out

    # ------------------------------------------------------------------ entries
    def _on_signal(self, sig: EntrySignal, view: MarketView, out: list[Intent], ctx: StrategyContext) -> None:
        s = self._s
        assert s is not None
        now_t = s.end.astimezone(IST).time()
        rec: dict[str, Any] = {
            "day": s.day, "at": s.end, "decided_at": view.now, "inputs_end": s.end,  # V1 look-ahead stamps
            "reason": sig.reason, "rights": [r.value for r in sig.rights],
            "facts": dict(sig.facts), "spot": s.last.close,
            "regime_tags": sorted(t.value for t in s.label.tags()) if s.label else [],
            "classifier": self._clf.version, "classifier_status": self._clf.cfg.status,
        }  # fmt: skip
        self.signals.append(rec)

        def skip(why: str) -> None:
            self._count(why)
            rec["outcome"] = why

        if self._trade is not None:
            return skip("IN_TRADE")
        if self._stand_down:
            return skip("STOOD_DOWN")
        if not (self.p.entry_start <= now_t <= self.p.entry_end):
            return skip("OUTSIDE_ENTRY_WINDOW")
        if self._trades_today >= self.p.max_trades_per_day:
            return skip("MAX_TRADES_TODAY")
        if len(sig.rights) != len(self.p.legs):
            raise BacktestError(f"{self.p.spec_id}: signal has {len(sig.rights)} legs, the spec {len(self.p.legs)}")
        if self._gate:
            verdict = regime_permits(self._policy, s.label.tags() if s.label else frozenset())
            rec["regime_check"] = verdict.check.value
            if not verdict.permitted:
                rec["regime_detail"] = verdict.detail
                return skip(verdict.check.value)
        spot = s.last.close
        exp = next((e for e in self._expiries if (e - s.day).days >= self.p.dte_min), None)
        if exp is None:
            return skip("NO_EXPIRY")
        legs: list[_Leg] = []
        risk = Decimal(0)
        for right in sig.rights:
            target = spot + (
                self.p.otm_steps * self._step if right is OptionRight.CE else -self.p.otm_steps * self._step
            )
            chain = sorted(self._chain.get((exp, right), []), key=lambda c: (abs(c.strike - target), c.strike))
            if not chain:
                return skip("NO_CONTRACT")
            c = chain[0]
            if ctx.position(c.instrument_key) != 0:
                # a residual from an earlier trade (the engine could not flatten it before 15:00 and it carried,
                # RESIDUAL_AT_CLOSE): never stack a new trade on it; positions are per contract, not per trade
                return skip("NO_TRADE_RESIDUAL_POSITION")
            ob = view.last_bar(c.instrument_key)
            if ob is None or ob.start != s.last.start:
                return skip("NO_OPTION_BAR")
            lim = self._ceil(ob.close) + self._tick
            r = self._risk_at_stop(lim, c, s.day)
            if r is None:
                return skip("NO_TRADE_STOP_BELOW_TICK")
            risk += r
            legs.append(_Leg(c, lim))
        if self.p.budget_stop_min_pct is not None:
            fit = self._budget_stop(legs, s.day)
            if fit is None:
                rec["risk_budget"] = self._budget
                return skip("NO_TRADE_STOP_TOO_TIGHT")
            pct, risk = fit
            for lg in legs:
                lg.stop_pct = pct
            rec["stop_pct"] = pct
        rec["risk_at_stop"] = risk
        if risk > self._budget:
            rec["risk_budget"] = self._budget
            return skip("NO_TRADE_RISK")
        self._trades_today += 1
        t = _Trade(s.day, self._trades_today, sig, s.end, spot, legs)
        t.record = {
            **rec,
            "expiry": exp,
            "legs": [lg.key for lg in legs],
            "entry_limits": [lg.first_limit for lg in legs],
        }
        rec["outcome"] = "ENTRY_SENT"
        self._trade = t
        self._count("ENTRY_SENT")
        for lg in legs:
            self._send_entry(t, lg, lg.first_limit, s.end, out)

    def _budget_stop(self, legs: Sequence[_Leg], d: date) -> tuple[Decimal, Decimal] | None:
        """The widest stop % (spec value down to budget_stop_min_pct, in 0.5% steps) whose summed risk fits."""
        lo = self.p.budget_stop_min_pct
        assert lo is not None
        pct = self.p.stop_pct
        while pct >= lo:
            rs = [self._risk_at_stop(lg.first_limit, lg.contract, d, pct) for lg in legs]
            if all(r is not None for r in rs):
                total = sum((r for r in rs if r is not None), Decimal(0))
                if total <= self._budget:
                    return pct, total
            pct -= Decimal("0.5")
        return None

    def _send_entry(self, t: _Trade, lg: _Leg, lim: Decimal, now: datetime, out: list[Intent]) -> None:
        lg.attempts += 1
        lg.tag = self._tag(t, lg, f"ENTRY{lg.attempts}")
        lg.submitted_at = now
        lg.state = "ENTRY_WORKING"
        out.append(self._order(lg.contract, OrderSide.BUY, lg.contract.lot_size, SimOrderType.LIMIT, lim, None, lg.tag))

    def _send_exit(self, t: _Trade, lg: _Leg, view: MarketView, now: datetime, qty: int, out: list[Intent]) -> None:
        last = view.last_bar(lg.key)
        assert last is not None
        lg.exit_attempts += 1
        px = max(self._tick, self._floor(last.close) - (1 + 2 * (lg.exit_attempts - 1)) * self._tick)
        lg.tag = self._tag(t, lg, f"EXIT{lg.exit_attempts}")
        lg.submitted_at = now
        lg.state = "EXIT_WORKING"
        out.append(self._order(lg.contract, OrderSide.SELL, qty, SimOrderType.LIMIT, px, None, lg.tag))

    @staticmethod
    def _fill_of(ctx: StrategyContext, tag: str) -> tuple[Decimal, datetime] | None:
        for f in reversed(ctx.fills):
            if f.tag == tag:
                return f.price, f.ts
        return None

    @staticmethod
    def _filled_qty(ctx: StrategyContext, tag: str) -> int:
        """Quantity filled on this leg's own order tag (not the contract's net position)."""
        return sum(f.qty for f in ctx.fills if f.tag == tag)

    @staticmethod
    def _last_sell(ctx: StrategyContext, key: str) -> tuple[Decimal, datetime] | None:
        for f in reversed(ctx.fills):
            if f.instrument_key == key and f.side is OrderSide.SELL:
                return f.price, f.ts
        return None

    # ------------------------------------------------------------------ management
    def _finish(self, t: _Trade, outcome: str) -> None:
        t.record.update(
            outcome=outcome,
            exit_reason=t.exit_reason,
            legs_detail=[
                {"key": lg.key, "state": lg.state, "outcome": lg.outcome, "entry_fill": lg.fill_price,
                 "entry_ts": lg.fill_ts, "stop_trigger": lg.stop_trigger, "stop_limit": lg.stop_limit,
                 "exit_fill": lg.exit_fill, "entry_attempts": lg.attempts, "exit_attempts": lg.exit_attempts}
                for lg in t.legs
            ],
        )  # fmt: skip
        if t.stand_down:
            self._stand_down = True
        done = [lg for lg in t.legs if lg.fill_price is not None and lg.exit_fill is not None]
        if (
            done
            and sum((lg.exit_fill - lg.fill_price for lg in done if lg.exit_fill and lg.fill_price), Decimal(0)) < 0
        ):
            self._losses_today += 1
            if 0 < self.p.max_losses_per_day <= self._losses_today:
                self._stand_down = True  # discipline rule: no more entries today
        self.trades.append(t.record)
        self._trade = None

    def _manage(self, view: MarketView, ctx: StrategyContext, out: list[Intent]) -> None:
        t = self._trade
        s = self._s
        assert t is not None and s is not None
        now = s.end
        if now.astimezone(IST).time() >= self._hands_off:
            for lg in t.legs:
                pos = ctx.position(lg.key)
                if pos > 0 and lg.state != "FLATTEN":
                    lg.state = "FLATTEN"
                    t.exit_reason = t.exit_reason or "ENGINE_FLATTEN"
                elif pos == 0 and lg.state == "ENTRY_WORKING":
                    # a still-working entry must not fill into the flatten window: cancel it, settle it next bar
                    oid = ctx.accepted.get(lg.tag)
                    lg.state = "ENTRY_CANCELLING"
                    if oid is not None:
                        out.append(CancelOrder(oid))
                elif pos == 0 and lg.state == "ENTRY_CANCELLING":
                    oid = ctx.accepted.get(lg.tag)
                    if oid is None or self._status.get(oid) in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                        lg.state, lg.outcome = "UNFILLED", "ENTRY_UNFILLED"
                elif pos == 0 and not lg.terminal:
                    fp = (
                        self._fill_of(ctx, f"{lg.tag}")
                        if lg.state == "EXIT_WORKING"
                        else self._last_sell(ctx, lg.key)
                        if lg.state == "FLATTEN"
                        else None
                    )
                    lg.exit_fill = fp[0] if fp else lg.exit_fill
                    lg.outcome = "FLATTENED" if lg.fill_price is not None else "UNFILLED"
                    lg.state = "CLOSED" if lg.fill_price is not None else "UNFILLED"
            if all(lg.terminal for lg in t.legs):
                self._finish(t, "CLOSED" if any(lg.fill_price is not None for lg in t.legs) else "ENTRY_UNFILLED")
            return
        for lg in t.legs:
            self._manage_leg(t, lg, view, ctx, now, out)
        if all(lg.terminal for lg in t.legs):
            filled = any(lg.fill_price is not None for lg in t.legs)
            self._finish(t, "CLOSED" if filled else "ENTRY_UNFILLED")
            return
        # a straddle leg that never filled or was refused (e.g. MAX_LOTS under the 1-lot canary cap): the trade's
        # thesis needs both legs, so close the other
        if t.exit_reason is None and any(lg.state in ("UNFILLED", "REJECTED") for lg in t.legs):
            rejected = any(lg.state == "REJECTED" for lg in t.legs)
            self._decide_exit(t, "LEG_REJECTED" if rejected else "LEG_UNFILLED", now, ctx, out)
            return
        live = [lg for lg in t.legs if not lg.terminal]
        if t.exit_reason is not None:  # a leg that got protected after the exit decision still has to go
            for lg in live:
                oid = ctx.accepted.get(lg.tag)
                if lg.state == "PROTECTED" and oid is not None:
                    lg.state = "EXIT_CANCEL_STOP"
                    out.append(CancelOrder(oid))
            return
        if live and all(lg.state == "PROTECTED" for lg in live):
            reason = self._exit_reason(t, view, s)
            if reason is not None:
                self._decide_exit(t, reason, now, ctx, out)

    def _exit_reason(self, t: _Trade, view: MarketView, s: Session) -> str | None:
        filled = [lg for lg in t.legs if lg.fill_price is not None]
        first = min((lg.fill_ts for lg in filled if lg.fill_ts is not None), default=None)
        why = self.plugin.check_exit(s, self.p.signal, OpenTrade(t.signal, t.signal_at, t.spot, first))
        if why is not None:
            return why  # the plug-in's own reason (e.g. INVALIDATED_BACK_IN_RANGE, TARGET_VWAP)
        if s.label is not None:
            hit = triggered_regime_invalidations(self.spec.invalidation_rules, s.label.tags())
            for rule in hit:
                if rule.action in (InvalidationAction.EXIT_POSITION, InvalidationAction.STAND_DOWN_DAY):
                    t.stand_down = t.stand_down or rule.action is InvalidationAction.STAND_DOWN_DAY
                    return "INVALIDATED_REGIME_CHANGE"
        if self.p.target_pts is not None:
            live = [lg for lg in filled if not lg.terminal]
            marks = [view.last_bar(lg.key) for lg in live]
            if live and all(m is not None for m in marks):
                entry = sum((lg.fill_price for lg in live if lg.fill_price is not None), Decimal(0))
                value = sum((m.close for m in marks if m is not None), Decimal(0))
                if value >= entry + self.p.target_pts:
                    return "TARGET"
        if self.p.target_r is not None:
            live = [lg for lg in filled if not lg.terminal]
            if live:
                entry = sum((lg.fill_price for lg in live if lg.fill_price is not None), Decimal(0))
                risk = sum(
                    (lg.fill_price - lg.stop_trigger for lg in live if lg.fill_price and lg.stop_trigger), Decimal(0)
                )
                marks = [view.last_bar(lg.key) for lg in live]
                if all(m is not None for m in marks):
                    value = sum((m.close for m in marks if m is not None), Decimal(0))
                    if risk > 0 and value >= entry + self.p.target_r * risk:
                        return "TARGET"
        if s.end.astimezone(IST).time() >= self.p.time_exit:
            return "TIME_EXIT"
        if first is not None and s.end >= first + self.p.max_hold:
            return "MAX_HOLD"
        return None

    def _decide_exit(self, t: _Trade, reason: str, now: datetime, ctx: StrategyContext, out: list[Intent]) -> None:
        t.exit_reason = reason
        t.exit_decided_at = now
        t.record["exit_decided_at"] = now
        for lg in t.legs:
            if lg.state == "PROTECTED":
                oid = ctx.accepted.get(lg.tag)
                if oid is not None:
                    lg.state = "EXIT_CANCEL_STOP"
                    out.append(CancelOrder(oid))
            elif lg.state in ("ENTRY_WORKING",):
                oid = ctx.accepted.get(lg.tag)
                if oid is not None:
                    lg.state = "ENTRY_CANCELLING"
                    lg.attempts = self.p.max_chase_ticks + 1  # no re-quote: the trade is being closed

    def _protect(self, t: _Trade, lg: _Leg, ctx: StrategyContext, pos: int, out: list[Intent]) -> None:
        fp = self._fill_of(ctx, lg.tag)
        assert fp is not None
        lg.fill_price, lg.fill_ts = fp
        stops = self._stops(lg.fill_price, lg.stop_pct)
        assert stops is not None  # the entry limit passed the same check
        lg.stop_trigger, lg.stop_limit = stops
        lg.tag = self._tag(t, lg, "STOP")
        lg.state = "PROTECTED"
        out.append(
            self._order(lg.contract, OrderSide.SELL, pos, SimOrderType.SL_LIMIT, lg.stop_limit, lg.stop_trigger, lg.tag)
        )

    def _manage_leg(
        self, t: _Trade, lg: _Leg, view: MarketView, ctx: StrategyContext, now: datetime, out: list[Intent]
    ) -> None:
        if lg.terminal:
            return
        pos = ctx.position(lg.key)
        if lg.tag in ctx.rejected and pos == 0 and lg.fill_price is None:
            lg.state, lg.outcome = "REJECTED", ",".join(ctx.rejected[lg.tag])
            return
        oid = ctx.accepted.get(lg.tag)
        st = self._status.get(oid) if oid is not None else None
        if lg.state in ("ENTRY_WORKING", "ENTRY_CANCELLING"):
            own = self._filled_qty(ctx, lg.tag)
            if pos > 0 and own > 0:  # (if the trade is already being closed, _manage cancels this new stop next bar)
                self._protect(t, lg, ctx, min(pos, own), out)
                return
            if lg.state == "ENTRY_WORKING" and oid is not None and lg.submitted_at is not None:
                if now >= lg.submitted_at + 2 * ONE_MIN:  # one full bar was eligible
                    lg.state = "ENTRY_CANCELLING"
                    out.append(CancelOrder(oid))
                return
            if lg.state == "ENTRY_CANCELLING" and st is OrderStatus.CANCELLED:
                last = view.last_bar(lg.key)
                in_window = now.astimezone(IST).time() <= self.p.entry_end
                if lg.attempts > self.p.max_chase_ticks or last is None or not in_window:
                    lg.state, lg.outcome = "UNFILLED", "ENTRY_UNFILLED"
                    return
                cap = lg.first_limit + self.p.max_chase_ticks * self._tick
                lim = min(self._ceil(last.close) + self._tick, cap)
                risk = self._risk_at_stop(lim, lg.contract, t.day, lg.stop_pct)
                if risk is None or risk > self._budget:
                    lg.state, lg.outcome = "UNFILLED", "ENTRY_UNFILLED_RISK"
                    return
                self._send_entry(t, lg, lim, now, out)
            return
        if lg.state == "PROTECTED":
            if pos == 0:
                fp = self._fill_of(ctx, lg.tag)
                lg.exit_fill = fp[0] if fp else None
                lg.state, lg.outcome = "CLOSED", "STOP"
                if t.exit_reason is None and all(x.terminal or x is lg for x in t.legs):
                    t.exit_reason = "STOP"
            return
        if lg.state == "EXIT_CANCEL_STOP":
            if pos == 0:  # the stop filled before the cancel took effect
                fp = self._fill_of(ctx, lg.tag)
                lg.exit_fill = fp[0] if fp else None
                lg.state, lg.outcome = "CLOSED", "STOP"
            elif st is OrderStatus.CANCELLED:
                self._send_exit(t, lg, view, now, pos, out)
            return
        if lg.state in ("EXIT_WORKING", "EXIT_CANCELLING"):
            if pos == 0:
                fp = self._fill_of(ctx, lg.tag)
                lg.exit_fill = fp[0] if fp else None
                lg.state, lg.outcome = "CLOSED", t.exit_reason or "EXIT"
                return
            if lg.state == "EXIT_WORKING" and oid is not None and lg.submitted_at is not None:
                if now >= lg.submitted_at + 2 * ONE_MIN:
                    lg.state = "EXIT_CANCELLING"
                    out.append(CancelOrder(oid))
                return
            if lg.state == "EXIT_CANCELLING" and st is OrderStatus.CANCELLED:
                self._send_exit(t, lg, view, now, pos, out)


def library_metadata(strategy: LongOptionStrategy) -> dict[str, object]:
    p = strategy.p
    return {
        "spec": p.spec_id,
        "spec_version": p.spec_version,
        "plugin": strategy.plugin.code,
        "params": {k: str(v) for k, v in sorted(p.signal.items())},
        "stop_pct": str(p.stop_pct),
        "target_r": None if p.target_r is None else str(p.target_r),
        "target_pts": None if p.target_pts is None else str(p.target_pts),
        "stop_method": "PREMIUM_POINTS" if p.stop_points else "PREMIUM_PCT",
        "regime_gate": strategy._gate,
        "classifier": strategy._clf.version,
        "classifier_status": strategy._clf.cfg.status,
        "uses_futures_volume": strategy.plugin.uses_futures_volume,
        "deviations": list(strategy.plugin.deviations),
        "labels": ["SYNTHETIC inputs", "classifier UNVALIDATED", "costs ASSUMED where flagged"],
    }

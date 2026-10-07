"""Paper loop on a SIMULATED feed (docs/research/paper-trading.md §14.1 rehearsal; backlog P-01 groundwork).

The whole trading plane, end to end, on seeded SYNTHETIC days:

    synthetic index/VIX/futures bars + SYNTHETIC Black-Scholes chain
      -> K-11 regime classifier (UNVALIDATED) -> one RegimeReading per closed 1-minute bar
      -> each library strategy's signal plug-in (its own Session, the same bars)
      -> portfolio allocator v1 (eligibility, regime policy, exclusive groups, risk budget)
      -> TradeIntent -> RiskGovernor (paper venue, regime gate on) -> KernelRuntime -> FakeBroker

The kernel owns orders: it places the entry, rests the protective SL-limit after the fill, and flattens from 14:50
and runs Exit-All at 15:00. The loop only decides: entries (through the allocator and the Governor), exits for the
spec's reasons (plug-in invalidation, REGIME_CHANGE rules, the "N R" target, time exit, maximum hold) through
``request_exit``, and cancelling an entry not filled in time through ``cancel_entries``.

Honesty rules:

* every strategy is rehearsed as PAPER, whatever its spec says (every library spec is RESEARCH). This is a
  mechanics rehearsal, never a promotion; the run labels say so;
* the quotes are the SYNTHETIC option close +/- an ASSUMED half-spread; fills are the fake broker's (a buy fills
  when the ask is at or below the limit). Nothing here measures slippage or an edge;
* the Governor's live limits are unchanged: the 1-lot canary cap (one position across the whole book), 2% risk per
  trade, the 09:20-14:00 entry window, price sanity, kills, the event-day rule (no strategy is event-certified).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from project100c.backtest.types import OptionContract
from project100c.broker import FakeBroker
from project100c.calendar import ExpiryCalendar, MarketClock, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.core_types import OptionRight, OrderSide
from project100c.costs import CostModel, Side, load_brokerage_plans, load_charge_book
from project100c.costs.config import default_live_plan_id
from project100c.instruments import Contract, InstrumentKind
from project100c.journal import Journal
from project100c.kernel.governor import MarketSnapshot, RiskGovernor, TradeIntent, entry_caps_from_specs
from project100c.kernel.limits import RiskLimits, load_risk_limits
from project100c.kernel.regime_gate import RegimeGate, RegimeReading
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.market_types import Bar, Quote
from project100c.portfolio import AllocationMode, AllocatorConfig, StrategyRecord, allocate, load_allocator_config
from project100c.regime import EventCalendar, RegimeClassifier, RegimeConfig, load_regime_config
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows
from project100c.spec.models import InvalidationAction, Lifecycle, StrategySpec
from project100c.spec.regime_policy import triggered_regime_invalidations
from project100c.strategies.library import PLUGINS, EntrySignal, LibraryParams, OpenTrade, Session, SignalPlugin
from project100c.synthetic import DayPlan, SyntheticChain, SyntheticDay, generate_chain, generate_days
from project100c.validation.event_cert import CERT_DIR, event_certified_strategies

ONE_MIN = timedelta(minutes=1)
TICK = Decimal("0.05")
STRIKE_STEP = 50
LABELS = (
    "SIMULATED: paper loop on SYNTHETIC days and a SYNTHETIC Black-Scholes chain; not market data",
    "every strategy is rehearsed as PAPER (the specs are RESEARCH): mechanics only, no promotion implied",
    "fills from the fake broker on ASSUMED quotes (synthetic close +/- a half-spread); no slippage is measured",
    "regime classifier UNVALIDATED; thresholds ASSUMED; no edge is claimed",
)


@dataclass(frozen=True, slots=True)
class PaperKernel:
    """The config-derived pieces the loop needs (loaded once from ``configs/``)."""

    market_clock: MarketClock
    expiries: ExpiryCalendar
    costs: CostModel
    plan_id: str
    limits: RiskLimits
    regime: RegimeConfig
    allocator: AllocatorConfig
    # saved event-day gate results (validation/event_cert.py); a certified spec is verified against its file
    event_cert_dir: Path = CERT_DIR

    @classmethod
    def load(cls, configs: Path) -> PaperKernel:
        cal = TradingCalendar(load_holiday_book(configs / "calendar" / "nse_fo_holidays.toml"))
        sessions = SessionCalendar(
            load_exchange_sessions(configs / "sessions" / "exchange_sessions.toml"),
            load_trading_windows(configs / "sessions" / "trading_window.toml"),
        )
        costs = CostModel(
            load_charge_book(configs / "costs" / "nse_fo_index_options.toml"),
            load_brokerage_plans(configs / "costs" / "brokerage_plans.toml"),
        )
        return cls(
            MarketClock(cal, sessions),
            ExpiryCalendar(cal, load_expiry_rules(configs / "calendar" / "nifty_expiry_rules.toml")),
            costs,
            default_live_plan_id(configs / "costs" / "brokerage_plans.toml"),
            load_risk_limits(configs / "risk" / "limits.toml"),
            load_regime_config(configs / "regime" / "classifier.toml"),
            load_allocator_config(configs / "portfolio" / "allocator.toml"),
            configs / "validation" / "event_certifications",
        )


@dataclass(frozen=True, slots=True)
class PaperDecision:
    """One entry signal and what became of it."""

    at: datetime
    strategy_id: str
    signal: str
    outcome: str  # SKIPPED | ALLOCATOR_REFUSED | GOVERNOR_REJECTED | APPROVED
    reasons: tuple[str, ...] = ()
    regime_tags: tuple[str, ...] = ()
    risk_at_stop: Decimal | None = None
    budget: Decimal | None = None
    intents: tuple[str, ...] = ()
    repeats: int = 1  # the same signal with the same outcome on consecutive bars is folded into one record
    last_at: datetime | None = None

    def same_as(self, o: PaperDecision) -> bool:
        return (self.strategy_id, self.signal, self.outcome, self.reasons) == (
            o.strategy_id, o.signal, o.outcome, o.reasons)  # fmt: skip


@dataclass(slots=True)
class _Leg:
    contract: Contract
    intent_id: str
    limit: Decimal
    stop_trigger: Decimal
    stop_limit: Decimal
    state: str = "WORKING"  # WORKING -> OPEN -> CLOSED, or UNFILLED / REJECTED
    entry: Decimal | None = None
    filled_at: datetime | None = None
    pnl: Decimal | None = None
    stopped_out: bool = False

    @property
    def key(self) -> str:
        return self.contract.instrument_key

    @property
    def terminal(self) -> bool:
        return self.state in ("CLOSED", "UNFILLED", "REJECTED")


@dataclass(slots=True)
class PaperTrade:
    strategy_id: str
    day: date
    signal: EntrySignal
    signal_at: datetime
    spot: Decimal
    legs: list[_Leg]
    exit_reason: str | None = None
    exit_at: datetime | None = None
    stand_down: bool = False

    @property
    def done(self) -> bool:
        return all(lg.terminal for lg in self.legs)

    @property
    def net_pnl(self) -> Decimal:
        return sum((lg.pnl for lg in self.legs if lg.pnl is not None), Decimal(0))

    @property
    def filled(self) -> bool:
        return any(lg.entry is not None for lg in self.legs)

    def as_json(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy_id, "day": self.day.isoformat(), "signal": self.signal.reason,
            "signal_at": self.signal_at.isoformat(), "exit_reason": self.exit_reason,
            "net_pnl": str(self.net_pnl), "filled": self.filled,
            "legs": [{"key": lg.key, "state": lg.state, "entry": None if lg.entry is None else str(lg.entry),
                      "stop": str(lg.stop_limit), "pnl": None if lg.pnl is None else str(lg.pnl),
                      "stopped_out": lg.stopped_out} for lg in self.legs],
        }  # fmt: skip


@dataclass(slots=True)
class PaperDay:
    day: date
    sod_nav: Decimal
    realised: Decimal = Decimal(0)  # net of charges, from the kernel's journal-derived state
    charges: Decimal = Decimal(0)
    decisions: list[PaperDecision] = field(default_factory=list)
    trades: list[PaperTrade] = field(default_factory=list)
    regime_tags_close: tuple[str, ...] = ()
    event_day: bool = False
    urgent_alerts: list[str] = field(default_factory=list)
    kills: list[str] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)
    flat_at_end: bool = True
    orders_sent: int = 0
    fills: int = 0

    @property
    def eod_nav(self) -> Decimal:
        return self.sod_nav + self.realised


@dataclass(slots=True)
class PaperRun:
    days: list[PaperDay]
    strategies: tuple[str, ...]
    stage: Lifecycle
    config_versions: dict[str, str]
    journal_path: Path
    journal_verified: bool
    labels: tuple[str, ...] = LABELS

    @property
    def trades(self) -> list[PaperTrade]:
        return [t for d in self.days for t in d.trades]


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _floor(p: Decimal) -> Decimal:
    return (p / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK


def _contract(c: OptionContract) -> Contract:
    sym = f"NIFTY {c.expiry:%d%b%y} {int(c.strike)} {c.right.value} (SYNTHETIC)".upper()
    return Contract("SYNTHETIC", c.instrument_key, "0", c.underlying, InstrumentKind.OPTION, c.right, c.expiry,
                    c.strike, c.lot_size, TICK, 1800, sym)  # fmt: skip


class PaperLoop:
    """Run library strategies through the real allocator, Governor and kernel runtime on SIMULATED days."""

    def __init__(
        self,
        kernel: PaperKernel,
        specs: Sequence[StrategySpec],
        *,
        nav: Decimal,
        workdir: Path,
        events: EventCalendar | None = None,
        half_spread_ticks: int = 1,  # ASSUMED quote half-spread around the synthetic close
        entry_ttl: timedelta = timedelta(minutes=3),  # an entry not filled by then is cancelled
        stage: Lifecycle = Lifecycle.PAPER,
        plugins: Mapping[str, type[SignalPlugin]] | None = None,
        strike_fit_steps: int = 0,  # single-leg only: walk up to N strikes further OTM until the risk fits the budget
        chain_source: Callable[[SyntheticDay, Sequence[date]], SyntheticChain] = generate_chain,
    ) -> None:
        if nav <= 0:
            raise ValueError("nav must be positive")
        if half_spread_ticks < 1:
            raise ValueError("half_spread_ticks must be >= 1")
        if not 0 <= strike_fit_steps <= 40:
            raise ValueError("strike_fit_steps must be in [0, 40]")
        ids = [s.id for s in specs]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate strategy ids")
        reg = plugins if plugins is not None else PLUGINS
        missing = [i for i in ids if i not in reg]
        if missing:
            raise ValueError(f"no signal plug-in for {missing}")
        self.k = kernel
        self.specs = list(specs)
        self.params = {s.id: LibraryParams.from_spec(s) for s in specs}
        self._plugin_cls = {i: reg[i] for i in ids}
        self.nav = nav
        self.events = events
        self.half_spread = half_spread_ticks * TICK
        self.entry_ttl = entry_ttl
        self.stage = stage
        self.strike_fit_steps = strike_fit_steps
        self.chain_source = chain_source
        workdir.mkdir(parents=True, exist_ok=True)
        self.journal_path = workdir / "paper-journal.sqlite"
        self.clock = _Clock(datetime(2000, 1, 1, tzinfo=IST))
        self.broker = FakeBroker(self.clock, funds=nav)
        self.journal = Journal(self.journal_path, clock=self.clock)
        self.alerts = MemoryAlerts()
        certified = event_certified_strategies(self.specs, kernel.event_cert_dir)  # verified against saved results
        self.gov = RiskGovernor(kernel.limits, kernel.market_clock, kernel.costs, kernel.plan_id,
                                regime_gate=RegimeGate.from_specs(self.specs), paper_venue=True,
                                strategy_entry_caps=entry_caps_from_specs(self.specs),
                                event_certified_strategies=certified)  # fmt: skip
        self.rt = KernelRuntime(self.journal, self.broker, self.gov, kernel.costs, kernel.plan_id, self.clock,
                                self.alerts)  # fmt: skip
        self._seq = 0
        self._day_nav = nav
        self._losses: dict[str, int] = dict.fromkeys(ids, 0)
        self._cum: dict[str, Decimal] = dict.fromkeys(ids, Decimal(0))
        self._peak: dict[str, Decimal] = dict.fromkeys(ids, Decimal(0))

    # ------------------------------------------------------------------ run
    def run(self, plans: Sequence[DayPlan]) -> PaperRun:
        days: list[PaperDay] = []
        nav = self.nav
        prev_monday: date | None = None
        for sd in generate_days(plans):
            d = sd.plan.day
            if not self.k.market_clock.calendar.is_trading_day(d):
                raise ValueError(f"{d} is not a trading day")
            week = d - timedelta(days=d.weekday())
            pd = self._run_day(sd, nav, week_start=week != prev_monday)
            prev_monday = week
            nav = pd.eod_nav
            days.append(pd)
        versions = {
            "limits": self.k.limits.version, "allocator": self.k.allocator.version,
            "classifier": self.k.regime.version, "events": self.events.version if self.events else "none",
        }  # fmt: skip
        ok = True
        try:
            self.journal.verify()
        except Exception:
            ok = False
        return PaperRun(days, tuple(s.id for s in self.specs), self.stage, versions, self.journal_path, ok)

    # ------------------------------------------------------------------ one day
    def _run_day(self, sd: SyntheticDay, nav: Decimal, *, week_start: bool) -> PaperDay:
        d = sd.plan.day
        k = self.k
        self.clock.t = datetime.combine(d, time(9, 0), IST)
        self.rt.start_day(d, nav, week_start=week_start)
        pd = PaperDay(d, nav)
        self._day_nav = nav
        exps = [e.date for e in k.expiries.expiries_between(d, d + timedelta(days=21))]
        chain = self.chain_source(sd, exps)
        obar: dict[tuple[str, datetime], Bar] = {(b.instrument_key, b.start): b for b in chain.bars}
        by_exp: dict[tuple[date, OptionRight], list[OptionContract]] = {}
        for c in chain.contracts.values():
            by_exp.setdefault((c.expiry, c.right), []).append(c)
        expiries = sorted({c.expiry for c in chain.contracts.values()})
        contracts: dict[str, Contract] = {}
        clf = RegimeClassifier(k.regime, expiries=k.expiries, events=self.events)
        prev = Decimal(repr(sd.plan.prev_close))
        clf.start_session(d, prev)
        plugins = {sid: cls() for sid, cls in self._plugin_cls.items()}
        sessions = {sid: Session(d, prev) for sid in plugins}
        for s in sessions.values():
            s.expiry_day = k.expiries.is_expiry_day(d)
        open_trades: dict[str, PaperTrade] = {}
        trades_today: dict[str, int] = dict.fromkeys(plugins, 0)
        stood_down: set[str] = set()
        daily_risk = Decimal(0)
        last_dec: dict[str, int] = {}
        n_closed = len(self.rt.state.closed_trades)
        fut = {b.start: b for b in sd.fut}
        vix = {b.start: b for b in sd.vix}
        reading: RegimeReading | None = None
        orders_before = len(self.broker.orders())

        def quote_of(key: str, start: datetime, now: datetime) -> Quote | None:
            b = obar.get((key, start))
            if b is None:
                return None
            bid = max(TICK, b.close - self.half_spread)
            ask = b.close + self.half_spread
            return Quote(key, now, now, bid, ask, 6500, 6500, b.close, 250_000)

        for b in sd.index:
            now = b.start + ONE_MIN
            self.clock.t = now
            f, v = fut.get(b.start), vix.get(b.start)
            label = clf.on_bar(b, vix=None if v is None else v.close, volume=None if f is None else f.volume)
            reading = RegimeReading.from_label(label)
            pd.event_day = pd.event_day or label.event_day
            for s in sessions.values():
                s.idx.append(b)
                s.fut.append(f)
                s.vix.append(v)
                s.label = label
                s.event_day = label.event_day
            keys = set(self.rt.state.positions) | {o.instrument_key for o in self.rt.state.open_orders.values()}
            keys |= {lg.key for t in open_trades.values() for lg in t.legs if not lg.terminal}
            quotes = {key: q for key in keys if (q := quote_of(key, b.start, now)) is not None}
            for key, q in quotes.items():
                self.broker.set_quote(key, q.bid, q.ask)
            self.rt.step(quotes)
            n_closed = self._settle(open_trades, n_closed, now)
            for sid in list(open_trades):
                t = open_trades[sid]
                if t.done:
                    self._finish(t, pd, stood_down)
                    del open_trades[sid]
                else:
                    self._manage(t, sessions[sid], plugins[sid], obar, b.start, now)
            for sid, plugin in plugins.items():
                s = sessions[sid]
                sig = plugin.on_bar(s, self.params[sid].signal)
                if sig is None:
                    continue
                dec = self._on_signal(sid, sig, s, now, reading, open_trades, trades_today, stood_down, daily_risk,
                                      by_exp, expiries, contracts, quote_of, b.start)  # fmt: skip
                last = last_dec.get(sid)
                if last is not None and dec.outcome != "APPROVED" and pd.decisions[last].same_as(dec):
                    prev_d = pd.decisions[last]
                    pd.decisions[last] = replace(prev_d, repeats=prev_d.repeats + 1, last_at=dec.at)
                else:
                    last_dec[sid] = len(pd.decisions)
                    pd.decisions.append(dec)
                if dec.outcome == "APPROVED" and dec.risk_at_stop is not None:
                    daily_risk += dec.risk_at_stop
        # the session is over: anything still open was flattened by the kernel (or is a residual: reported)
        self._settle(open_trades, n_closed, self.clock.t)
        for t in open_trades.values():
            for lg in t.legs:
                if not lg.terminal:
                    lg.state = "UNFILLED" if lg.entry is None else lg.state
            t.exit_reason = t.exit_reason or "SESSION_END"
            self._finish(t, pd, stood_down)
        st = self.rt.state
        pd.realised = st.realised_today
        pd.flat_at_end = not st.positions and not st.open_orders
        pd.regime_tags_close = tuple(sorted(x.value for x in reading.tags)) if reading else ()
        pd.urgent_alerts = list(self.alerts.urgent())
        pd.kills = [f"{sw}{'(' + sc + ')' if sc else ''}: {kl.reason}" for (sw, sc), kl in st.kills.items()]
        pd.halts = [f"{h}: {hl.reason}" for h, hl in st.halts.items()]
        day_fills = [x for x in self.broker.trades() if x.ts.astimezone(IST).date() == d]
        pd.fills = len(day_fills)
        pd.orders_sent = len(self.broker.orders()) - orders_before
        pd.charges = sum(
            (k.costs.order_charges(Side.BUY if x.side is OrderSide.BUY else Side.SELL, x.price, x.qty, d,
                                   k.plan_id).total for x in day_fills),
            Decimal(0),
        )  # fmt: skip
        return pd

    # ------------------------------------------------------------------ entries
    def _records(self) -> list[StrategyRecord]:
        st = self.rt.state
        out = []
        for s in self.specs:
            dd = (self._peak[s.id] - self._cum[s.id]) / self._day_nav
            out.append(StrategyRecord(s, status=self.stage, killed=st.strategy_killed(s.id),
                                      consecutive_losses=self._losses[s.id], drawdown_frac=dd,
                                      entries_today=st.entries_by_strategy.get(s.id, 0)))  # fmt: skip
        return out

    def _on_signal(
        self,
        sid: str,
        sig: EntrySignal,
        s: Session,
        now: datetime,
        reading: RegimeReading | None,
        open_trades: dict[str, PaperTrade],
        trades_today: dict[str, int],
        stood_down: set[str],
        daily_risk: Decimal,
        by_exp: Mapping[tuple[date, OptionRight], list[OptionContract]],
        expiries: Sequence[date],
        contracts: dict[str, Contract],
        quote_of: Any,
        start: datetime,
    ) -> PaperDecision:
        p = self.params[sid]
        tags = tuple(sorted(x.value for x in reading.tags)) if reading else ()

        def skip(why: str, *more: str) -> PaperDecision:
            return PaperDecision(now, sid, sig.reason, "SKIPPED", (why, *more), tags)

        t_now = s.end.astimezone(IST).time()
        if sid in open_trades:
            return skip("IN_TRADE")
        if sid in stood_down:
            return skip("STOOD_DOWN")
        if not (p.entry_start <= t_now <= p.entry_end):
            return skip("OUTSIDE_ENTRY_WINDOW")
        if trades_today[sid] >= p.max_trades_per_day:
            return skip("MAX_TRADES_TODAY")
        plan = allocate(
            self._records(),
            nav=self._day_nav,
            regime=reading,
            now=now,
            cfg=self.k.allocator,
            mode=AllocationMode.SIMULATE,
            daily_risk_used=daily_risk,
            book_entries_today=self.rt.state.entries_today,
            max_entries_per_day=self.k.limits.max_trades_per_day,
        )
        a = plan.get(sid)
        if not a.eligible:
            return PaperDecision(now, sid, sig.reason, "ALLOCATOR_REFUSED",
                                 tuple(f"{c.value}: {n}" for c, n in a.refusals), tags)  # fmt: skip
        spot = s.last.close
        exp = next((e for e in expiries if (e - s.day).days >= p.dte_min), None)
        if exp is None:
            return skip("NO_EXPIRY")
        multi = len(sig.rights) > 1

        def build(steps: int) -> tuple[list[_Leg], Decimal] | str:
            legs: list[_Leg] = []
            risk = Decimal(0)
            for right in sig.rights:
                off = steps * STRIKE_STEP
                target = spot + (off if right is OptionRight.CE else -off)
                cands = sorted(by_exp.get((exp, right), []), key=lambda c: (abs(c.strike - target), c.strike))
                if not cands:
                    return "NO_CONTRACT"
                oc = cands[0]
                q = quote_of(oc.instrument_key, start, now)
                if q is None or q.ask is None:
                    return "NO_QUOTE"
                lim = q.ask + TICK
                trig = _floor(lim * (1 - p.stop_pct / 100))
                stop = trig - p.stop_offset_ticks * TICK
                if stop < TICK:
                    return "NO_TRADE_STOP_BELOW_TICK"
                charges = self.k.costs.order_charges
                rt_cost = (
                    charges(Side.BUY, lim, oc.lot_size, s.day, self.k.plan_id).total
                    + charges(Side.SELL, stop, oc.lot_size, s.day, self.k.plan_id).total
                )
                risk += (lim - stop) * oc.lot_size + rt_cost
                legs.append(_Leg(_contract(oc), "", lim, trig, stop))
            return legs, risk

        # the spec's strike first; with strike_fit_steps (single-leg only) walk further OTM until the risk fits
        tries = range(p.otm_steps, p.otm_steps + (0 if multi else self.strike_fit_steps) + 1)
        built = build(tries[0])
        for steps in tries[1:]:
            if isinstance(built, tuple) and built[1] <= a.risk_budget_inr:
                break
            nxt = build(steps)
            if isinstance(nxt, str):
                break
            built = nxt
        if isinstance(built, str):
            return skip(built)
        legs, risk = built
        for lg in legs:
            lg.contract = contracts.setdefault(lg.key, lg.contract)
            self._seq += 1
            lg.intent_id = f"P-{sid}-{s.day:%m%d}-{self._seq:04d}"
        if risk > a.risk_budget_inr:
            return PaperDecision(now, sid, sig.reason, "SKIPPED", ("NO_TRADE_RISK_OVER_ALLOCATION",), tags,
                                 risk, a.risk_budget_inr)  # fmt: skip
        t = PaperTrade(sid, s.day, sig, now, spot, legs)
        reasons: list[str] = []
        approved: list[str] = []
        gov_risk = Decimal(0)
        budget: Decimal | None = None
        for lg in legs:
            q = quote_of(lg.key, start, now)
            assert q is not None
            self.broker.set_quote(lg.key, q.bid, q.ask)
            intent = TradeIntent(lg.intent_id, sid, self.stage, lg.contract, OrderSide.BUY, lg.contract.lot_size,
                                 lg.limit, now, stop_trigger=lg.stop_trigger, stop_limit=lg.stop_limit,
                                 spec_stop_limit=lg.stop_limit, multi_leg=multi)  # fmt: skip
            cash = self.broker.funds()
            dec = self.rt.submit(intent, MarketSnapshot(now, q, cash, event_day=s.event_day, regime=reading))
            budget = dec.budget if dec.budget is not None else budget
            if dec.approved:
                approved.append(lg.intent_id)
                # a later leg's risk already includes the earlier legs' worst case (OD-013 combined risk)
                gov_risk = max(gov_risk, dec.risk_at_stop or Decimal(0))
            else:
                lg.state = "REJECTED"
                reasons.extend(f"{r.value}" for r in dec.reasons)
                reasons.extend(dec.notes)
        if not approved:
            return PaperDecision(now, sid, sig.reason, "GOVERNOR_REJECTED", tuple(reasons), tags, risk, budget)
        trades_today[sid] += 1
        open_trades[sid] = t
        if reasons:  # a multi-leg entry with a refused leg: its thesis needs every leg, close the rest
            t.exit_reason, t.exit_at = "LEG_REJECTED", now
        return PaperDecision(now, sid, sig.reason, "APPROVED", tuple(reasons), tags, gov_risk, budget,
                             tuple(approved))  # fmt: skip

    # ------------------------------------------------------------------ management
    def _settle(self, open_trades: Mapping[str, PaperTrade], n_closed: int, now: datetime) -> int:
        """Move legs along from the kernel's state: filled -> OPEN, closed -> CLOSED with its net P&L."""
        st = self.rt.state
        new = st.closed_trades[n_closed:]
        for t in open_trades.values():
            for lg in t.legs:
                if lg.state == "WORKING" and lg.key in st.positions:
                    lg.state, lg.entry, lg.filled_at = "OPEN", st.positions[lg.key].avg_price, now
                elif lg.state == "WORKING":
                    working = any(o.instrument_key == lg.key for o in st.open_orders.values())
                    done = next((ct for ct in new if ct.instrument_key == lg.key and ct.strategy_id == t.strategy_id),
                                None)  # fmt: skip
                    if done is not None:  # filled and closed within one step
                        lg.state, lg.pnl, lg.stopped_out, lg.entry = "CLOSED", done.pnl, done.stopped_out, lg.limit
                    elif not working:
                        lg.state = "UNFILLED"
                if lg.state == "OPEN" and lg.key not in st.positions:
                    done = next((ct for ct in new if ct.instrument_key == lg.key and ct.strategy_id == t.strategy_id),
                                None)  # fmt: skip
                    lg.state = "CLOSED"
                    if done is not None:
                        lg.pnl, lg.stopped_out = done.pnl, done.stopped_out
                        if done.stopped_out and t.exit_reason is None:
                            t.exit_reason, t.exit_at = "STOP", done.closed_at
        return len(st.closed_trades)

    def _manage(self, t: PaperTrade, s: Session, plugin: SignalPlugin, obar: Mapping[tuple[str, datetime], Bar],
                start: datetime, now: datetime) -> None:  # fmt: skip
        p = self.params[t.strategy_id]
        for lg in t.legs:  # an entry not filled in time is cancelled (the kernel never chases)
            if lg.state == "WORKING" and now - t.signal_at >= self.entry_ttl:
                self.rt.cancel_entries(lg.key)
        if t.exit_reason is None:
            if any(lg.state in ("UNFILLED", "REJECTED") for lg in t.legs) and len(t.legs) > 1:
                t.exit_reason = "LEG_UNFILLED"
            else:
                t.exit_reason = self._exit_reason(t, s, plugin, p, obar, start)
            if t.exit_reason is not None:
                t.exit_at = now
        if t.exit_reason is not None:
            for lg in t.legs:
                if lg.state == "OPEN":
                    self.rt.request_exit(lg.key, t.exit_reason)
                elif lg.state == "WORKING":
                    self.rt.cancel_entries(lg.key)

    def _exit_reason(self, t: PaperTrade, s: Session, plugin: SignalPlugin, p: LibraryParams,
                     obar: Mapping[tuple[str, datetime], Bar], start: datetime) -> str | None:  # fmt: skip
        live = [lg for lg in t.legs if lg.state == "OPEN"]
        if not live or any(lg.state == "WORKING" for lg in t.legs):
            return None
        first = min((lg.filled_at for lg in live if lg.filled_at is not None), default=None)
        why = plugin.check_exit(s, p.signal, OpenTrade(t.signal, t.signal_at, t.spot, first))
        if why is not None:
            return why
        if s.label is not None:
            for rule in triggered_regime_invalidations(self._spec(t.strategy_id).invalidation_rules, s.label.tags()):
                if rule.action in (InvalidationAction.EXIT_POSITION, InvalidationAction.STAND_DOWN_DAY):
                    t.stand_down = t.stand_down or rule.action is InvalidationAction.STAND_DOWN_DAY
                    return "INVALIDATED_REGIME_CHANGE"
        if p.target_r is not None:
            entry = sum((lg.entry for lg in live if lg.entry is not None), Decimal(0))
            risk = sum((lg.entry - lg.stop_trigger for lg in live if lg.entry is not None), Decimal(0))
            marks = [obar.get((lg.key, start)) for lg in live]
            if risk > 0 and all(m is not None for m in marks):
                value = sum((m.close for m in marks if m is not None), Decimal(0))
                if value >= entry + p.target_r * risk:
                    return "TARGET"
        if s.end.astimezone(IST).time() >= p.time_exit:
            return "TIME_EXIT"
        if first is not None and s.end >= first + p.max_hold:
            return "MAX_HOLD"
        return None

    def _spec(self, sid: str) -> StrategySpec:
        return next(s for s in self.specs if s.id == sid)

    def _finish(self, t: PaperTrade, pd: PaperDay, stood_down: set[str]) -> None:
        if t.exit_reason is None and not t.filled:
            t.exit_reason = "ENTRY_UNFILLED"
        if t.exit_reason is None:
            t.exit_reason = "KERNEL_FLATTEN"  # the 14:50 forced flatten or the 15:00 Exit-All
        if t.stand_down:
            stood_down.add(t.strategy_id)
        pd.trades.append(t)
        if t.filled:
            sid = t.strategy_id
            self._cum[sid] += t.net_pnl
            self._peak[sid] = max(self._peak[sid], self._cum[sid])
            self._losses[sid] = self._losses[sid] + 1 if t.net_pnl < 0 else 0

"""SIMULATED trading day for the dashboard: synthetic prices -> simulated strategies -> the REAL Risk Governor and
kernel runtime harness -> the fake broker. The dashboard reads what happened; it cannot change it.

Every event the simulator yields is labelled SIMULATED. The ``kill_port()`` it hands to the server is a
kill-only object: MANUAL_MASTER_KILL via the kernel runtime's public API, and nothing else.
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from project100c.broker import FakeBroker
from project100c.broker.fake import FaultPlan
from project100c.calendar import ExpiryCalendar, MarketClock, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.core_types import OptionRight, OrderSide
from project100c.costs import CostModel, load_brokerage_plans, load_charge_book
from project100c.costs.config import default_live_plan_id
from project100c.economics import (
    EconomicsConfig,
    build_statement,
    cost_justification,
    load_economics_config,
    snapshot,
    trading_days_from_journal,
)
from project100c.economics.fmt import inr
from project100c.instruments import Contract, InstrumentKind
from project100c.journal import Journal, JournalRecord
from project100c.kernel.governor import MarketSnapshot, RiskGovernor, TradeIntent
from project100c.kernel.kills import KillSwitch
from project100c.kernel.limits import RiskLimits, load_risk_limits
from project100c.kernel.runtime import KernelRuntime, MemoryAlerts
from project100c.market_types import Bar, Quote
from project100c.observability.dashboard import tony
from project100c.observability.dashboard.events import DashboardError, DashEvent, EventKind, Flow
from project100c.observability.dashboard.market import (
    STEP,
    TICK,
    MarketPoint,
    atm,
    option_quote,
    q_tick,
    synthetic_day,
    synthetic_prev_close,
)
from project100c.observability.dashboard.topology import EDGE_SET, STRATEGIES
from project100c.regime import RegimeClassifier, RegimeConfig, RegimeLabel, load_regime_config
from project100c.sessions import IST, SessionCalendar, load_exchange_sessions, load_trading_windows
from project100c.sessions.model import TradingWindow
from project100c.spec.models import Lifecycle, Regime

REPO = Path(__file__).resolve().parents[4]
CONFIGS = REPO / "configs"
LOT = 65  # current NIFTY lot (NSE/FAOP/70616); the lot history table ends 30-Sep-2026, so it is fixed here
NAV = Decimal("10000")
ORB, VWAPC = "S-ORB-001", "S-VWAPC-001"
_STAGE = {s.id: Lifecycle(s.stage) for s in STRATEGIES}
_SNODE = {s.id: s.node for s in STRATEGIES}
_ORDER_KIND_TEXT = {
    "ENTRY": "Entry order",
    "PROTECTIVE": "Protective stop",
    "EXIT": "Exit order",
    "EXIT_ALL": "Exit-All",
}
_PHASE_TEXT = {
    "BEFORE_WINDOW": "Session: before the trading window (no order activity)",
    "OPENING_NO_ENTRY": "Session: order activity allowed from 09:15; no new entries before 09:20",
    "ENTRY_ALLOWED": "Session: entry window open (09:20-14:00)",
    "EXIT_ONLY": "Session: entry cutoff passed (14:00); exits only",
    "FLATTENING": "Session: forced-flatten window (14:50-15:00)",
    "CLOSED": "Session: hard flat (15:00); no order activity",
}


def _flow(src: str, dst: str, kind: str) -> Flow:
    if (src, dst) not in EDGE_SET:
        raise DashboardError(f"flow {src}->{dst} is not an edge of the topology")
    return Flow(src, dst, kind)


@dataclass(frozen=True, slots=True)
class Scenario:
    day: date
    seed: int
    name: str
    title: str
    broker_drop: tuple[time, time] | None = None  # fake-broker link down in [start, end)


REPLAY_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(date(2026, 10, 5), 101, "normal-1", "Mon 05-Oct-2026 · SIMULATED · routine session"),
    Scenario(date(2026, 10, 7), 202, "normal-2", "Wed 07-Oct-2026 · SIMULATED · routine session"),
    Scenario(
        date(2026, 10, 8),
        303,
        "broker-drop",
        "Thu 08-Oct-2026 · SIMULATED · fake-broker link drop 11:40 -> BROKER_CONNECTIVITY_KILL",
        (time(11, 40), time(11, 42)),
    ),
)


@dataclass
class _Clock:
    t: datetime

    def __call__(self) -> datetime:
        return self.t


@dataclass
class Kernel:
    """Shared, config-derived pieces (loaded once)."""

    market_clock: MarketClock
    expiries: ExpiryCalendar
    costs: CostModel
    plan_id: str
    limits: RiskLimits
    window: TradingWindow
    economics: EconomicsConfig
    regime: RegimeConfig

    @classmethod
    def load(cls, configs: Path = CONFIGS) -> Kernel:
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
            sessions.window,
            load_economics_config(configs / "economics" / "economics.toml"),
            load_regime_config(configs / "regime" / "classifier.toml"),
        )

    def window_json(self) -> dict[str, str]:
        w = self.window
        return {
            "version": w.version,
            "order_activity_start": w.order_activity_start.isoformat(),
            "entry_start": w.entry_start.isoformat(),
            "entry_cutoff": w.entry_cutoff.isoformat(),
            "flatten_start": w.flatten_start.isoformat(),
            "hard_flat": w.hard_flat.isoformat(),
            "order_activity_end": w.order_activity_end.isoformat(),
        }


class KillPort:
    """The ONLY capability the dashboard server receives: a kill-only callable. No attribute exposes the kernel."""

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[str, str], dict[str, Any]]) -> None:
        self._fn = fn

    def manual_master_kill(self, reason: str, requested_by: str) -> dict[str, Any]:
        return self._fn(reason, requested_by)


@dataclass
class _Trade:
    key: str
    contract: Contract
    stop_trigger: Decimal
    stop_limit: Decimal
    target: Decimal
    entry_limit: Decimal
    exit_requested: str | None = None
    intent_id: str = ""
    thesis: str = ""


@dataclass
class SimulatedSession:
    scenario: Scenario
    kernel: Kernel
    workdir: Path
    step_s: int = 15
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        sc = self.scenario
        if not self.kernel.market_clock.calendar.is_trading_day(sc.day):
            raise DashboardError(f"{sc.day} is not a trading day")
        self.points = synthetic_day(sc.day, sc.seed, self.step_s)
        self.clock = _Clock(self.points[0].ts - timedelta(minutes=1))
        faults = FaultPlan()
        if sc.broker_drop is not None:
            a, b = sc.broker_drop
            faults.disconnected_windows.append((datetime.combine(sc.day, a, IST), datetime.combine(sc.day, b, IST)))
        self.broker = FakeBroker(self.clock, funds=NAV, faults=faults)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.workdir / f"journal-{sc.name}.sqlite", clock=self.clock)
        self.alerts = MemoryAlerts()
        k = self.kernel
        self.gov = RiskGovernor(k.limits, k.market_clock, k.costs, k.plan_id)
        self.rt = KernelRuntime(self.journal, self.broker, self.gov, k.costs, k.plan_id, self.clock, self.alerts)
        nxt = [e for e in k.expiries.expiries_between(sc.day + timedelta(days=1), sc.day + timedelta(days=15))]
        self.expiry = nxt[0].date
        self._contracts: dict[str, Contract] = {}
        self._journal_seq = 0
        self._or: tuple[Decimal, Decimal] | None = None
        self._or_pts: list[Decimal] = []
        self._orb_trades = 0
        self._orb_blocked = False
        self._trade: _Trade | None = None
        self._vw_n = 0
        self._vw_sum = Decimal(0)
        self._vw_sent = 0
        self._last_kills: tuple[tuple[str, str], ...] | None = None
        self._last_phase: str | None = None
        self._intent_seq = 0
        self._prev_spot: Decimal | None = None
        self._open_spot: Decimal | None = None
        self._last_pos_sig: tuple[Any, ...] | None = None
        self._orders: dict[str, dict[str, Any]] = {}  # client_order_id -> what was submitted (for activity text)
        self._decisions: list[tony.DecisionView] = []
        self._pending_thesis = ""
        self._econ: dict[str, Any] | None = None
        self._regime: dict[str, Any] | None = None
        # K-11 regime classifier on 1-minute bars built from the 15 s points (UNVALIDATED; SIMULATED inputs)
        self._rc = RegimeClassifier(k.regime, expiries=k.expiries)
        self._rc.start_session(sc.day, synthetic_prev_close(sc.day, sc.seed))
        self._bar: list[MarketPoint] = []
        self._last_tick: datetime | None = None
        self._last_point: MarketPoint | None = None
        self._unprotected_since: datetime | None = None
        self._tony_key: tuple[Any, ...] | None = None
        self._tony_answers: Any = None
        self._tony_at: datetime | None = None
        self._attention = "NORMAL"
        m0 = sc.day.replace(day=1)
        m1 = (m0 + timedelta(days=32)).replace(day=1)
        cal = self.kernel.market_clock.calendar
        self._month_trading_days = sum(cal.is_trading_day(m0 + timedelta(days=i)) for i in range((m1 - m0).days))
        self.finished = False
        self._owner = threading.get_ident()  # the SQLite journal belongs to the thread that created the session

    # ------------------------------------------------------------------ helpers
    def _contract(self, strike: int, right: OptionRight) -> Contract:
        key = f"NSE_FO|NIFTY{self.expiry:%d%b%y}{strike}{right.value}".upper()
        if key not in self._contracts:
            self._contracts[key] = Contract(
                "SIMULATED",
                key,
                "0",
                "NIFTY",
                InstrumentKind.OPTION,
                right,
                self.expiry,
                Decimal(strike),
                LOT,
                TICK,
                1800,
                f"NIFTY {self.expiry:%d%b%y} {strike} {right.value}".upper(),
            )
        return self._contracts[key]

    def _quote(self, c: Contract, p: MarketPoint) -> Quote:
        assert c.strike is not None
        oq = option_quote(p.spot, int(c.strike), c.right is OptionRight.CE, p.ts, self.expiry, p.vix)
        return Quote(c.instrument_key, p.ts, p.ts, oq.bid, oq.ask, 6500, 6500, oq.theo, 250000)

    def _ev(self, kind: EventKind, data: dict[str, Any], *flows: Flow) -> DashEvent:
        return DashEvent(self.clock.t, kind, data, tuple(flows))

    def _log(self, level: str, text: str, *flows: Flow, act: tuple[str, str, str] | None = None) -> DashEvent:
        """A raw log line; ``act`` = (actor node, human sentence, tone) adds the System Activity view of it.
        Tones: info | ok | warn | reject | bad | muted."""
        data: dict[str, Any] = {"level": level, "text": text}
        if act is not None:
            data["activity"] = {"actor": act[0], "text": act[1], "tone": act[2]}
        return self._ev(EventKind.LOG, data, *flows)

    def _sym(self, key: str) -> str:
        c = self._contracts.get(key)
        return c.trading_symbol if c is not None else key

    # ------------------------------------------------------------------ the day
    def run(self) -> Iterator[DashEvent]:
        sc, k = self.scenario, self.kernel
        with self._lock:
            self.rt.start_day(sc.day, NAV, week_start=sc.day.weekday() == 0)
            self._journal_seq = self.journal.head[0]
            first = [
                self._ev(
                    EventKind.SESSION,
                    {
                        "scenario": sc.name,
                        "title": sc.title,
                        "day": sc.day,
                        "seed": sc.seed,
                        "expiry": self.expiry,
                        "lot": LOT,
                        "nav": NAV,
                        "step_s": self.step_s,
                        "window": k.window_json(),
                        "limits": {
                            "version": k.limits.version,
                            "per_trade_max_loss_frac": k.limits.per_trade_max_loss_frac,
                            "daily_stop_frac": k.limits.daily_stop_frac,
                            "dd_warning_frac": k.limits.dd_warning_frac,
                            "dd_suspend_frac": k.limits.dd_suspend_frac,
                            "dd_hard_ceiling_frac": k.limits.dd_hard_ceiling_frac,
                        },
                        "notice": "SIMULATED: synthetic prices, fake broker, simulated lifecycle stages",
                    },
                ),
                self._strategies_event(),
                self._economics_event(),
                self._log(
                    "INFO",
                    f"SIMULATED session {sc.name} ({sc.day}) started · weekly expiry {self.expiry}",
                    act=(
                        "cio",
                        f"Session started (SIMULATED {sc.day:%d %b %Y}); weekly expiry {self.expiry:%d %b}",
                        "muted",
                    ),
                ),
            ]
            first += self._tony_events(force=True)
        yield from first
        for i, p in enumerate(self.points):
            with self._lock:
                evs = self._step(i, p)
            yield from evs
        with self._lock:
            self.finished = True
            st = self.rt.state
            end = [
                self._risk_event(),
                self._economics_event(),
                self._ev(
                    EventKind.DAY_END,
                    {
                        "scenario": sc.name,
                        "trades": len(st.closed_trades),
                        "realised": st.realised_today,
                        "nav": st.nav,
                        "kills": [f"{sw}" for sw, _ in st.kills],
                    },
                ),
                *self._tony_events(force=True),
                self._log(
                    "INFO",
                    f"SIMULATED session {sc.name} ended · NAV {q_tick(st.nav, Decimal('0.01'))}",
                    act=(
                        "cio",
                        f"Session ended: NAV {inr(st.nav)}, {len(st.closed_trades)} trade(s) closed",
                        "muted",
                    ),
                ),
            ]
        yield from end

    def _strategies_event(self) -> DashEvent:
        rows = [
            {
                "id": s.id,
                "hypothesis": s.hypothesis,
                "name": s.name,
                "stage": s.stage,
                "killed": self.rt.state.strategy_killed(s.id),
            }
            for s in STRATEGIES
        ]
        return self._ev(EventKind.STRATEGIES, {"rows": rows, "stages_simulated": True})

    def _step(self, i: int, p: MarketPoint) -> list[DashEvent]:
        self.clock.t = p.ts
        self._last_tick = p.ts
        self._last_point = p
        self._feed_regime(p)
        out: list[DashEvent] = []
        prev = self._prev_spot or p.spot
        self._prev_spot = p.spot
        if self._open_spot is None:
            self._open_spot = p.spot
        chg = p.spot - self._open_spot
        tick_flows = [_flow("broker", "market_intel", "tick"), _flow("market_intel", "data_quality", "tick")]
        if i % 4 == 0:
            tick_flows += [_flow("data_quality", _SNODE[s.id], "clean") for s in STRATEGIES[:3]]
        out.append(
            self._ev(
                EventKind.TICK,
                {
                    "spot": p.spot,
                    "vix": p.vix,
                    "change": chg,
                    "change_pct": (chg / self._open_spot * 100).quantize(Decimal("0.01")),
                    "delta": p.spot - prev,
                },
                *tick_flows,
            )
        )
        tod = p.ts.timetz()
        if i % 4 == 0:
            out.append(self._chain_event(p))
        if i % 20 == 0:
            out.append(self._regime_event(i, p))
        if i % 240 == 120:
            out.append(
                self._log(
                    "INFO",
                    "Validation: lifecycle evidence updated from post-trade (SIMULATED)",
                    _flow("post_trade", "validation", "report"),
                    _flow("strategy_factory", "validation", "promote"),
                    act=("validation", "Validation refreshed lifecycle evidence from post-trade (SIMULATED)", "muted"),
                )
            )
        # quotes for every contract we touch
        quotes = {key: self._quote(c, p) for key, c in self._contracts.items()}
        for key, q in quotes.items():
            self.broker.set_quote(key, q.bid, q.ask)
        # strategies
        if tod.replace(tzinfo=None) < time(9, 30):
            self._or_pts.append(p.spot)
        elif self._or is None and self._or_pts:
            self._or = (max(self._or_pts), min(self._or_pts))
            out.append(
                self._log(
                    "INFO",
                    f"S-ORB-001 opening range {self._or[1]}-{self._or[0]} (SIMULATED)",
                    act=(
                        _SNODE[ORB],
                        f"Opening range set: {tony.pts(self._or[1])}-{tony.pts(self._or[0])} (09:15-09:30). "
                        f"Breakout triggers: above {tony.pts(self._or[0] + 8)} or below {tony.pts(self._or[1] - 8)}",
                        "info",
                    ),
                )
            )
        self._vw_n += 1
        self._vw_sum += p.spot
        out += self._orb(p)
        out += self._vwapc(p)
        out += self._manage_trade(p)
        # kernel step (sync, reconcile, protective, required actions)
        quotes = {key: self._quote(c, p) for key, c in self._contracts.items()}
        self.rt.step(quotes)
        self._track_protective()
        jev = self._journal_events()
        out += jev
        if any(e.kind is EventKind.FILL for e in jev) and not self.rt.state.positions:
            out.append(self._economics_event())  # flat again: today's round trip is complete
            assert self._econ is not None
            t = self._econ["today"]
            out.append(
                self._log(
                    "INFO",
                    f"economics: net of everything today {t['net']}",
                    act=(
                        "post_trade",
                        f"Round trip complete. Net of everything today {inr(t['net'])} (gross "
                        f"{inr(t['gross'])}, charges {inr(t['charges'])}, fixed-cost share "
                        f"{inr(t['fixed_share'])}, ASSUMED tax {inr(t['tax'])})",
                        "ok" if t["net"] >= 0 else "warn",
                    ),
                )
            )
        out.append(self._risk_event())
        pos = self._position_event(p)
        if pos is not None:
            out.append(pos)
        kills = self._kills_event()
        if kills is not None:
            out.append(kills)
        phase = str(self.kernel.market_clock.phase(p.ts))
        if phase != self._last_phase:
            self._last_phase = phase
            out.append(self._ev(EventKind.WINDOW, {"phase": phase}))
            out.append(
                self._log("INFO", f"trading window -> {phase}", act=("cio", _PHASE_TEXT.get(phase, phase), "info"))
            )
        out += self._tony_events()
        return out

    # ------------------------------------------------------------------ Tony (deterministic explanations)
    def _track_protective(self) -> None:
        unprotected = any(not pos.protective_confirmed for pos in self.rt.state.positions.values())
        if not unprotected:
            self._unprotected_since = None
        elif self._unprotected_since is None:
            self._unprotected_since = self.clock.t

    def _orb_status(self) -> str:
        if self._or is None:
            return "BUILDING_RANGE"
        if self._orb_blocked:
            return "STOOD_DOWN"
        t = self._trade
        if t is not None:
            if t.key in self.rt.state.positions:
                return "IN_TRADE"
            done = t.exit_requested is not None or any(ct.instrument_key == t.key for ct in self.rt.state.closed_trades)
            if not done:
                return "WORKING"
        if self._orb_trades >= 2:
            return "DONE"
        return "ARMED"

    def view(self) -> tony.SystemView:
        """The real state Tony explains: kernel state and journal-derived verdicts, kill switches, the
        SIMULATED strategy watches, the last market and economics snapshots."""
        st, k, lim = self.rt.state, self.kernel, self.kernel.limits
        w = k.window
        sod = st.sod_nav or NAV
        pos_v = None
        if st.positions:
            key, pos = next(iter(sorted(st.positions.items())))
            c = self._contracts.get(key)
            t = self._trade if self._trade is not None and self._trade.key == key else None
            bid = self._quote(c, self._last_point).bid if c is not None and self._last_point is not None else None
            pos_v = tony.PositionView(
                key,
                c.trading_symbol if c else key,
                pos.strategy_id,
                pos.qty,
                pos.avg_price.quantize(Decimal("0.01")),
                bid,
                t.stop_trigger if t else None,
                t.stop_limit if t else None,
                t.target if t else None,
                pos.protective_confirmed,
                pos.opened_at,
                st.unrealised,
                t.thesis if t else "",
            )
        latches = [
            tony.LatchView("KILL", str(sw), scope, lt.reason, lt.latched_at)
            for (sw, scope), lt in sorted(st.kills.items(), key=lambda kv: kv[1].latched_at)
        ] + [
            tony.LatchView("HALT", str(h), "", hl.reason, hl.latched_at)
            for h, hl in sorted(st.halts.items(), key=lambda kv: kv[1].latched_at)
        ]
        closed = tuple(
            tony.ClosedView(ct.strategy_id, self._sym(ct.instrument_key), ct.pnl, ct.stopped_out, ct.closed_at)
            for ct in st.closed_trades
        )
        rg = self._regime or {}
        now = self.clock.t
        cal = k.market_clock.calendar
        return tony.SystemView(
            now=now,
            session_day=self.scenario.day,
            next_session=cal.next_trading_day(self.scenario.day),
            phase=str(k.market_clock.phase(now)) if now >= self.points[0].ts else "BEFORE_WINDOW",
            entry_start=w.entry_start,
            entry_cutoff=w.entry_cutoff,
            flatten_start=w.flatten_start,
            hard_flat=w.hard_flat,
            spot=self._prev_spot,
            vix=rg.get("vix"),
            ret_30m_pct=rg.get("ret_30m_pct"),
            regime_tags=tuple(rg.get("tags", ())),
            regime=rg.get("label"),
            nav=st.nav,
            sod_nav=sod,
            realised=st.realised_today,
            unrealised=st.unrealised,
            daily_loss=st.daily_loss,
            daily_stop=lim.daily_stop_frac * sod,
            dd_frac=st.drawdown_frac,
            dd_warning=lim.dd_warning_frac,
            per_trade_budget=lim.per_trade_max_loss_frac * st.nav,
            position=pos_v,
            latches=tuple(latches),
            integrity_failed=self.rt.integrity_failed,
            broker_connected=self.broker.is_connected(),
            feed_age_s=None if self._last_tick is None else Decimal((now - self._last_tick).total_seconds()),
            decisions=tuple(self._decisions),
            closed=closed,
            strategies=tuple(
                tony.StrategyView(s.id, s.hypothesis, s.name, s.stage, st.strategy_killed(s.id)) for s in STRATEGIES
            ),
            orb=tony.OrbWatch(
                self._orb_status(),
                self._or[0] if self._or else None,
                self._or[1] if self._or else None,
                Decimal(8),
                self._orb_trades,
                2,
                time(14, 5),
                time(9, 30),
            ),
            vwap=tony.VwapWatch(
                (self._vw_sum / self._vw_n) if self._vw_n else None,
                Decimal(25),
                self._vw_sent,
                3,
                time(10, 30),
                time(13, 30),
            ),
            economics=self._econ,
            session_finished=self.finished,
            protective_unconfirmed_s=(
                None if self._unprotected_since is None else Decimal((now - self._unprotected_since).total_seconds())
            ),
        )

    def _tony_events(self, *, force: bool = False) -> list[DashEvent]:
        """A TONY event when the explanation materially changes, or (for refreshed answers such as P&L) at most
        once a simulated minute. A change of the attention level is also written to the activity log."""
        pl = tony.payload(self.view())
        key = tony.material(pl)
        now = self.clock.t
        due = self._tony_at is None or now - self._tony_at >= timedelta(seconds=60)
        if not force and key == self._tony_key and (pl["answers"] == self._tony_answers or not due):
            return []
        out: list[DashEvent] = []
        lvl = pl["attention"]["level"]
        if lvl != self._attention:
            tone = {"NORMAL": "ok", "ATTENTION": "warn", "INTERVENTION": "bad"}[lvl]
            label = {"NORMAL": "Back to normal", "ATTENTION": "Attention", "INTERVENTION": "Intervention required"}[lvl]
            out.append(
                self._log(
                    "WARN" if lvl != "NORMAL" else "INFO",
                    f"attention {self._attention} -> {lvl}: {pl['attention']['headline']}",
                    act=(
                        "cio",
                        f"{label}: {pl['attention']['headline'][0].lower()}{pl['attention']['headline'][1:]}",
                        tone,
                    ),
                )
            )
            self._attention = lvl
        self._tony_key, self._tony_answers, self._tony_at = key, pl["answers"], now
        out.append(self._ev(EventKind.TONY, pl))
        return out

    # ------------------------------------------------------------------ panels
    def _economics_event(self) -> DashEvent:
        """Net of everything for the SIMULATED day so far (docs/risk/system-economics.md), from the kernel's own
        journal. Only built
        while flat (the economics adapter refuses an open day). Advisory only: nothing here feeds the kernel."""
        k = self.kernel
        days = trading_days_from_journal(self.journal, k.costs, k.plan_id, simulated=True)
        stmt = build_statement(days, k.economics)
        snap = snapshot(stmt, cost_justification(stmt, k.economics))
        td, m = days[-1], stmt.latest
        share = m.fixed_total / self._month_trading_days
        pre_tax = td.gross_pnl - td.charges.total - share
        tax = max(pre_tax, Decimal(0)) * k.economics.tax.effective_rate
        cent = Decimal("0.01")
        g, c, f, tx = (q_tick(x, cent) for x in (td.gross_pnl, td.charges.total, share, tax))
        snap["today"] = {  # rounded first so the card's lines add up to its net exactly
            "gross": g,
            "brokerage": q_tick(td.charges.brokerage, cent),
            "statutory": c - q_tick(td.charges.brokerage, cent),
            "charges": c,
            "fixed_share": f,
            "month_trading_days": self._month_trading_days,
            "tax": tx,
            "net": g - c - f - tx,
            "executed_orders": td.executed_orders,
            "round_trips": td.round_trips,
        }
        snap["tax_rate"] = k.economics.tax.effective_rate
        snap["tax_status"] = str(k.economics.tax.status)
        self._econ = snap
        return self._ev(EventKind.ECONOMICS, snap)

    def _chain_event(self, p: MarketPoint) -> DashEvent:
        a = atm(p.spot)
        rows = []
        for k in range(-4, 5):
            s = a + k * STEP
            ce = option_quote(p.spot, s, True, p.ts, self.expiry, p.vix)
            pe = option_quote(p.spot, s, False, p.ts, self.expiry, p.vix)
            rows.append(
                {
                    "strike": s,
                    "ce": {"bid": ce.bid, "ask": ce.ask, "iv": ce.iv},
                    "pe": {"bid": pe.bid, "ask": pe.ask, "iv": pe.iv},
                }
            )
        return self._ev(EventKind.CHAIN, {"expiry": self.expiry, "atm": a, "spot": p.spot, "rows": rows})

    def _feed_regime(self, p: MarketPoint) -> None:
        """Aggregate the 15 s points into 1-minute bars; a bar is classified once the next minute's first point
        arrives (so a label is never computed from a bar that is still forming)."""
        minute = p.ts.replace(second=0, microsecond=0)
        if self._bar and self._bar[0].ts.replace(second=0, microsecond=0) != minute:
            pts = self._bar
            spots = [x.spot for x in pts]
            bar = Bar(
                "SIMULATED|NIFTY_INDEX",
                pts[0].ts.replace(second=0, microsecond=0),
                spots[0],
                max(spots),
                min(spots),
                spots[-1],
                0,
            )
            self._rc.on_bar(bar, vix=pts[-1].vix)
            self._bar = []
        self._bar.append(p)

    def _regime_event(self, i: int, p: MarketPoint) -> DashEvent:
        back = self.points[max(0, i - 120)].spot  # 30 minutes at 15 s
        ret = ((p.spot - back) / back * 100).quantize(Decimal("0.001"))
        lab: RegimeLabel | None = self._rc.last
        tags = sorted(t.value for t in lab.tags()) if lab is not None else [Regime.NO_EDGE.value]
        label: dict[str, Any] = (
            lab.as_json()
            if lab is not None
            else {"warmup": True, "status": self._rc.cfg.status, "classifier": self._rc.version}
        )
        self._regime = {"tags": tags, "ret_30m_pct": ret, "vix": p.vix, "label": label}
        return self._ev(
            EventKind.REGIME,
            {
                "tags": tags,
                "ret_30m_pct": ret,
                "vix": p.vix,
                "classifier": self._rc.version,
                "status": self._rc.cfg.status,
                "inputs": "SIMULATED",
                "warmup": label["warmup"],
                "trend": label.get("trend"),
                "volatility": label.get("volatility"),
                "gap": label.get("gap"),
                "opening": label.get("opening"),
                "classifier_agreement": label.get("classifier_agreement"),
                "classifier_agreement_note": "share of the classifier's voters agreeing; not a confidence",
            },
            _flow("market_intel", "cio", "regime"),
            _flow("cio", "allocator", "budget"),
            _flow("cio", "strategy_factory", "regime"),
        )

    def _risk_event(self) -> DashEvent:
        st, lim = self.rt.state, self.kernel.limits
        sod = st.sod_nav or NAV
        return self._ev(
            EventKind.RISK,
            {
                "nav": st.nav,
                "sod_nav": sod,
                "hwm": st.hwm,
                "day_pnl": st.realised_today + st.unrealised,
                "realised": st.realised_today,
                "unrealised": st.unrealised,
                "daily_stop": lim.daily_stop_frac * sod,
                "daily_loss": st.daily_loss,
                "dd_frac": st.drawdown_frac,
                "dd_warning_frac": lim.dd_warning_frac,
                "dd_suspend_frac": lim.dd_suspend_frac,
                "dd_hard_ceiling_frac": lim.dd_hard_ceiling_frac,
                "per_trade_budget": lim.per_trade_max_loss_frac * st.nav,
                "cash": self.broker.funds() if self.broker.is_connected() else None,
                "entries_today": st.entries_today,
                "trades_closed": len(st.closed_trades),
            },
        )

    def _position_event(self, p: MarketPoint) -> DashEvent | None:
        st = self.rt.state
        t = self._trade
        if not st.positions:
            sig: tuple[Any, ...] = ("flat",)
            data: dict[str, Any] = {"open": False}
        else:
            key, pos = next(iter(sorted(st.positions.items())))
            c = self._contracts.get(key)
            q = self._quote(c, p) if c is not None else None
            data = {
                "open": True,
                "key": key,
                "symbol": c.trading_symbol if c else key,
                "strategy": pos.strategy_id,
                "qty": pos.qty,
                "lots": pos.qty // max(1, pos.lot_size),
                "avg": pos.avg_price.quantize(Decimal("0.01")),
                "bid": q.bid if q else None,
                "ask": q.ask if q else None,
                "stop_trigger": t.stop_trigger if t and t.key == key else None,
                "stop_limit": t.stop_limit if t and t.key == key else None,
                "target": t.target if t and t.key == key else None,
                "protective_confirmed": pos.protective_confirmed,
                "unrealised": st.unrealised,
                "opened_at": pos.opened_at,
                "thesis": t.thesis if t and t.key == key else "",
                "intent_id": t.intent_id if t and t.key == key else "",
            }
            sig = (key, pos.qty, q.bid if q else None, pos.protective_confirmed, st.unrealised)
        if sig == self._last_pos_sig:
            return None
        self._last_pos_sig = sig
        return self._ev(EventKind.POSITION, data)

    def _kills_event(self) -> DashEvent | None:
        st = self.rt.state
        sig = tuple(sorted((str(sw), sc) for sw, sc in st.kills)) + tuple(sorted(("HALT", str(h)) for h in st.halts))
        if sig == self._last_kills:
            return None
        self._last_kills = sig
        switches = []
        for sw in KillSwitch:
            latches = [lt for (s, _), lt in st.kills.items() if s is sw]
            switches.append(
                {
                    "id": sw.value,
                    "latched": bool(latches),
                    "scope": [lt.scope for lt in latches],
                    "reason": latches[0].reason if latches else "",
                    "at": latches[0].latched_at if latches else None,
                }
            )
        halts = [{"kind": str(h), "reason": hl.reason, "at": hl.latched_at} for h, hl in st.halts.items()]
        return self._ev(EventKind.KILLS, {"switches": switches, "halts": halts})

    # ------------------------------------------------------------------ simulated strategies
    def _submit(
        self, sid: str, c: Contract, p: MarketPoint, limit: Decimal, trig: Decimal, stop: Decimal
    ) -> list[DashEvent]:
        self._intent_seq += 1
        iid = f"{sid}-{self.scenario.day:%m%d}-{self._intent_seq:03d}"
        q = self._quote(c, p)
        self.broker.set_quote(c.instrument_key, q.bid, q.ask)  # the fake broker must know a contract first seen now
        intent = TradeIntent(
            iid, sid, _STAGE[sid], c, OrderSide.BUY, LOT, limit, p.ts, stop_trigger=trig, stop_limit=stop,
            spec_stop_limit=stop,
        )  # fmt: skip
        idata = {
            "intent_id": iid,
            "strategy": sid,
            "stage": _STAGE[sid].value,
            "symbol": c.trading_symbol,
            "side": "BUY",
            "qty": LOT,
            "limit": limit,
            "stop_trigger": trig,
            "stop_limit": stop,
        }
        out = [
            self._ev(
                EventKind.INTENT,
                idata,
                _flow(_SNODE[sid], "allocator", "intent"),
                _flow("allocator", "risk_governor", "intent"),
            )
        ]
        cash = self.broker.funds() if self.broker.is_connected() else Decimal(0)
        d = self.rt.submit(intent, MarketSnapshot(p.ts, q, cash))
        sim_only = d.ticket is not None and d.ticket.simulate_only
        reasons = tuple(r.value for r in d.reasons)
        risk = d.risk_at_stop.quantize(Decimal("0.01")) if d.risk_at_stop is not None else None
        bud = d.budget.quantize(Decimal("0.01")) if d.budget is not None else None
        self._decisions.append(
            tony.DecisionView(
                p.ts,
                iid,
                sid,
                _STAGE[sid].value,
                c.trading_symbol,
                d.verdict.value,
                reasons,
                risk,
                bud,
                sim_only,
                limit,
            )
        )
        explain = tony.explain_reasons(reasons, risk, bud)
        flows = (
            [_flow("risk_governor", "execution", "approve")]
            if d.approved and not sim_only
            else [_flow("risk_governor", "allocator", "approve" if d.approved else "reject")]
        )
        out.append(
            self._ev(
                EventKind.DECISION,
                {
                    **idata,
                    "verdict": d.verdict.value,
                    "reasons": [r.value for r in d.reasons],
                    "risk_at_stop": d.risk_at_stop.quantize(Decimal("0.01")) if d.risk_at_stop is not None else None,
                    "budget": d.budget.quantize(Decimal("0.01")) if d.budget is not None else None,
                    "simulate_only": sim_only,
                    "explain": explain,
                },
                *flows,
            )
        )
        verdict = "APPROVED" + (" (simulate-only, SHADOW)" if sim_only else "") if d.approved else "REJECTED"
        why = "" if d.approved else " · " + ", ".join(r.value for r in d.reasons)
        order = f"BUY {LOT} {c.trading_symbol} at {tony.pts(limit)}"
        if d.approved:
            budget_txt = f", risk {inr(risk)} of the {inr(bud)} budget" if risk is not None and bud else ""
            human = (
                f"Risk Governor approved {sid} (simulate-only, {_STAGE[sid].value}): {order}{budget_txt}. "
                "No order is sent"
                if sim_only
                else f"Risk Governor approved {sid}: {order}{budget_txt}"
            )
        else:
            more = f" (+{len(explain) - 2} more)" if len(explain) > 2 else ""
            human = f"Risk Governor rejected {sid}: {'; '.join(explain[:2])}{more}"
        out.append(
            self._log(
                "OK" if d.approved else "WARN",
                f"Risk Governor {verdict} {iid} {c.trading_symbol}{why}",
                act=("risk_governor", human, ("ok" if not sim_only else "info") if d.approved else "reject"),
            )
        )
        if d.approved and not sim_only and sid == ORB:
            thesis = self._pending_thesis + (
                f" The Risk Governor approved it: {inr(risk)} at the stop against the {inr(bud)} budget."
                if risk is not None and bud is not None
                else ""
            )
            self._trade = _Trade(
                c.instrument_key, c, trig, stop, limit + Decimal("1.5") * (limit - trig), limit, None, iid, thesis
            )
            self._orb_trades += 1
        if not d.approved and sid == ORB:
            # a REJECT is final (docs/risk/risk-engine.md): no retry with modified parameters today
            self._orb_blocked = True
        return out

    def _orb(self, p: MarketPoint) -> list[DashEvent]:
        if self._or is None or self._orb_blocked or self._trade is not None or self._orb_trades >= 2:
            return []
        t = p.ts.astimezone(IST).time()
        if t.second or t.minute % 5 or t > time(14, 5):
            return []
        hi, lo = self._or
        buf = Decimal(8)
        right = OptionRight.CE if p.spot > hi + buf else OptionRight.PE if p.spot < lo - buf else None
        if right is None:
            return []
        # sizing rule of the SIMULATED strategy: the most-ATM OTM strike whose ask is <= 40, stop 1.5 points below
        a = atm(p.spot)
        for k in range(1, 12):
            strike = a + k * STEP if right is OptionRight.CE else a - k * STEP
            c = self._contract(strike, right)
            q = self._quote(c, p)
            assert q.ask is not None
            if q.ask <= 40:
                trig = q.ask - Decimal("1.5")
                up = right is OptionRight.CE
                side = "above" if up else "below"
                edge = hi if up else lo
                self._pending_thesis = (
                    f"S-ORB-001 bought {c.trading_symbol} at {tony.pts(q.ask)} after NIFTY broke {side} the opening "
                    f"range ({tony.pts(lo)}-{tony.pts(hi)}) by more than {buf:.0f} points: {tony.pts(p.spot)} at "
                    f"{tony.hhmm(p.ts)}. Sizing rule: the nearest OTM strike with an ask of at most 40, stop 1.5 "
                    "points below the entry."
                )
                return [
                    self._log(
                        "INFO",
                        f"S-ORB-001 breakout {'UP' if up else 'DOWN'} at {p.spot}",
                        act=(
                            _SNODE[ORB],
                            f"Breakout {side} the opening range ({tony.pts(edge)}) at {tony.pts(p.spot)}: proposing "
                            f"BUY {c.trading_symbol}",
                            "info",
                        ),
                    ),
                    *self._submit(ORB, c, p, q.ask, trig, trig - 10 * TICK),
                ]
        return []

    def _vwapc(self, p: MarketPoint) -> list[DashEvent]:
        t = p.ts.astimezone(IST).time()
        if self._vw_sent >= 3 or t.second or t.minute % 15 or t < time(10, 30) or t > time(13, 30):
            return []
        vwap = self._vw_sum / self._vw_n
        if abs(p.spot - vwap) > Decimal(25):
            return []
        right = OptionRight.CE if p.spot >= vwap else OptionRight.PE
        c = self._contract(atm(p.spot), right)  # ATM with the spec's 30% premium stop
        q = self._quote(c, p)
        assert q.ask is not None
        trig = (q.ask * Decimal("0.70") / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK
        self._vw_sent += 1
        return [
            self._log(
                "INFO",
                f"S-VWAPC-001 pullback to VWAP {vwap.quantize(Decimal('0.1'))} (SHADOW)",
                act=(
                    _SNODE[VWAPC],
                    f"Pullback to VWAP {tony.pts(vwap)} (spot {tony.pts(p.spot)}): proposing a simulate-only "
                    f"BUY {c.trading_symbol} (SHADOW)",
                    "info",
                ),
            ),
            *self._submit(VWAPC, c, p, q.ask, trig, trig - 4 * TICK),
        ]

    def _manage_trade(self, p: MarketPoint) -> list[DashEvent]:
        t = self._trade
        if t is None:
            return []
        if t.key not in self.rt.state.positions:
            # not open yet (entry working) or already closed (stop, exit, flatten)
            if t.exit_requested is not None or any(ct.instrument_key == t.key for ct in self.rt.state.closed_trades):
                self._trade = None
            return []
        q = self._quote(t.contract, p)
        tod = p.ts.astimezone(IST).time()
        reason = None
        if q.bid is not None and q.bid >= t.target:
            reason = "TARGET 1.5R"
        elif tod >= time(14, 30):
            reason = "TIME_EXIT 14:30"
        if reason is None and t.exit_requested is None:
            return []
        t.exit_requested = t.exit_requested or reason
        if self.rt.request_exit(t.key, t.exit_requested or ""):
            why = {"TARGET 1.5R": "target (1.5R) reached", "TIME_EXIT 14:30": "time exit at 14:30"}.get(
                t.exit_requested or "", t.exit_requested or ""
            )
            return [
                self._log(
                    "INFO",
                    f"S-ORB-001 exit: {t.exit_requested} (sell-to-close)",
                    act=(_SNODE[ORB], f"Exiting {t.contract.trading_symbol}: {why} (sell-to-close)", "info"),
                )
            ]
        return []

    # ------------------------------------------------------------------ journal -> events (read-only)
    def _journal_events(self) -> list[DashEvent]:
        out: list[DashEvent] = []
        recs: list[JournalRecord] = list(self.journal.records(from_seq=self._journal_seq + 1))
        cent = Decimal("0.01")
        for r in recs:
            self._journal_seq = r.seq
            pl, et = r.payload, r.event_type
            if et == "ORDER_SUBMITTED":
                sym = self._sym(pl["instrument_key"])
                self._orders[pl["client_order_id"]] = {
                    "kind": pl["kind"],
                    "side": pl["side"],
                    "symbol": sym,
                    "strategy": pl["strategy_id"],
                }
                out.append(
                    self._ev(
                        EventKind.ORDER,
                        {
                            "client_order_id": pl["client_order_id"],
                            "kind": pl["kind"],
                            "side": pl["side"],
                            "qty": pl["qty"],
                            "price": pl["price"],
                            "key": pl["instrument_key"],
                            "symbol": sym,
                        },
                        _flow("execution", "broker", "order"),
                    )
                )
                what = _ORDER_KIND_TEXT.get(str(pl["kind"]), f"{pl['kind']} order")
                out.append(
                    self._log(
                        "INFO",
                        f"order {pl['kind']} {pl['side']} {pl['qty']} @ {pl['price']} -> fake broker",
                        act=(
                            "execution",
                            f"{what} sent: {pl['side']} {pl['qty']} {sym} at {tony.pts(pl['price'])}",
                            "info",
                        ),
                    )
                )
            elif et == "FILL":
                o = self._orders.get(pl["client_order_id"], {})
                sym, side = o.get("symbol", "?"), o.get("side", "?")
                out.append(
                    self._ev(
                        EventKind.FILL,
                        {
                            "client_order_id": pl["client_order_id"],
                            "qty": pl["qty"],
                            "price": pl["price"],
                            "charges": pl["charges"],
                            "side": side,
                            "symbol": sym,
                            "kind": o.get("kind", "?"),
                        },
                        _flow("broker", "execution", "fill"),
                        _flow("execution", "post_trade", "fill"),
                    )
                )
                out.append(
                    self._log(
                        "OK",
                        f"FILL {pl['qty']} @ {pl['price']} (charges {q_tick(pl['charges'], cent)})",
                        act=(
                            "broker",
                            f"Order executed: {side} {pl['qty']} {sym} at {inr(pl['price'])} "
                            f"(charges {inr(pl['charges'])})",
                            "ok",
                        ),
                    )
                )
            elif et == "PROTECTIVE_CONFIRMED":
                out.append(
                    self._log(
                        "OK",
                        f"protective SL resting at broker for {pl['instrument_key']}",
                        _flow("broker", "execution", "ack"),
                        act=("broker", f"Broker-side stop confirmed for {self._sym(pl['instrument_key'])}", "ok"),
                    )
                )
            elif et == "ORDER_TERMINAL":
                o = self._orders.get(pl["client_order_id"], {})
                what = _ORDER_KIND_TEXT.get(str(o.get("kind", "")), "Order")
                out.append(
                    self._log(
                        "INFO",
                        f"order {pl['client_order_id']} -> {pl['status']}",
                        act=("execution", f"{what} for {o.get('symbol', '?')}: {str(pl['status']).lower()}", "muted"),
                    )
                )
            elif et == "KILL_LATCHED":
                name = tony.KILL_SHORT.get(str(pl["switch"]), str(pl["switch"]))
                out.append(
                    self._log(
                        "KILL",
                        f"{pl['switch']} LATCHED: {pl['reason']}",
                        _flow("risk_governor", "cio", "kill"),
                        _flow("risk_governor", "execution", "kill"),
                        act=("risk_governor", f"{name} kill switch latched: {pl['reason']}", "bad"),
                    )
                )
                out.append(self._strategies_event())
            elif et == "HALT_LATCHED":
                out.append(
                    self._log(
                        "KILL",
                        f"HALT {pl['kind']}: {pl['reason']}",
                        _flow("risk_governor", "cio", "kill"),
                        act=("risk_governor", f"Trading halt {pl['kind']}: {pl['reason']}", "bad"),
                    )
                )
            elif et == "ALERT":
                lvl = "KILL" if pl["severity"] == "URGENT" else "WARN" if pl["severity"] == "WARN" else "INFO"
                tone = {"KILL": "bad", "WARN": "warn"}.get(lvl, "info")
                out.append(
                    self._log(
                        lvl,
                        f"ALERT {pl['severity']}: {pl['message']}",
                        act=("risk_governor", f"Owner alert ({str(pl['severity']).lower()}): {pl['message']}", tone),
                    )
                )
            elif et in ("EXIT_ALL_REQUESTED", "EXIT_ALL_RESULT", "POSITION_ADOPTED"):
                out.append(
                    self._log("KILL", f"{et}: {pl}", act=("execution", et.replace("_", " ").capitalize(), "bad"))
                )
        return out

    # ------------------------------------------------------------------ owner control
    def manual_master_kill(self, reason: str, requested_by: str) -> dict[str, Any]:
        if threading.get_ident() != self._owner:
            raise DashboardError("the kill must run on the simulator's own thread (use LiveRunner.kill_port())")
        with self._lock:
            if self.finished:
                raise DashboardError("this simulated session has ended")
            latched = self.rt.manual_master_kill(reason, requested_by=requested_by)
            if self.rt.integrity_failed or (KillSwitch.MANUAL_MASTER, "") not in self.rt.state.kills:
                raise DashboardError("MANUAL_MASTER_KILL did not latch (journal/system integrity failure)")
            return {"latched": latched, "already_latched": not latched, "at": self.clock.t.isoformat()}

    def drain_after_kill(self) -> list[DashEvent]:
        """Events produced by a kill request (journal records, kill panel) so the bus shows them at once."""
        with self._lock:
            out = self._journal_events()
            k = self._kills_event()
            if k is not None:
                out.append(k)
            out.append(self._strategies_event())
            out += self._tony_events(force=True)
            return out


def build_replay(scenario: Scenario, kernel: Kernel, *, step_s: int = 15) -> list[dict[str, Any]]:
    """A whole SIMULATED day as a list of events (seq from 1). Deterministic per scenario."""
    with tempfile.TemporaryDirectory(prefix="p100c-replay-") as d:
        sess = SimulatedSession(scenario, kernel, Path(d), step_s=step_s)
        out = [e.with_seq(i + 1).as_dict() for i, e in enumerate(sess.run())]
        sess.journal.close()
    return out

"""The host's composition root (K-12, docs/engineering/security.md): one entrypoint that wires the whole paper/shadow
system.

    python -m project100c.ops.host_main --config configs/host/host.toml [--mode replay|paper|live]

Real-money trading is OFF. ``live`` is refused by this build (``LIVE_ENABLED_IN_THIS_BUILD = False``) and, even in
a build that allowed it, needs the config lock opened, the owner's sign-off and the go-live checklist's hash
(`live_lock_problems`). Nothing here constructs an order-placing broker adapter: the broker is always the
SIMULATED `PaperBroker`.

What one process runs:

* **Config and credentials.** ``configs/host/host.toml`` plus the environment; credentials from the environment or
  the encrypted store (`ops.credstore`), every value registered with the log and alert redactor; structured JSON
  logs on stdout.
* **Feed.** ``feed = auto``: Upstox Market Data Feed V3 when the app keys exist (connected after the day's token
  arrives on the notifier webhook), else a SYNTHETIC replay day. ``feed = upstox`` without keys exits 2.
* **Decide.** `ops.shadow.ShadowStrategies`: the regime classifier and every library plug-in on each closed
  1-minute bar, signals recorded as SHADOW (no intents: every spec is RESEARCH).
* **Order engine.** `RiskGovernor` (paper venue, signed tickets) -> `ExecutionGateway` -> `PaperBroker`, the
  `KernelRuntime` on a persistent journal, the `ReconcilerService`, all under `HostService` supervision. Idle in
  shadow mode (no intents reach it); in replay mode the same order path is rehearsed on each SYNTHETIC day with
  `PaperLoop` (allocator, Governor, kernel, fake broker), every strategy treated as PAPER for mechanics only.
* **Daily gate.** `TokenGate`: request 08:45, deadline 09:05 IST, fail closed. In replay the approval is SIMULATED.
* **Notifier.** Telegram when its settings exist (alerts plus /status, /kill, /deny), always through the
  redactor; every alert also goes to the log.
* **Scheduler.** `ops.scheduler.MarketScheduler` (IST): token gate, entries 09:20-14:00, flatten 14:50, flat by
  15:00, end of day 15:35 (journal backup with retention, daily report).
* **Status.** `ops.status_server` on loopback: public status, private detail behind the proxy, ``/healthz``.

Replay mode runs on an accelerated simulated clock (``step_s`` simulated seconds per step) and exits 0 after the
last day (or keeps serving the status page with ``hold_after``). Exit codes: 0 stopped, 2 configuration or
credential error, 3 live refused, 70 too many failed steps (the supervisor restarts the process from the journal).
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import logging
import signal
import sys
import threading
import time as _time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from project100c.alpha import spec_dirs
from project100c.broker.paper import PaperBroker
from project100c.calendar import MarketClock
from project100c.execution import ExecutionGateway, ReconcilerService
from project100c.kernel.governor import MarketSnapshot, RiskGovernor, TradeIntent
from project100c.kernel.regime_gate import RegimeGate
from project100c.kernel.runtime import KernelRuntime
from project100c.kernel.tickets import TicketSigner
from project100c.market_types import Bar, Quote
from project100c.ops.credstore import CredentialStoreError, register_all, resolve_credentials
from project100c.ops.daily_gate import TokenGate
from project100c.ops.host import EXIT_FAILED, EXIT_STOPPED, HostService, backup_journal, open_journal
from project100c.ops.redact import JsonFormatter, RedactingAlerts, Redactor, install_log_redaction
from project100c.ops.scheduler import DayEvent, MarketScheduler, ScheduleTimes
from project100c.ops.shadow import BarBuilder, FeedKeys, ReplayFeed, ShadowStrategies
from project100c.ops.status_server import StatusBoard, serve_status
from project100c.paper.live import LivePaperSession
from project100c.paper.loop import PaperKernel, PaperLoop
from project100c.sessions import IST
from project100c.spec.io import load_spec_file
from project100c.spec.models import StrategySpec
from project100c.strategies.library import PLUGINS
from project100c.synthetic import DayPlan, SyntheticDay, generate_days

# real money stays OFF until docs/risk/canary-criteria.md is signed and a strategy passes
LIVE_ENABLED_IN_THIS_BUILD: Final = False
EXIT_CONFIG = 2
EXIT_LIVE_REFUSED = 3
MODES = ("replay", "paper", "live")
FEEDS = ("auto", "upstox", "replay")
LABELS = (
    "PAPER / SHADOW: real money is OFF; the broker is the SIMULATED paper broker",
    "every strategy spec is RESEARCH: signals are recorded in SHADOW, none is traded",
    "regime classifier UNVALIDATED; thresholds ASSUMED; no edge is claimed",
)
log = logging.getLogger("project100c.host")


# ----------------------------------------------------------------------------------------------- configuration
@dataclass(frozen=True, slots=True)
class LiveLock:
    unlocked: bool = False
    owner_signoff: str = ""
    checklist_sha256: str = ""


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    seed: int = 7
    start: date = date(2026, 10, 5)
    days: int = 1
    step_s: float = 15.0
    pace_s: float = 0.0
    rehearse_orders: bool = True
    hold_after: bool = False
    rehearsal_nav: Decimal = Decimal(1_000_000)  # HYPOTHETICAL (see host.toml)


@dataclass(frozen=True, slots=True)
class HostConfig:
    mode: str
    feed: str
    state_dir: Path
    nav: Decimal
    interval_s: float = 1.0
    status_port: int = 8780
    webhook_port: int = 8781
    backup_keep: int = 30
    max_consecutive_failures: int = 5
    schedule: ScheduleTimes = field(default_factory=ScheduleTimes)
    keys: FeedKeys = field(default_factory=lambda: FeedKeys("NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX"))
    upstox_base_url: str = "https://api.upstox.com"
    upstox_feed_mode: str = "full"
    replay: ReplayConfig = field(default_factory=ReplayConfig)
    live_lock: LiveLock = field(default_factory=LiveLock)
    config_version: str = ""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {self.mode!r}")
        if self.feed not in FEEDS:
            raise ValueError(f"feed must be one of {FEEDS}, not {self.feed!r}")
        if self.nav <= 0 or self.interval_s <= 0 or self.backup_keep < 1 or self.replay.days < 1:
            raise ValueError("nav, interval_s, backup_keep and replay.days must be positive")
        if self.replay.step_s <= 0 or self.replay.step_s > 60:
            raise ValueError("replay.step_s must be in (0, 60]")


def _t(v: Any, default: time) -> time:
    return default if v is None else time.fromisoformat(str(v))


def load_host_config(path: Path, *, mode: str | None = None, state_dir: Path | None = None) -> HostConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    h, s, k, u = raw.get("host", {}), raw.get("schedule", {}), raw.get("feed_keys", {}), raw.get("upstox", {})
    r, lk = raw.get("replay", {}), raw.get("live_lock", {})
    d = ScheduleTimes()
    return HostConfig(
        mode=mode or str(h.get("mode", "replay")),
        feed=str(h.get("feed", "auto")),
        state_dir=state_dir or Path(str(h.get("state_dir", "/var/lib/project100c"))),
        nav=Decimal(str(h.get("nav", "10000"))),
        interval_s=float(h.get("interval_s", 1.0)),
        status_port=int(h.get("status_port", 8780)),
        webhook_port=int(h.get("webhook_port", 8781)),
        backup_keep=int(h.get("backup_keep", 30)),
        max_consecutive_failures=int(h.get("max_consecutive_failures", 5)),
        schedule=ScheduleTimes(
            _t(s.get("token_request"), d.token_request),
            _t(s.get("gate_deadline"), d.gate_deadline),
            _t(s.get("end_of_day"), d.end_of_day),
        ),
        keys=FeedKeys(
            str(k.get("index", "NSE_INDEX|Nifty 50")), str(k.get("vix", "")) or None, str(k.get("fut", "")) or None
        ),
        upstox_base_url=str(u.get("base_url", "https://api.upstox.com")),
        upstox_feed_mode=str(u.get("feed_mode", "full")),
        replay=ReplayConfig(
            int(r.get("seed", 7)),
            date.fromisoformat(str(r.get("start", "2026-10-05"))),
            int(r.get("days", 1)),
            float(r.get("step_s", 15)),
            float(r.get("pace_s", 0.0)),
            bool(r.get("rehearse_orders", True)),
            bool(r.get("hold_after", False)),
            Decimal(str(r.get("rehearsal_nav", "1000000"))),
        ),
        live_lock=LiveLock(
            bool(lk.get("unlocked", False)), str(lk.get("owner_signoff", "")), str(lk.get("checklist_sha256", ""))
        ),
        config_version=str(raw.get("config_version", "")),
    )


def build_allows_live() -> bool:
    return bool(LIVE_ENABLED_IN_THIS_BUILD)


def live_lock_problems(lock: LiveLock, checklist: Path) -> list[str]:
    """Every reason live trading may not start. Empty only if all of the locks are open (never in this build)."""
    out: list[str] = []
    if not build_allows_live():
        out.append("live trading is disabled in this build (LIVE_ENABLED_IN_THIS_BUILD = False)")
    if not lock.unlocked:
        out.append("[live_lock] unlocked is false")
    if not lock.owner_signoff.strip():
        out.append(
            "[live_lock] owner_signoff is empty "
            "(docs/risk/canary-criteria.md and the go-live checklist need the owner's sign-off)"
        )
    digest = hashlib.sha256(checklist.read_bytes()).hexdigest() if checklist.is_file() else None
    if digest is None:
        out.append(f"go-live checklist not found at {checklist}")
    elif lock.checklist_sha256.lower() != digest:
        out.append("[live_lock] checklist_sha256 does not match the go-live checklist as it stands")
    return out


def trading_days(clock: MarketClock, start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if clock.calendar.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# ----------------------------------------------------------------------------------------------- small parts
class SimClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class LogAlerts:
    """Every alert goes to the structured log and to a short in-memory history (for the private status)."""

    def __init__(self, keep: int = 50) -> None:
        self.recent: collections.deque[tuple[str, str, str]] = collections.deque(maxlen=keep)
        self.counts: collections.Counter[str] = collections.Counter()

    def send(self, severity: str, message: str) -> None:
        self.counts[severity] += 1
        self.recent.append((datetime.now(IST).isoformat(timespec="seconds"), severity, message))
        level = logging.ERROR if severity == "URGENT" else logging.WARNING if severity == "WARNING" else logging.INFO
        log.log(level, message, extra={"event": "ALERT"})


class Tee:
    def __init__(self, *sinks: Any) -> None:
        self.sinks = sinks

    def send(self, severity: str, message: str) -> None:
        for s in self.sinks:
            try:
                s.send(severity, message)
            except Exception as e:  # one broken channel must not silence the others
                log.error("alert sink %s failed: %s", type(s).__name__, type(e).__name__)


class ThrottledCommands:
    """Poll the owner's command channel at most every ``every`` seconds (Telegram getUpdates)."""

    def __init__(self, inner: Any, clock: Callable[[], datetime], every: float = 5.0) -> None:
        self.inner, self.clock, self.every = inner, clock, timedelta(seconds=every)
        self._last: datetime | None = None

    def poll_commands(self) -> Sequence[Any]:
        now = self.clock()
        if self._last is not None and now - self._last < self.every:
            return []
        self._last = now
        return list(self.inner.poll_commands())


class LiveFeedHolder:
    """The broker feed exists only once the day's token has arrived (webhook thread); until then: no quotes."""

    def __init__(self, factory: Callable[[Any], Any], keys: Sequence[str], redactor: Redactor) -> None:
        self.factory, self.keys, self.redactor = factory, list(keys), redactor
        self.feed: Any = None
        self._lock = threading.Lock()

    def on_session(self, session: Any) -> None:
        self.redactor.register(session.access_token)
        feed = self.factory(session)
        feed.subscribe(self.keys)
        with self._lock:
            old, self.feed = self.feed, feed
        if old is not None:
            old.close()

    def poll(self) -> Mapping[str, Quote]:
        with self._lock:
            feed = self.feed
        return {} if feed is None else feed.poll()

    def health(self, now: datetime) -> str:
        with self._lock:
            feed = self.feed
        if feed is None:
            return "WAITING_FOR_TOKEN"
        return "HEALTHY" if feed.healthy(now) else "DOWN"


class NoFeed:
    def poll(self) -> Mapping[str, Quote]:
        return {}


@dataclass
class _DayFeed:
    """The current day's replay feed, swapped in at each day's start (so the session's quote source stays one)."""

    feed: ReplayFeed | None = None

    def poll(self) -> Mapping[str, Quote]:
        return {} if self.feed is None else self.feed.poll()


def load_library_specs(specs_dir: Path) -> list[StrategySpec]:
    """Library specs with a plug-in: the public ``specs/`` plus the private alpha library's, if present."""
    out = []
    for p in (p for d in spec_dirs(specs_dir) for p in sorted(d.glob("*.yaml"))):
        s = load_spec_file(p)
        if s.id in PLUGINS:
            out.append(s)
    return out


def configure_logging(redactor: Redactor, *, fmt: str = "json", stream: Any = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(redactor, service="host") if fmt == "json" else logging.Formatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    install_log_redaction(redactor, root)


# ----------------------------------------------------------------------------------------------- the host
class Host:
    def __init__(
        self,
        cfg: HostConfig,
        *,
        root: Path,
        env: Mapping[str, str] | None = None,
        forbid_under: Path | None = None,
        serve: bool = True,
        redactor: Redactor | None = None,
    ) -> None:
        self.cfg, self.root = cfg, root
        self.redactor = redactor or Redactor()
        self.creds = resolve_credentials(env)
        register_all(self.redactor, self.creds, env)
        self.kernel = PaperKernel.load(root / "configs")
        self.mc = self.kernel.market_clock
        self.specs = load_library_specs(root / "specs")
        self.replay = cfg.mode == "replay"
        self.feed_kind = self._feed_kind()
        self.log_alerts = LogAlerts()
        self.telegram: Any = None
        sinks: list[Any] = [self.log_alerts]
        if not self.replay and self.creds.get("TELEGRAM_BOT_TOKEN") and self.creds.get("TELEGRAM_CHAT_ID"):
            from project100c.notify.telegram import TelegramNotifier, load_telegram_config

            self.telegram = TelegramNotifier(load_telegram_config(self.creds), clock=lambda: datetime.now(IST))
            sinks.append(self.telegram)
        self.alerts = RedactingAlerts(Tee(*sinks), self.redactor)
        if cfg.mode == "paper" and self.feed_kind == "replay":
            self.alerts.send("WARNING", "paper mode without broker keys: the feed is a SIMULATED replay day")
        # clock: simulated in replay, the wall clock otherwise
        self.days: list[SyntheticDay] = []
        if self.replay:
            plans = [DayPlan(d, cfg.replay.seed) for d in trading_days(self.mc, cfg.replay.start, cfg.replay.days)]
            self.days = generate_days(plans)
            self.sim: SimClock | None = SimClock(self._day_begin(self.days[0].plan.day))
            self.clock: Callable[[], datetime] = self.sim
        else:
            self.sim = None
            self.clock = lambda: datetime.now(IST)
        self.scheduler = MarketScheduler(self.mc, cfg.schedule)
        self.board = StatusBoard(max_step_age=max(30.0, 10 * cfg.interval_s))
        # order engine (paper venue only)
        state = cfg.state_dir
        self.journal_path = state / "journal" / "host-journal.sqlite"
        self.journal = open_journal(self.journal_path, clock=self.clock, forbid_under=forbid_under)
        self.broker = PaperBroker(self.clock, funds=cfg.nav)
        signer = TicketSigner()
        gov = RiskGovernor(self.kernel.limits, self.mc, self.kernel.costs, self.kernel.plan_id,
                           regime_gate=RegimeGate.from_specs(self.specs), paper_venue=True,
                           ticket_signer=signer)  # fmt: skip
        self.runtime = KernelRuntime(
            self.journal,
            ExecutionGateway(self.broker, self.clock, ticket_signer=signer),
            gov,
            self.kernel.costs,
            self.kernel.plan_id,
            self.clock,
            self.alerts,
        )
        self.shadow = ShadowStrategies(self.specs, self.kernel.regime, self.kernel.expiries)
        self._bars = BarBuilder(frozenset(k for k in (cfg.keys.index, cfg.keys.vix, cfg.keys.fut) if k))
        self._aux: dict[tuple[str, datetime], Bar] = {}
        # feed and gate
        self.webhook: Any = None
        self.live_feed: LiveFeedHolder | None = None
        self.day_feed = _DayFeed()
        quotes: Any
        if self.feed_kind == "upstox":
            quotes = self._wire_upstox()
        elif self.feed_kind == "replay":
            quotes = self.day_feed
        else:
            quotes = NoFeed()
        if self.feed_kind != "upstox":
            self.gate = TokenGate(lambda: self.clock() + timedelta(minutes=15), self.alerts,
                                  broker_name="SIMULATED broker", deadline=cfg.schedule.gate_deadline)  # fmt: skip
        commands = ThrottledCommands(self.telegram, self.clock) if self.telegram is not None else None
        self.session = LivePaperSession(
            self.runtime, self.broker, quotes, self._decide, self.clock, self.alerts, gate=self.gate, commands=commands
        )
        self.reconciler = ReconcilerService(
            lambda: self.runtime.state,
            self.broker,
            self.alerts,
            self.runtime.report_reconciliation_mismatch,
            unknown_orders=lambda: self.runtime.unknown_orders,
        )
        self.service = HostService(
            self.session,
            self.clock,
            self.alerts,
            reconciler=self.reconciler,
            interval_s=cfg.interval_s,
            max_consecutive_failures=cfg.max_consecutive_failures,
        )
        self.stop = self.service.stop
        self.status_srv: Any = None
        if serve:
            self.status_srv = serve_status(
                self.board, cfg.status_port, proxy_secret=self.creds.get("P100C_PROXY_SHARED")
            )
        self.started_at = self.clock()
        self.day: date | None = None
        self.day_open = False
        self.reports: list[dict[str, Any]] = []
        self.backups: list[Path] = []

    # -- wiring helpers
    def _feed_kind(self) -> str:
        has_keys = bool(self.creds.get("UPSTOX_API_KEY") and self.creds.get("UPSTOX_API_SECRET"))
        if self.replay:
            return "replay"
        if self.cfg.feed == "upstox" and not has_keys:
            raise CredentialStoreError("feed = upstox but UPSTOX_API_KEY / UPSTOX_API_SECRET are not configured")
        if self.cfg.feed == "replay" or (self.cfg.feed == "auto" and not has_keys):
            return "replay"
        return "upstox"

    def _wire_upstox(self) -> LiveFeedHolder:
        from project100c.broker.upstox.auth import request_access_token
        from project100c.broker.upstox.credentials import load_app_credentials
        from project100c.broker.upstox.feed import UpstoxFeed
        from project100c.broker.upstox.transport import HttpTransport
        from project100c.broker.upstox.webhook import NotifierReceiver
        from project100c.broker.upstox.webhook import serve as serve_webhook

        app = load_app_credentials(self.creds)
        path = self.creds.get("P100C_WEBHOOK_PATH", "")
        cfg = self.cfg

        def request() -> datetime:
            return request_access_token(app, HttpTransport(), base_url=cfg.upstox_base_url).authorization_expiry

        self.gate = TokenGate(request, self.alerts, broker_name="Upstox", deadline=cfg.schedule.gate_deadline)
        keys = [k for k in (cfg.keys.index, cfg.keys.vix, cfg.keys.fut) if k]
        holder = LiveFeedHolder(
            lambda s: UpstoxFeed(s, self.clock, base_url=cfg.upstox_base_url, mode=cfg.upstox_feed_mode),
            keys,
            self.redactor,
        )
        receiver = NotifierReceiver(app.api_key, path, self.gate, holder.on_session, self.clock)
        self.webhook = serve_webhook(receiver, "127.0.0.1", cfg.webhook_port)
        self.live_feed = holder
        return holder

    def _day_begin(self, d: date) -> datetime:
        return datetime.combine(d, self.cfg.schedule.token_request, tzinfo=IST) - timedelta(minutes=5)

    # -- decide: shadow strategies, never an intent
    def _decide(self, now: datetime, q: Mapping[str, Quote]) -> list[tuple[TradeIntent, MarketSnapshot]]:
        if self.feed_kind == "replay":
            if self.day_feed.feed is not None:
                for idx, fut, vix in self.day_feed.feed.closed_bars():
                    self.shadow.on_bar(idx, fut, vix)
            return []
        k = self.cfg.keys
        for b in [*self._bars.on_quotes(q), *self._bars.flush_before(now)]:
            if b.instrument_key != k.index:
                self._aux[(b.instrument_key, b.start)] = b
                continue
            if self.shadow.day != b.start.astimezone(IST).date():
                self.shadow.start_day(b.start.astimezone(IST).date(), b.open)  # prev close unknown: gap 0 (ASSUMED)
            fut = self._aux.pop((k.fut, b.start), None) if k.fut else None
            vix = self._aux.pop((k.vix, b.start), None) if k.vix else None
            self.shadow.on_bar(b, fut, vix)
        cutoff = now - timedelta(minutes=5)
        self._aux = {kk: v for kk, v in self._aux.items() if v.start >= cutoff}
        return []

    # -- scheduled events
    def _open_day(self, d: date, now: datetime) -> None:
        self.day, self.day_open = d, True
        self.shadow.journal = self.cfg.state_dir / "shadow" / f"{d}.jsonl"
        prev = self.runtime.state.trading_date
        monday = d - timedelta(days=d.weekday())
        self.service.ensure_day_started(d, self.cfg.nav, week_start=prev is None or prev < monday)
        if self.feed_kind == "replay":
            sd = self._synthetic_day(d)
            self.day_feed.feed = ReplayFeed(sd, self.clock, self.cfg.keys)
            self.shadow.start_day(d, Decimal(repr(sd.plan.prev_close)))
        self.gate.start(now)
        if self.feed_kind != "upstox":
            self.gate.on_token(datetime.combine(d + timedelta(days=1), time(3, 30), tzinfo=IST), now)
            self.alerts.send("INFO", "SIMULATED token approval (no broker keys: replay feed)")

    def _synthetic_day(self, d: date) -> SyntheticDay:
        for sd in self.days:
            if sd.plan.day == d:
                return sd
        (sd,) = generate_days([DayPlan(d, int(d.strftime("%Y%m%d")))])
        return sd

    def on_event(self, ev: DayEvent, now: datetime) -> None:
        log.info("schedule %s", ev.value, extra={"event": ev.value, "mode": self.cfg.mode})
        if ev is DayEvent.TOKEN_REQUEST:
            self._open_day(now.astimezone(IST).date(), now)
        elif ev is DayEvent.GATE_DEADLINE:
            self.gate.tick(now)
            if not self.gate.trading_allowed(now):
                self.alerts.send("URGENT", f"09:05 gate: {self.gate.state}. No trading today; the system stays flat.")
        elif ev is DayEvent.HARD_FLAT:
            st = self.runtime.state
            if st.positions or st.open_orders:
                self.alerts.send(
                    "URGENT", f"15:00: NOT FLAT ({len(st.positions)} positions, {len(st.open_orders)} orders)"
                )
        elif ev is DayEvent.END_OF_DAY and self.day_open:
            self._end_of_day(now)

    def _end_of_day(self, now: datetime) -> None:
        self.day_open = False
        state = self.cfg.state_dir
        self.backups.append(backup_journal(self.journal_path, state / "backups", now))
        for old in sorted((state / "backups").glob("host-journal-*.sqlite"))[: -self.cfg.backup_keep]:
            old.unlink()
        st = self.runtime.state
        report: dict[str, Any] = {
            "day": str(self.day),
            "mode": self.cfg.mode,
            "feed": self.feed_kind,
            "real_money": "OFF",
            "gate": [(t.isoformat(), str(s), why) for t, s, why in self.gate.history],
            "flat": not st.positions and not st.open_orders,
            "kills": sorted(f"{sw}{'(' + sc + ')' if sc else ''}: {k.reason}" for (sw, sc), k in st.kills.items()),
            "halts": sorted(str(h) for h in st.halts),
            "shadow": {
                "bars": self.shadow.bars,
                "signals": [s.as_json() for s in self.shadow.signals],
                "plugins_stood_down": dict(self.shadow.errors),
                "strategies": list(self.shadow.strategy_ids),
            },
            "host": {"steps": self.service.steps, "failed_steps": self.service.total_failures},
            "backup": self.backups[-1].name,
            "labels": list(LABELS),
        }
        if self.replay and self.cfg.replay.rehearse_orders:
            report["order_rehearsal"] = self._rehearse(self._synthetic_day(self.day or now.date()))
        out = state / "reports" / f"{self.day}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
        self.reports.append(report)
        self.alerts.send(
            "INFO",
            f"End of day {self.day}: flat={report['flat']}, gate {self.gate.state}, "
            f"{len(self.shadow.signals)} shadow signals, journal backed up. Real money OFF.",
        )

    def _rehearse(self, sd: SyntheticDay) -> dict[str, Any]:
        """The order path on the same SYNTHETIC day (every strategy as PAPER, mechanics only)."""
        wd = self.cfg.state_dir / "rehearsal" / str(sd.plan.day)
        try:
            run = PaperLoop(self.kernel, self.specs, nav=self.cfg.replay.rehearsal_nav, workdir=wd).run([sd.plan])
        except Exception as e:
            self.alerts.send("WARNING", f"order rehearsal failed: {type(e).__name__}: {e}")
            return {"error": f"{type(e).__name__}: {e}"}
        (pd,) = run.days
        return {
            "label": "SIMULATED: synthetic chain, fake-broker fills; mechanics only, not a result",
            "nav": f"{self.cfg.replay.rehearsal_nav} (HYPOTHETICAL)",
            "decisions": len(pd.decisions),
            "outcomes": dict(collections.Counter(x.outcome for x in pd.decisions)),
            "approved": sum(1 for x in pd.decisions if x.outcome == "APPROVED"),
            "orders_sent": pd.orders_sent,
            "fills": pd.fills,
            "flat_at_end": pd.flat_at_end,
            "kills": pd.kills,
            "journal_verified": run.journal_verified,
        }

    # -- status
    def publish(self, now: datetime) -> None:
        st = self.runtime.state
        nxt = self.scheduler.next_event(now)
        if self.feed_kind == "upstox" and self.live_feed is not None:
            feed = f"Upstox Feed V3: {self.live_feed.health(now)}"
        elif self.feed_kind == "replay":
            feed = "SIMULATED replay (synthetic day)"
        else:
            feed = "none"
        self.board.publish(
            {
                "service": "project100c host",
                "mode": self.cfg.mode.upper(),
                "real_money": "OFF",
                "phase": self.scheduler.phase(now),
                "next_event": f"{nxt[0].value} at {nxt[1]:%H:%M} IST" if nxt else "none today",
                "gate": str(self.gate.state),
                "feed": feed,
                "kill_latched": bool(st.kills),
                "halted": bool(st.halts),
                "steps": self.service.steps,
                "last_step_at": now.astimezone(IST).isoformat(timespec="seconds"),
                "started_at": self.started_at.astimezone(IST).isoformat(timespec="seconds"),
                "labels": list(LABELS),
            },
            {
                "positions": {k: str(v.qty) for k, v in st.positions.items()},
                "open_orders": len(st.open_orders),
                "paper_realised_today": str(st.realised_today),
                "kills": [f"{sw}{'(' + sc + ')' if sc else ''}: {k.reason}" for (sw, sc), k in st.kills.items()],
                "halts": [f"{h}: {hl.reason}" for h, hl in st.halts.items()],
                "regime_tags": list(self.shadow.regime_tags),
                "shadow_signals_today": [s.as_json() for s in self.shadow.signals[-50:]],
                # structured detail for the Jarvis host stream (observability/dashboard/hostfeed.py)
                "day": str(self.shadow.day) if self.shadow.day else None,
                "nav": str(st.nav),
                "market": self._market_json(),
                "regime": self.shadow.label.as_json() if self.shadow.label is not None else None,
                "strategies": [
                    {"id": s.id, "name": _short(s.hypothesis), "hypothesis": s.id, "stage": str(s.status.value)}
                    for s in self.specs
                    if s.id in self.shadow.strategy_ids
                ],
                "plugins_stood_down": dict(self.shadow.errors),
                "kill_detail": [
                    {"id": str(sw), "scope": sc, "reason": k.reason, "at": k.latched_at.isoformat()}
                    for (sw, sc), k in st.kills.items()
                ],
                "halt_detail": [
                    {"kind": str(h), "reason": hl.reason, "at": hl.latched_at.isoformat()} for h, hl in st.halts.items()
                ],
                "recent_alerts": list(self.log_alerts.recent)[-20:],
                "reconciler": {"polls": self.reconciler.polls, "reports": self.reconciler.reports[-5:]},
                "failed_steps": self.service.total_failures,
            },
        )
        self.board.heartbeat()

    def _market_json(self) -> dict[str, Any] | None:
        """The latest closed index bar the shadow strategies saw, and where it came from."""
        b = self.shadow.last_bar
        if b is None:
            return None
        real = self.feed_kind == "upstox"
        return {
            "bar_start": b.start.astimezone(IST).isoformat(timespec="seconds"),
            "open": str(b.open),
            "high": str(b.high),
            "low": str(b.low),
            "close": str(b.close),
            "vix": None if self.shadow.last_vix is None else str(self.shadow.last_vix),
            "source": "Upstox Feed V3" if real else "SIMULATED replay (synthetic day)",
            "real_data": real,
        }

    # -- the loop
    def step(self) -> bool:
        now = self.clock()
        for ev in self.scheduler.due(now):
            self.on_event(ev, now)
        ok = True
        if self.day_open and self.runtime.state.trading_date == now.astimezone(IST).date():
            ok = self.service.run_once()
        self.publish(now)
        return ok

    def _advance_replay(self) -> bool:
        """Move the simulated clock on; at the end of a day jump to the next one. False when the replay is over."""
        assert self.sim is not None
        today = self.sim.t.astimezone(IST).date()
        eod = datetime.combine(today, self.cfg.schedule.end_of_day, tzinfo=IST)
        if self.sim.t > eod and not self.day_open:
            later = [sd.plan.day for sd in self.days if sd.plan.day > today]
            if not later:
                return False
            self.sim.t = self._day_begin(later[0])
            return True
        self.sim.t += timedelta(seconds=self.cfg.replay.step_s)
        return True

    def run(self, *, max_steps: int | None = None) -> int:
        n = 0
        while not self.stop.is_set() and (max_steps is None or n < max_steps):
            t0 = _time.monotonic()
            self.step()
            n += 1
            if self.service.failures >= self.cfg.max_consecutive_failures:
                self.alerts.send("URGENT", "host stopping after repeated failed steps: the supervisor restarts it")
                return EXIT_FAILED
            if self.sim is not None:
                if not self._advance_replay():
                    log.info("replay finished", extra={"event": "REPLAY_DONE", "mode": "replay"})
                    if not self.cfg.replay.hold_after:
                        return EXIT_STOPPED
                    while not self.stop.wait(1.0):
                        self.board.heartbeat()
                    return EXIT_STOPPED
                if self.cfg.replay.pace_s > 0:
                    self.stop.wait(self.cfg.replay.pace_s)
            else:
                self.stop.wait(max(0.0, self.cfg.interval_s - (_time.monotonic() - t0)))
        return EXIT_STOPPED

    def close(self) -> None:
        for srv in (self.status_srv, self.webhook):
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        if self.live_feed is not None and self.live_feed.feed is not None:
            self.live_feed.feed.close()
        self.journal.close()


def _short(text: str, n: int = 64) -> str:
    """A one-line label from a spec's hypothesis (the spec has no display name)."""
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m project100c.ops.host_main", description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, default=Path("configs/host/host.toml"))
    ap.add_argument("--root", type=Path, default=Path("."), help="directory holding configs/, specs/ and docs/")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--state-dir", type=Path)
    ap.add_argument("--log-format", choices=("json", "text"), default="json")
    ap.add_argument("--hold-after", action="store_true", help="replay: keep serving the status page when done")
    ap.add_argument("--replay-pace", type=float, help="replay: real seconds to sleep per step")
    a = ap.parse_args(argv)
    redactor = Redactor()
    configure_logging(redactor, fmt=a.log_format)
    try:
        cfg = load_host_config(a.config, mode=a.mode, state_dir=a.state_dir)
        if a.hold_after or a.replay_pace is not None:
            pace = cfg.replay.pace_s if a.replay_pace is None else a.replay_pace
            rp = dataclasses.replace(cfg.replay, hold_after=a.hold_after or cfg.replay.hold_after, pace_s=pace)
            cfg = dataclasses.replace(cfg, replay=rp)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        log.error("configuration error: %s", e, extra={"event": "CONFIG_ERROR"})
        return EXIT_CONFIG
    if cfg.mode == "live":
        problems = live_lock_problems(cfg.live_lock, a.root / "docs" / "go-live-checklist.md")
        for p in problems:
            log.error("live refused: %s", p, extra={"event": "LIVE_REFUSED", "mode": "live"})
        if problems:
            return EXIT_LIVE_REFUSED
        return EXIT_LIVE_REFUSED  # unreachable while the build lock holds; a live build needs its own wiring
    try:
        host = Host(cfg, root=a.root, redactor=redactor)
    except Exception as e:
        log.error("startup refused: %s: %s", type(e).__name__, e, extra={"event": "STARTUP_REFUSED"})
        return EXIT_CONFIG
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: host.stop.set())
    log.info(
        "host up: mode=%s feed=%s strategies=%d (SHADOW) real money OFF",
        cfg.mode,
        host.feed_kind,
        len(host.specs),
        extra={"event": "HOST_UP", "mode": cfg.mode},
    )
    try:
        return host.run()
    finally:
        host.close()


if __name__ == "__main__":
    raise SystemExit(main())

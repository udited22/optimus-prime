"""Host stream: Jarvis fed by the paper/shadow host instead of its own simulation.

``python -m project100c.observability.dashboard --source host`` polls the host's private status
(``ops.status_server``: ``/private/status`` on loopback, with the proxy's shared header) and turns each new reading
into the dashboard's existing event kinds, so the frontend needs no change:

* SESSION when the host's trading day changes; STRATEGIES (the shadow strategies and their RESEARCH stage);
* TICK and REGIME from the latest closed index bar and regime label the shadow strategies saw;
* WINDOW from the trading window on the bar's clock; KILLS and POSITION from the paper runtime;
* LOG lines for every new shadow signal ("not traded") and every new host alert.

Honesty: an event is marked real (``simulated=False``) only when it is derived from the Upstox feed. Everything
about orders, fills and positions comes from the SIMULATED paper broker and stays ``simulated=True``, and the
server label contains SIMULATED, so the frontend's mode indicator can never read LIVE (model/mode.ts).

Read-only: this module only GETs the status page. Its kill port refuses: the host's kill switch is the owner's
Telegram ``/kill`` command, not the dashboard."""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from project100c.kernel.kills import KillSwitch
from project100c.observability.dashboard.events import DashboardError, DashEvent, EventBus, EventKind, Flow
from project100c.observability.dashboard.simulator import LOT, Kernel, KillPort
from project100c.observability.dashboard.topology import EDGE_SET
from project100c.sessions import IST

HOST_LABEL = "HOST STREAM (paper/shadow): SIMULATED paper broker, real money OFF"
PROXY_HEADER = "X-P100C-Proxy-Auth"
ONE_MIN = timedelta(minutes=1)


def _flows(*pairs: tuple[str, str, str]) -> tuple[Flow, ...]:
    return tuple(Flow(a, b, k) for a, b, k in pairs if (a, b) in EDGE_SET)


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _ts(v: Any, default: datetime) -> datetime:
    if isinstance(v, str):
        try:
            t = datetime.fromisoformat(v)
        except ValueError:
            return default
        return t if t.tzinfo is not None else t.replace(tzinfo=IST)
    return default


@dataclass
class HostTranslator:
    """Turns successive private-status readings into dashboard events (pure: no I/O, deterministic)."""

    kernel: Kernel
    _day: date | None = None
    _open: Decimal | None = None
    _prev: Decimal | None = None
    _closes: list[Decimal] = field(default_factory=list)
    _bar: str | None = None
    _regime: str | None = None
    _phase: str | None = None
    _kills: tuple[Any, ...] | None = None
    _pos: tuple[Any, ...] | None = None
    _strats: tuple[Any, ...] | None = None
    _signals: set[tuple[str, str]] = field(default_factory=set)
    _alerts: set[tuple[str, ...]] = field(default_factory=set)

    def translate(self, status: Mapping[str, Any], now: datetime | None = None) -> list[DashEvent]:
        pub: Mapping[str, Any] = status.get("public") or {}
        ts = _ts(pub.get("last_step_at"), now or datetime.now(IST))
        market: Mapping[str, Any] = status.get("market") or {}
        real = bool(market.get("real_data"))
        feed = str(pub.get("feed", "unknown"))
        out: list[DashEvent] = []

        def ev(kind: EventKind, data: dict[str, Any], *flows: tuple[str, str, str], sim: bool = True) -> None:
            out.append(DashEvent(ts, kind, data, _flows(*flows), simulated=sim))

        day = date.fromisoformat(status["day"]) if status.get("day") else ts.astimezone(IST).date()
        if day != self._day:
            self._new_day(day)
            lim = self.kernel.limits
            ev(
                EventKind.SESSION,
                {
                    "scenario": f"host-{day:%Y%m%d}",
                    "title": f"{day:%a %d-%b-%Y} · {pub.get('mode', '?')} · host stream",
                    "day": day,
                    "seed": None,
                    "expiry": None,
                    "lot": LOT,
                    "nav": status.get("nav"),
                    "step_s": 60,
                    "window": self.kernel.window_json(),
                    "limits": {
                        "version": lim.version,
                        "per_trade_max_loss_frac": lim.per_trade_max_loss_frac,
                        "daily_stop_frac": lim.daily_stop_frac,
                        "dd_warning_frac": lim.dd_warning_frac,
                        "dd_suspend_frac": lim.dd_suspend_frac,
                        "dd_hard_ceiling_frac": lim.dd_hard_ceiling_frac,
                    },
                    "notice": f"HOST STREAM: feed {feed}; SIMULATED paper broker; strategies in SHADOW only "
                    "(nothing is traded); real money OFF",
                },
            )
        killed = {str(k.get("scope")) for k in status.get("kill_detail") or [] if k.get("scope")}
        rows = [
            {
                "id": s.get("id"),
                "hypothesis": s.get("hypothesis"),
                "name": s.get("name"),
                "stage": s.get("stage"),
                "killed": s.get("id") in killed or s.get("id") in (status.get("plugins_stood_down") or {}),
            }
            for s in status.get("strategies") or []
        ]
        sig = tuple(sorted((str(r["id"]), str(r["stage"]), bool(r["killed"])) for r in rows))
        if rows and sig != self._strats:
            self._strats = sig
            ev(EventKind.STRATEGIES, {"rows": rows, "stages_simulated": False, "shadow_only": True})
        self._market(market, real, feed, out)
        bar_ts = _ts(market.get("bar_start"), ts) + ONE_MIN if market else ts
        phase = str(self.kernel.market_clock.phase(bar_ts if market else ts))
        if phase != self._phase:
            self._phase = phase
            ev(EventKind.WINDOW, {"phase": phase})
        self._kills_event(status, ev)
        self._position_event(status, ev)
        for s in status.get("shadow_signals_today") or []:
            key = (str(s.get("at")), str(s.get("strategy_id")))
            if key in self._signals:
                continue
            self._signals.add(key)
            rights = "/".join(s.get("rights") or []) or "?"
            text = (
                f"SHADOW signal {s.get('strategy_id')} {rights}: {s.get('reason')} "
                f"(regime {', '.join(s.get('regime_tags') or []) or 'n/a'}); recorded, not traded"
            )
            out.append(
                DashEvent(
                    _ts(s.get("at"), ts),
                    EventKind.LOG,
                    {
                        "level": "INFO",
                        "text": text,
                        "activity": {"actor": "strategy_factory", "text": text, "tone": "info"},
                    },
                    _flows(("market_intel", "strategy_factory", "regime")),
                    simulated=not real,
                )
            )
        for a in status.get("recent_alerts") or []:
            akey = tuple(str(x) for x in a)
            if akey in self._alerts or len(akey) < 3:
                continue
            self._alerts.add(akey)
            tone = "bad" if akey[1] == "URGENT" else "warn" if akey[1] == "WARNING" else "muted"
            out.append(
                DashEvent(
                    _ts(akey[0], ts),
                    EventKind.LOG,
                    {
                        "level": akey[1],
                        "text": f"host: {akey[2]}",
                        "activity": {"actor": "cio", "text": akey[2], "tone": tone},
                    },
                )
            )
        return out

    def _new_day(self, day: date) -> None:
        self._day, self._open, self._prev, self._closes, self._bar, self._regime = day, None, None, [], None, None
        self._phase, self._kills, self._pos, self._strats = None, None, None, None
        self._signals, self._alerts = set(), set()

    def _market(self, m: Mapping[str, Any], real: bool, feed: str, out: list[DashEvent]) -> None:
        close = _dec(m.get("close"))
        if close is None or m.get("bar_start") == self._bar:
            return
        self._bar = str(m.get("bar_start"))
        at = _ts(m.get("bar_start"), datetime.now(IST)) + ONE_MIN
        if self._open is None:
            self._open = _dec(m.get("open")) or close
        prev = self._prev if self._prev is not None else close
        self._prev = close
        self._closes.append(close)
        chg = close - self._open
        vix = _dec(m.get("vix"))
        flows = _flows(("broker", "market_intel", "tick"), ("market_intel", "data_quality", "tick"))
        out.append(
            DashEvent(
                at,
                EventKind.TICK,
                {
                    "spot": close,
                    "vix": vix,
                    "change": chg,
                    "change_pct": (chg / self._open * 100).quantize(Decimal("0.01")) if self._open else Decimal(0),
                    "delta": close - prev,
                    "source": m.get("source", feed),
                    "bar": {"open": m.get("open"), "high": m.get("high"), "low": m.get("low"), "close": close},
                },
                flows,
                simulated=not real,
            )
        )

    def regime_event(self, status: Mapping[str, Any], at: datetime) -> DashEvent | None:
        lab: Mapping[str, Any] | None = status.get("regime")
        if not lab or lab.get("ts") == self._regime:
            return None
        self._regime = str(lab.get("ts"))
        back = self._closes[-31] if len(self._closes) > 30 else (self._closes[0] if self._closes else None)
        last = self._closes[-1] if self._closes else None
        ret = ((last - back) / back * 100).quantize(Decimal("0.001")) if back and last is not None else None
        market: Mapping[str, Any] = status.get("market") or {}
        return DashEvent(
            at,
            EventKind.REGIME,
            {
                "tags": list(status.get("regime_tags") or []),
                "ret_30m_pct": ret,
                "vix": _dec(market.get("vix")),
                "classifier": lab.get("classifier"),
                "status": lab.get("status"),
                "inputs": market.get("source", "host"),
                "warmup": lab.get("warmup"),
                "trend": lab.get("trend"),
                "volatility": lab.get("volatility"),
                "gap": lab.get("gap"),
                "opening": lab.get("opening"),
                "classifier_agreement": lab.get("classifier_agreement"),
                "classifier_agreement_note": "share of the classifier's voters agreeing; not a confidence",
            },
            _flows(("market_intel", "cio", "regime")),
            simulated=not bool(market.get("real_data")),
        )

    def _kills_event(self, status: Mapping[str, Any], ev: Callable[..., None]) -> None:
        kd = list(status.get("kill_detail") or [])
        hd = list(status.get("halt_detail") or [])
        sig = tuple(sorted((str(k.get("id")), str(k.get("scope"))) for k in kd)) + tuple(
            sorted(("HALT", str(h.get("kind"))) for h in hd)
        )
        if sig == self._kills:
            return
        self._kills = sig
        switches = []
        for sw in KillSwitch:
            mine = [k for k in kd if k.get("id") == sw.value]
            switches.append(
                {
                    "id": sw.value,
                    "latched": bool(mine),
                    "scope": [k.get("scope") for k in mine],
                    "reason": mine[0].get("reason", "") if mine else "",
                    "at": mine[0].get("at") if mine else None,
                }
            )
        ev(EventKind.KILLS, {"switches": switches, "halts": hd})

    def _position_event(self, status: Mapping[str, Any], ev: Callable[..., None]) -> None:
        pos: Mapping[str, Any] = status.get("positions") or {}
        sig = (tuple(sorted(pos.items())), status.get("open_orders"), status.get("paper_realised_today"))
        if sig == self._pos:
            return
        self._pos = sig
        first = next(iter(sorted(pos)), None)
        ev(
            EventKind.POSITION,
            {
                "open": bool(pos),
                "symbol": first,
                "qty": pos.get(first) if first else None,
                "positions": dict(pos),
                "open_orders": status.get("open_orders"),
                "paper_realised_today": status.get("paper_realised_today"),
                "label": "SIMULATED paper broker",
            },
        )

    def all_events(self, status: Mapping[str, Any], now: datetime | None = None) -> list[DashEvent]:
        """``translate`` plus the regime event (kept separate so the bar closes are folded first)."""
        out = self.translate(status, now)
        at = out[-1].ts if out else (now or datetime.now(IST))
        reg = self.regime_event(status, at)
        return out + ([reg] if reg is not None else [])


def http_fetcher(url: str, secret: str, timeout_s: float = 5.0) -> Callable[[], dict[str, Any]]:
    """GET the host's private status on loopback with the proxy's shared header."""
    if not url.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
        raise DashboardError("the host stream reads the status server on loopback only")

    def fetch() -> dict[str, Any]:
        req = urllib.request.Request(url, headers={PROXY_HEADER: secret, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:  # loopback URL checked above
            body = json.loads(r.read())
        if not isinstance(body, dict):
            raise DashboardError("host status is not a JSON object")
        return body

    return fetch


@dataclass
class HostFeedRunner:
    """Polls the host and publishes the translated events on the bus (same interface as ``LiveRunner``)."""

    bus: EventBus
    kernel: Kernel
    fetch: Callable[[], dict[str, Any]]
    poll_s: float = 2.0
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _tr: HostTranslator = field(init=False)
    _down: bool = field(default=False, init=False)
    polls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._tr = HostTranslator(self.kernel)

    def kill_port(self) -> KillPort:
        def refuse(reason: str, requested_by: str) -> dict[str, Any]:
            raise DashboardError(
                "read-only host stream: the dashboard cannot act on the host; the owner's kill switch is the "
                "Telegram /kill command"
            )

        return KillPort(refuse)

    def poll_once(self) -> int:
        """One poll: publish what is new. Returns the number of events published."""
        self.polls += 1
        try:
            status = self.fetch()
        except Exception as e:  # the host restarting or not up yet: say so once, keep polling
            if not self._down:
                self._down = True
                msg = f"host status unreachable ({type(e).__name__}); retrying every {self.poll_s:g} s"
                self.bus.publish([DashEvent(datetime.now(IST), EventKind.LOG, {"level": "WARNING", "text": msg})])
            return 0
        self._down = False
        evs = self._tr.all_events(status)
        if evs:
            self.bus.publish(evs)
        return len(evs)

    def start_thread(self) -> None:
        if self._thread is not None:
            raise DashboardError("host feed already started")
        self._thread = threading.Thread(target=self._run, name="p100c-host-feed", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.bus.close()

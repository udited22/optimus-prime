"""Tony, the Trading CIO (the orchestrator), explaining itself: NOW / WHY / NEXT, the attention state, and the
answers behind "Ask Tony".

Everything here is a deterministic function of a ``SystemView``: a snapshot of real system state (simulator
strategy watches, Risk Governor verdicts, kernel/journal state, kill switches and halts, economics). There is NO
language model and no free text that is not built from that state. When something is not known, the text says so.
Confidence numbers are never shown: none of the simulated strategies produces a calibrated confidence.

Attention rules (docs/architecture/observability.md §17.5, tested in tests/observability/test_tony.py):

INTERVENTION REQUIRED (red), any of:
  - a kill switch is latched (any of the nine, any scope)
  - a trading halt is latched (weekly freeze, drawdown suspension, Exit-All failed, residual at hard flat)
  - the kernel reported a journal/system-integrity failure
  - an open position has had no confirmed broker-side stop for more than PROTECTIVE_CRITICAL_S seconds
ATTENTION (amber), any of:
  - REJECTION_STREAK consecutive Risk Governor rejections, the latest within REJECTION_WINDOW
  - cost-justification advisory BELOW_THRESHOLD (net return under target for the whole window,
  docs/risk/system-economics.md)
  - data-quality warning: no market tick for FEED_STALE_S seconds
  - the broker link is down (before the connectivity kill latches)
  - a position is open within FLATTEN_WARNING of the forced-flatten time
  - an open position without a confirmed broker-side stop for more than PROTECTIVE_GRACE_S seconds
  - daily loss at or above DAILY_LOSS_ATTENTION of the daily stop
  - drawdown at or above the configured warning level
NORMAL otherwise. Informational notices (for example the *structural* cost advisory, which is expected at the
Rs 10k canary NAV, docs/risk/system-economics.md §19.5) are listed but do not raise the level.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from project100c.economics.fmt import inr
from project100c.kernel.kills import KILL_ACTIONS, RESET_RULES, KillAction, KillSwitch, ResetRule
from project100c.sessions import IST

REJECTION_STREAK = 3
REJECTION_WINDOW = timedelta(minutes=60)
FLATTEN_WARNING = timedelta(minutes=30)
PROTECTIVE_GRACE_S = 30
PROTECTIVE_CRITICAL_S = 60
FEED_STALE_S = 5
DAILY_LOSS_ATTENTION = Decimal("0.5")


class Level(StrEnum):
    NORMAL = "NORMAL"
    ATTENTION = "ATTENTION"
    INTERVENTION = "INTERVENTION"


class ConcernLevel(StrEnum):
    INFO = "INFO"
    ATTENTION = "ATTENTION"
    INTERVENTION = "INTERVENTION"


_RANK = {ConcernLevel.INFO: 0, ConcernLevel.ATTENTION: 1, ConcernLevel.INTERVENTION: 2}


@dataclass(frozen=True, slots=True)
class Concern:
    level: ConcernLevel
    code: str
    text: str


@dataclass(frozen=True, slots=True)
class Attention:
    level: Level
    concerns: tuple[Concern, ...]

    @property
    def headline(self) -> str:
        top = [c for c in self.concerns if c.level is not ConcernLevel.INFO]
        if not top:
            return "All systems normal"
        return top[0].text if len(top) == 1 else f"{top[0].text} (+{len(top) - 1} more)"


# ---------------------------------------------------------------------------------------------- the view
@dataclass(frozen=True, slots=True)
class DecisionView:
    at: datetime
    intent_id: str
    strategy: str
    stage: str
    symbol: str
    verdict: str  # APPROVE | REJECT (kernel Verdict)
    reasons: tuple[str, ...]
    risk_at_stop: Decimal | None
    budget: Decimal | None
    simulate_only: bool
    limit: Decimal

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"


@dataclass(frozen=True, slots=True)
class PositionView:
    key: str
    symbol: str
    strategy: str
    qty: int
    avg: Decimal
    bid: Decimal | None
    stop_trigger: Decimal | None
    stop_limit: Decimal | None
    target: Decimal | None
    protective_confirmed: bool
    opened_at: datetime
    unrealised: Decimal
    thesis: str  # one sentence built by the strategy watch from the entry facts; "" if unknown


@dataclass(frozen=True, slots=True)
class LatchView:
    kind: str  # KILL | HALT
    name: str  # KillSwitch value or HaltKind value
    scope: str
    reason: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ClosedView:
    strategy: str
    symbol: str
    pnl: Decimal  # net of charges
    stopped_out: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class StrategyView:
    id: str
    hypothesis: str
    name: str
    stage: str
    killed: bool


@dataclass(frozen=True, slots=True)
class OrbWatch:
    """The SIMULATED opening-range-breakout rule (S-ORB-001), as the simulator runs it."""

    status: str  # BUILDING_RANGE | ARMED | WORKING | IN_TRADE | STOOD_DOWN | DONE
    or_high: Decimal | None
    or_low: Decimal | None
    buffer: Decimal
    trades: int
    max_trades: int
    last_check: time
    range_end: time


@dataclass(frozen=True, slots=True)
class VwapWatch:
    """The SIMULATED VWAP-pullback rule (S-VWAPC-001, SHADOW: simulate-only)."""

    vwap: Decimal | None
    band: Decimal
    sent: int
    max_sent: int
    start: time
    end: time


@dataclass(frozen=True, slots=True)
class SystemView:
    now: datetime
    session_day: date
    next_session: date | None
    phase: str
    entry_start: time
    entry_cutoff: time
    flatten_start: time
    hard_flat: time
    spot: Decimal | None
    vix: Decimal | None
    ret_30m_pct: Decimal | None
    regime_tags: tuple[str, ...]
    nav: Decimal
    sod_nav: Decimal
    realised: Decimal
    unrealised: Decimal
    daily_loss: Decimal
    daily_stop: Decimal
    dd_frac: Decimal
    dd_warning: Decimal
    per_trade_budget: Decimal
    position: PositionView | None
    latches: tuple[LatchView, ...]
    integrity_failed: bool
    broker_connected: bool
    feed_age_s: Decimal | None  # None = no tick received yet
    decisions: tuple[DecisionView, ...]  # today, oldest first
    closed: tuple[ClosedView, ...]
    strategies: tuple[StrategyView, ...]
    orb: OrbWatch
    vwap: VwapWatch
    economics: Mapping[str, Any] | None = None  # economics.snapshot() + "today", or None before the first one
    session_finished: bool = False
    protective_unconfirmed_s: Decimal | None = None  # seconds the open position has been without a resting stop
    labels: tuple[str, ...] = field(default=("SIMULATED",))
    regime: Mapping[str, Any] | None = None  # the K-11 classifier's latest label (RegimeLabel.as_json), if any


# ---------------------------------------------------------------------------------------------- wording
def hhmm(t: datetime | time) -> str:
    if isinstance(t, datetime):
        t = t.astimezone(IST).time()
    return f"{t:%H:%M}"


def pts(x: Decimal) -> str:
    return f"{x:,.2f}"


def _rs(x: Decimal) -> str:
    return inr(x, 2)


def _signed_rs(x: Decimal) -> str:
    return ("+" if x > 0 else "") + inr(x, 2)


REASON_TEXT: dict[str, str] = {
    "SMALLEST_LOT_EXCEEDS_BUDGET": "one lot would risk {risk} at the stop, more than the {budget} per-trade budget",
    "RISK_BUDGET_EXCEEDED": "the risk at the stop ({risk}) is over the per-trade budget ({budget})",
    "INSUFFICIENT_BUYING_POWER": "not enough buying power for one lot",
    "DAILY_HEADROOM": "the trade could breach the daily loss stop",
    "WEEKLY_HEADROOM": "the trade could breach the weekly loss limit",
    "DRAWDOWN_HEADROOM": "the trade could breach the drawdown limit",
    "OUTSIDE_TRADING_WINDOW": "outside the 09:15-15:00 trading window",
    "ENTRY_WINDOW_CLOSED": "the entry window is closed (entries 09:20-14:00)",
    "KILL_ACTIVE": "a kill switch is latched",
    "STRATEGY_KILLED": "the strategy's kill switch is latched",
    "HALT_ACTIVE": "a trading halt is latched",
    "ALL_TRADING_HALTED": "all trading is halted",
    "MAX_POSITION": "it would exceed the one-position limit",
    "NO_AVERAGING": "averaging into a position is not allowed",
    "NOT_A_STRADDLE": "only a CE+PE long straddle may use a second lot (OD-013)",
    "OPEN_ORDER_LIMIT": "too many open orders",
    "MAX_TRADES_PER_DAY": "the system-wide entries-per-day cap is reached",
    "STRATEGY_MAX_ENTRIES": "the strategy's own entries-per-day cap is reached",
    "COOLDOWN": "the strategy is in its cool-down after a stop-out",
    "MARTINGALE": "it would increase size after a loss",
    "NO_PROTECTIVE_EXIT": "the intent has no protective stop",
    "STOP_INVALID": "the stop is invalid",
    "STOP_WIDENED": "the stop is wider than the spec allows",
    "NO_QUOTE": "no quote for the contract",
    "STALE_QUOTE": "the quote is stale",
    "LOW_LIQUIDITY": "the contract is too illiquid",
    "PRICE_SANITY": "the limit price failed the sanity check",
    "TICK_SIZE": "the price is off the tick grid",
    "MANDATE_LONG_ONLY": "it would open a short option position (long-only mandate, OD-006)",
    "STRATEGY_STATUS": "the strategy's lifecycle stage does not allow live orders",
    "EVENT_DAY_NOT_CERTIFIED": "the strategy is not certified for today's event",
    "ABNORMAL_MARKET": "the market is moving abnormally (NIFTY or VIX far from the open, or a halt; OD-017)",
    "SYSTEM_INTEGRITY_FAILURE": "a system-integrity failure",
}


def explain_reasons(reasons: Sequence[str], risk: Decimal | None, budget: Decimal | None) -> list[str]:
    out = []
    for r in reasons:
        t = REASON_TEXT.get(r)
        if t is None:
            out.append(r.replace("_", " ").lower())
            continue
        if "{risk}" in t or "{budget}" in t:
            if risk is None or budget is None:
                out.append(r.replace("_", " ").lower())
                continue
            t = t.format(risk=_rs(risk), budget=_rs(budget))
        out.append(t)
    return out


_ACTION_TEXT = {
    KillAction.BLOCK_ENTRIES: "new entries blocked",
    KillAction.CANCEL_ALL: "working orders cancelled",
    KillAction.FLATTEN: "positions flattened",
    KillAction.FLATTEN_UNEXPECTED: "positions the journal cannot explain are exited",
    KillAction.VERIFY_PROTECTIVE: "broker-side stops verified",
    KillAction.HALT: "all automated activity halted",
    KillAction.ALERT_OWNER: "owner alerted",
}
_ACTION_ORDER = list(KillAction)
_RESET_TEXT = {
    ResetRule.AUTO_NEXT_DAY_PREFLIGHT: "it clears at the next session's pre-flight",
    ResetRule.AUTO_DQ_HEALTHY: "it clears once data quality has been healthy for the configured period and resynced",
    ResetRule.RECONNECT_AND_RECONCILE: "it clears only after the broker reconnects and positions reconcile",
    ResetRule.OWNER_OR_COOLOFF: "it needs the owner, or the cool-off period",
    ResetRule.VALIDATION_REPORT_AND_OWNER: "it needs a validation report and the owner",
    ResetRule.OWNER_ONLY: "only the owner can reset it",
}
KILL_SHORT = {
    "STRATEGY_KILL": "Strategy",
    "PORTFOLIO_KILL": "Portfolio",
    "DAILY_LOSS_KILL": "Daily loss",
    "DATA_QUALITY_KILL": "Data quality",
    "BROKER_CONNECTIVITY_KILL": "Broker link",
    "ABNORMAL_MARKET_KILL": "Abnormal market",
    "POSITION_RECONCILIATION_KILL": "Reconciliation",
    "SYSTEM_INTEGRITY_KILL": "System integrity",
    "MANUAL_MASTER_KILL": "Manual master",
}


def kill_response(name: str) -> tuple[str, str]:
    """(kernel response, reset rule) for a kill switch, from kernel/kills.py (docs/risk/risk-engine.md §9.4)."""
    sw = KillSwitch(name)
    acts = sorted(KILL_ACTIONS[sw], key=_ACTION_ORDER.index)
    return ", ".join(_ACTION_TEXT[a] for a in acts), _RESET_TEXT[RESET_RULES[sw]]


def latch_label(lt: LatchView) -> str:
    if lt.kind == "KILL":
        base = f"{KILL_SHORT.get(lt.name, lt.name)} kill switch"
        return f"{base} ({lt.scope})" if lt.scope else base
    return f"{lt.name.replace('_', ' ').lower().capitalize()} halt"


# ---------------------------------------------------------------------------------------------- attention
def attention(v: SystemView) -> Attention:
    cs: list[Concern] = []
    crit, A = ConcernLevel.INTERVENTION, ConcernLevel.ATTENTION
    for lt in v.latches:
        cs.append(Concern(crit, f"{lt.kind}:{lt.name}", f"{latch_label(lt)} latched at {hhmm(lt.at)}: {lt.reason}"))
    if v.integrity_failed:
        cs.append(Concern(crit, "INTEGRITY", "Journal or system-integrity failure reported by the kernel"))
    p = v.position
    if p is not None and not p.protective_confirmed and v.protective_unconfirmed_s is not None:
        s = v.protective_unconfirmed_s
        if s > PROTECTIVE_CRITICAL_S:
            cs.append(Concern(crit, "NO_STOP", f"{p.symbol} has had no confirmed broker-side stop for {s:.0f} s"))
        elif s > PROTECTIVE_GRACE_S:
            cs.append(Concern(A, "STOP_PENDING", f"{p.symbol}: broker-side stop not confirmed after {s:.0f} s"))
    streak = _rejection_streak(v)
    if streak:
        last = v.decisions[-1]
        cs.append(
            Concern(
                A,
                "REJECTION_STREAK",
                f"Risk Governor rejected the last {len(streak)} trade intents (latest {last.strategy} at "
                f"{hhmm(last.at)})",
            )
        )
    if not v.broker_connected and not any(lt.name == "BROKER_CONNECTIVITY_KILL" for lt in v.latches):
        cs.append(Concern(A, "BROKER_DOWN", "Broker link is down (fake broker); the connectivity kill may latch"))
    if v.feed_age_s is not None and v.feed_age_s >= FEED_STALE_S and not v.session_finished:
        cs.append(Concern(A, "FEED_STALE", f"Data quality: no market tick for {v.feed_age_s:.0f} s"))
    if p is not None and v.phase not in ("CLOSED",):
        flat_at = datetime.combine(v.session_day, v.flatten_start, IST)
        if v.now >= flat_at - FLATTEN_WARNING:
            cs.append(
                Concern(A, "NEAR_FLATTEN", f"{p.symbol} still open at {hhmm(v.now)}; forced flatten {hhmm(flat_at)}")
            )
    if v.daily_stop > 0 and v.daily_loss >= DAILY_LOSS_ATTENTION * v.daily_stop:
        cs.append(
            Concern(A, "DAILY_LOSS", f"Daily loss {_rs(v.daily_loss)} is {v.daily_loss / v.daily_stop:.0%} of the stop")
        )
    if v.dd_frac >= v.dd_warning:
        cs.append(Concern(A, "DRAWDOWN", f"Drawdown {v.dd_frac:.1%} is at or above the {v.dd_warning:.0%} warning"))
    e = v.economics
    if e is not None:
        if e.get("status") == "BELOW_THRESHOLD":
            cs.append(Concern(A, "COST_ADVISORY", str(e.get("headline", "cost-justification advisory"))))
        elif e.get("raised"):
            cs.append(
                Concern(
                    ConcernLevel.INFO,
                    "COST_STRUCTURAL",
                    "Cost advisory (structural, expected at the canary NAV): " + str(e.get("headline", "")),
                )
            )
    cs.sort(key=lambda c: -_RANK[c.level])
    top = max((_RANK[c.level] for c in cs), default=0)
    level = (Level.NORMAL, Level.ATTENTION, Level.INTERVENTION)[top]
    return Attention(level, tuple(cs))


def _rejection_streak(v: SystemView) -> tuple[DecisionView, ...]:
    tail: list[DecisionView] = []
    for d in reversed(v.decisions):
        if d.approved:
            break
        tail.append(d)
    if len(tail) >= REJECTION_STREAK and v.now - tail[0].at <= REJECTION_WINDOW:
        return tuple(reversed(tail))
    return ()


# ---------------------------------------------------------------------------------------------- the brief
_TREND_READ = {"UP": "Trending up", "DOWN": "Trending down", "RANGE": "Range-bound, no directional pressure"}
_VOL_READ = {"EXPANSION": "volatility expanding", "COMPRESSION": "volatility compressed", "NORMAL": "volatility normal"}
_OPEN_READ = {"OPENING_DRIVE": "the open was a drive", "MEAN_REVERSION": "the open reverted"}
_GAP_READ = {
    "GAP_UP_LARGE": "large gap up",
    "GAP_UP": "gap up",
    "GAP_DOWN": "gap down",
    "GAP_DOWN_LARGE": "large gap down",
}


def _classifier_read(v: SystemView, rg: Mapping[str, Any]) -> dict[str, str]:
    ver, status = rg.get("classifier", "?"), rg.get("status", "UNVALIDATED")
    basis = f"deterministic regime classifier {ver} ({status}; thresholds ASSUMED; SIMULATED inputs)"
    if rg.get("warmup", True):
        return {"text": "Regime classifier warming up: no regime yet, so NO_EDGE", "basis": basis}
    parts = [_TREND_READ.get(str(rg.get("trend")), "Trend unknown"), _VOL_READ.get(str(rg.get("volatility")), "")]
    if str(rg.get("gap")) in _GAP_READ:
        parts.append(_GAP_READ[str(rg.get("gap"))])
    if str(rg.get("opening")) in _OPEN_READ:
        parts.append(_OPEN_READ[str(rg.get("opening"))])
    if rg.get("abnormal"):
        parts.append(f"ABNORMAL ({rg.get('abnormal_reason', '')})")
    text = "; ".join(p for p in parts if p)
    if v.ret_30m_pct is not None:
        text += f" ({v.ret_30m_pct:+.2f}% over 30 min)"
    agree = rg.get("classifier_agreement")
    if agree is not None:
        basis += f"; classifier agreement {agree} is the share of its voters agreeing, not a confidence"
    return {"text": text, "basis": basis}


def market_read(v: SystemView) -> dict[str, str]:
    if v.regime is not None:
        return _classifier_read(v, v.regime)
    basis = "rule-based SIMULATED classifier: 30-min return vs +/-0.25%, VIX vs 14"
    if v.ret_30m_pct is None or v.spot is None:
        return {"text": "No market read yet (waiting for the first regime update)", "basis": basis}
    r = v.ret_30m_pct
    if r <= Decimal("-0.5"):
        d = "Strong bearish pressure"
    elif r < Decimal("-0.25"):
        d = "Moderate bearish pressure"
    elif r >= Decimal("0.5"):
        d = "Strong bullish pressure"
    elif r > Decimal("0.25"):
        d = "Moderate bullish pressure"
    else:
        d = "Range-bound, no directional pressure"
    vol = (
        "volatility expanding"
        if "VOLATILITY_EXPANSION" in v.regime_tags
        else "volatility compressed"
        if "VOLATILITY_COMPRESSION" in v.regime_tags
        else "volatility normal"
    )
    return {"text": f"{d}; {vol} ({r:+.2f}% over 30 min)", "basis": basis}


def regime_phrase(v: SystemView) -> str:
    """' · volatility expansion' etc. from the SIMULATED regime tags; empty when there is no vol tag yet."""
    if "VOLATILITY_EXPANSION" in v.regime_tags:
        return " · volatility expansion"
    if "VOLATILITY_COMPRESSION" in v.regime_tags:
        return " · volatility compression"
    if "VOLATILITY_NORMAL" in v.regime_tags:
        return " · normal volatility"
    return ""


def _orb_levels(v: SystemView) -> tuple[Decimal, Decimal] | None:
    o = v.orb
    if o.or_high is None or o.or_low is None:
        return None
    return o.or_high + o.buffer, o.or_low - o.buffer


def _next_check(v: SystemView) -> time | None:
    t = v.now.astimezone(IST)
    m = (t.minute // 5 + 1) * 5
    nxt = t.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=m)
    return nxt.time() if nxt.time() <= v.orb.last_check else None


def brief(v: SystemView, att: Attention | None = None) -> dict[str, Any]:
    """NOW / WHY / NEXT. Only static facts (no per-tick numbers) so the event is emitted on real changes."""
    att = att or attention(v)
    facts: list[dict[str, str]] = [{"label": "Risk state", "value": att.level.value}]
    levels: dict[str, Any] = {}
    budget = f"one lot, risk at the stop ≤ {_rs(v.per_trade_budget)} (2% of NAV)"
    no_conf = "not produced (rule-based signals; no calibrated model yet)"
    p = v.position
    kills = [lt for lt in v.latches if lt.kind == "KILL"]
    halts = [lt for lt in v.latches if lt.kind == "HALT"]
    o = v.orb
    lv = _orb_levels(v)
    if kills or halts or v.integrity_failed:
        first = (kills or halts)[0] if (kills or halts) else None
        if first is not None and first.kind == "KILL":
            resp, reset = kill_response(first.name)
            now = f"Trading halted: {latch_label(first)} latched at {hhmm(first.at)}"
            why = f"{first.reason}. Kernel response (docs/risk/risk-engine.md): {resp}."
            nxt = f"Waiting for the owner. Reset rule: {reset}."
        elif first is not None:
            now = f"Trading halted: {latch_label(first)} at {hhmm(first.at)}"
            why = f"{first.reason}."
            nxt = "Waiting for the owner: halts reset only by the owner (with an acknowledgement)."
        else:
            now = "Trading halted: system-integrity failure"
            why = "The kernel could not write or verify its journal, so it refuses further activity."
            nxt = "Waiting for the owner."
        facts.append({"label": "Position", "value": f"{p.symbol} open (exit handled by the kernel)" if p else "Flat"})
        if len(kills) + len(halts) > 1:
            facts.append({"label": "Latched", "value": ", ".join(latch_label(x) for x in [*kills, *halts])})
        status = "Halted"
        cog = ("HALTED", latch_label(first) if first is not None else "system-integrity failure")
    elif p is not None:
        now = f"Managing {p.symbol} long ({p.strategy})"
        why = p.thesis or f"{p.strategy} holds {p.qty} {p.symbol}; the entry reason was not recorded."
        parts = []
        if p.target is not None:
            parts.append(f"target {pts(p.target)}")
        if p.stop_trigger is not None and p.stop_limit is not None:
            parts.append(f"stop {pts(p.stop_trigger)} / {pts(p.stop_limit)} resting at the broker")
        tail = "time exit at 14:30" if p.strategy == "S-ORB-001" else "the strategy's exit rules"
        nxt = (
            f"Exit on {', '.join(parts) or 'the strategy exit'}, or {tail}; forced flatten from "
            f"{hhmm(v.flatten_start)}, hard flat {hhmm(v.hard_flat)}."
        )
        at_risk = (
            f"{_rs((p.avg - p.stop_limit) * p.qty)} to the stop limit "
            f"({(p.avg - p.stop_limit) * p.qty / v.per_trade_budget:.0%} of the per-trade budget)"
            if p.stop_limit is not None and v.per_trade_budget > 0
            else "unknown (stop level not recorded)"
        )
        facts += [
            {"label": "Capital at risk", "value": at_risk},
            {"label": "Broker stop", "value": "resting" if p.protective_confirmed else "not confirmed yet"},
            {"label": "Confidence", "value": no_conf},
        ]
        levels = {
            "target": p.target,
            "stop": p.stop_trigger,
            "entry": p.avg,
        }
        status = f"Managing {p.strategy}"
        cog = ("MANAGING", f"{p.strategy} / {p.symbol}")
    elif v.session_finished or v.phase == "CLOSED":
        now = "Session closed: flat"
        why = f"Hard flat at {hhmm(v.hard_flat)} (OD-002). {len(v.closed)} trade(s) closed today."
        nxt = (
            f"Next session {v.next_session:%a %d %b} from 09:15 (entries from {hhmm(v.entry_start)})."
            if v.next_session
            else "Next session date unknown."
        )
        status = "Session closed"
        cog = ("RESTING", "Session closed / flat")
    elif v.phase in ("BEFORE_WINDOW", "OPENING_NO_ENTRY"):
        now = "Observing the open: no entries before " + hhmm(v.entry_start)
        why = (
            f"The entry window is {hhmm(v.entry_start)}-{hhmm(v.entry_cutoff)} (OD-009). S-ORB-001 is building its "
            f"opening range until {hhmm(o.range_end)}."
        )
        nxt = f"Opening range set at {hhmm(o.range_end)}; then S-ORB-001 watches for a breakout every 5 minutes."
        status = "Observing the open"
        cog = ("OBSERVING", "The open / NIFTY")
    elif v.phase in ("EXIT_ONLY", "FLATTENING"):
        now = (
            f"Exit-only: no new entries after {hhmm(v.entry_cutoff)} (OD-008)"
            if v.phase == "EXIT_ONLY"
            else f"Forced-flatten window ({hhmm(v.flatten_start)}-{hhmm(v.hard_flat)}): flat"
        )
        why = f"Flat, with {len(v.closed)} trade(s) closed today. Nothing new can open until the next session."
        nxt = f"Hard flat at {hhmm(v.hard_flat)}; session ends."
        status = "Exit-only"
        cog = ("WAITING", "Exit-only / no new entries")
    elif o.status == "BUILDING_RANGE":
        now = f"Building the opening range (09:15-{hhmm(o.range_end)})"
        why = (
            f"S-ORB-001 needs the high and low of 09:15-{hhmm(o.range_end)} before it can define a breakout; "
            "no other strategy here may open a position."
        )
        nxt = f"At {hhmm(o.range_end)} the range is fixed and the breakout triggers are published."
        status = "Building the range"
        cog = ("OBSERVING", "Opening range / NIFTY")
    elif o.status == "STOOD_DOWN":
        last = next((d for d in reversed(v.decisions) if d.strategy == "S-ORB-001" and not d.approved), None)
        now = "Not trading: S-ORB-001 stood down for the day"
        if last is not None:
            why = (
                f"The Risk Governor rejected its intent at {hhmm(last.at)}: "
                + "; ".join(explain_reasons(last.reasons, last.risk_at_stop, last.budget))
                + ". A rejection is final for the day (docs/risk/risk-engine.md): no retry with changed parameters."
            )
        else:
            why = "Its last intent was rejected; the rejection record is not available."
        nxt = "No further canary entries today. The SHADOW strategy keeps proposing simulate-only intents."
        status = "Standing down"
        cog = ("WAITING", "No valid opportunity / S-ORB-001 stood down")
    elif o.status == "DONE" or (o.status == "ARMED" and _next_check(v) is None):
        now = f"No new entries: S-ORB-001 is done for the day ({o.trades} of {o.max_trades} trades)"
        why = f"Its last breakout check is {hhmm(o.last_check)}; it trades at most {o.max_trades} times a day."
        nxt = f"Session continues to hard flat at {hhmm(v.hard_flat)}; nothing further is planned."
        status = "Done for the day"
        cog = ("WAITING", "No valid opportunity / S-ORB-001 done for the day")
    elif o.status == "WORKING":
        now = "Entry order working: S-ORB-001"
        why = "The Risk Governor approved the intent; the limit order is at the (fake) broker, waiting for a fill."
        nxt = "On fill the kernel places the protective stop at the broker before anything else."
        status = "Entry working"
        cog = ("EXECUTING", "S-ORB-001 entry order")
    elif lv is not None:
        up, dn = lv
        assert o.or_high is not None and o.or_low is not None
        now = "Watching for an opening-range breakout (S-ORB-001)"
        why = (
            f"NIFTY is inside the opening range {pts(o.or_low)}-{pts(o.or_high)} plus an {o.buffer:.0f}-point buffer, "
            "so the breakout condition is not met. The rule acts only on a 5-minute check beyond the buffer."
        )
        chk = _next_check(v)
        nxt = (
            f"Buy a CE if NIFTY is above {pts(up)}, or a PE if below {pts(dn)}, at the next check "
            f"({hhmm(chk) if chk else 'none left'}); then the Risk Governor decides."
        )
        facts += [
            {"label": "Capital considered", "value": budget},
            {"label": "Confidence", "value": no_conf},
        ]
        levels = {"trigger_up": up, "trigger_down": dn}
        status = "Watching S-ORB-001"
        cog = ("WATCHING", f"Opening-range breakout{regime_phrase(v)} / NIFTY")
    else:  # pragma: no cover - ARMED always has levels; kept so an unknown state says so
        now = "State not recognised"
        why = f"The S-ORB-001 watch reports {o.status!r}, which this explanation does not cover."
        nxt = "Unknown."
        status = "Unknown"
        cog = ("UNKNOWN", "state not recognised")
    return {
        "now": now,
        "why": why,
        "next": nxt,
        "facts": facts,
        "levels": levels,
        "ai_status": status,
        # Tony's current thought for the neural hero: a verb and a subject, both from the state above
        "cognition": {"verb": cog[0], "subject": cog[1]},
        "market": market_read(v),
    }


# ---------------------------------------------------------------------------------------------- strategies
def _stage_allocation(stage: str, budget: Decimal) -> str:
    if stage in ("CANARY", "PRODUCTION"):
        return f"1 lot max · risk ≤ {_rs(budget)}"
    if stage in ("SHADOW", "PAPER"):
        return "₹0 · simulate-only"
    return "₹0 · not trading"


def strategy_rows(v: SystemView) -> list[dict[str, Any]]:
    out = []
    for s in v.strategies:
        ds = [d for d in v.decisions if d.strategy == s.id]
        closed = [c for c in v.closed if c.strategy == s.id]
        last = ds[-1] if ds else None
        if s.killed:
            status = "Kill switch latched"
        elif s.id == "S-ORB-001":
            status = {
                "BUILDING_RANGE": "Building the opening range",
                "ARMED": "Watching for a breakout",
                "WORKING": "Entry order working",
                "IN_TRADE": "In a trade",
                "STOOD_DOWN": "Stood down (rejected)",
                "DONE": "Done for the day",
            }.get(v.orb.status, v.orb.status)
        elif s.id == "S-VWAPC-001":
            w = v.vwap
            status = (
                f"Proposes at 15-min marks {hhmm(w.start)}-{hhmm(w.end)} near VWAP ({w.sent}/{w.max_sent} sent)"
                if w.sent < w.max_sent
                else f"Done ({w.sent}/{w.max_sent} intents)"
            )
        else:
            status = "No simulated activity (hypothesis only)"
        dec = None
        if last is not None:
            text = (
                "Approved" + (" (simulate-only)" if last.simulate_only else "")
                if last.approved
                else "Rejected: " + "; ".join(explain_reasons(last.reasons, last.risk_at_stop, last.budget))
            )
            dec = {"verdict": last.verdict, "at": last.at, "text": text}
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "hypothesis": s.hypothesis,
                "stage": s.stage,
                "killed": s.killed,
                "status": status,
                "allocation": _stage_allocation(s.stage, v.per_trade_budget),
                "trades": len(closed),
                "net": sum((c.pnl for c in closed), Decimal(0)).quantize(Decimal("0.01")) if closed else None,
                "intents": len(ds),
                "last_decision": dec,
            }
        )
    return out


# ---------------------------------------------------------------------------------------------- answers
QUESTIONS: tuple[tuple[str, str], ...] = (
    ("why_not_trading", "Why are we not trading?"),
    ("explain_pnl", "Explain today's P&L"),
    ("largest_risk", "What is our largest risk?"),
    ("last_rejection", "Why was the last trade rejected?"),
    ("researching", "What strategies are currently researching?"),
    ("next_trigger", "What would trigger the next trade?"),
)


def answers(v: SystemView, b: Mapping[str, Any], att: Attention) -> dict[str, dict[str, Any]]:
    q = dict(QUESTIONS)
    out: dict[str, dict[str, Any]] = {}

    def put(key: str, lines: list[str], basis: list[str]) -> None:
        out[key] = {"question": q[key], "lines": lines, "basis": basis}

    p = v.position
    # why not trading
    if p is not None:
        lines = [f"We are trading: {b['now']}.", str(b["why"])]
    else:
        lines = [str(b["now"]) + ".", str(b["why"])]
        if v.decisions and not v.decisions[-1].approved:
            d = v.decisions[-1]
            lines.append(
                f"Last Risk Governor verdict ({hhmm(d.at)}, {d.strategy}): rejected because "
                + "; ".join(explain_reasons(d.reasons, d.risk_at_stop, d.budget))
                + "."
            )
    put("why_not_trading", lines, ["strategy watches", "Risk Governor verdicts", "trading window", "kill switches"])

    # P&L
    lines = [
        f"NAV {_rs(v.nav)} against {_rs(v.sod_nav)} at the start of day: {_signed_rs(v.nav - v.sod_nav)}.",
        f"Realised {_signed_rs(v.realised)} (net of charges); unrealised {_signed_rs(v.unrealised)} (mark to bid).",
    ]
    for c in v.closed:
        lines.append(
            f"{hhmm(c.at)} {c.strategy} {c.symbol}: {_signed_rs(c.pnl)}" + (" (stopped out)" if c.stopped_out else "")
        )
    if not v.closed:
        lines.append("No trade has closed today.")
    e = v.economics
    if e is not None and isinstance(e.get("today"), Mapping):
        t = e["today"]
        lines.append(
            f"Net of everything today: gross {_signed_rs(Decimal(t['gross']))}, less charges "
            f"{_rs(Decimal(t['charges']))}, the fixed-cost day share {_rs(Decimal(t['fixed_share']))} and tax "
            f"{_rs(Decimal(t['tax']))} (ASSUMED) = "
            f"{_signed_rs(Decimal(t['net']))} (docs/risk/system-economics.md, completed round trips only)."
        )
    put("explain_pnl", lines, ["kernel state (journal)", "closed trades", "economics"])

    # largest risk
    if p is not None and p.stop_limit is not None:
        risk = (p.avg - p.stop_limit) * p.qty
        lines = [
            f"The open {p.symbol} position: {_rs(risk)} to the stop limit ({risk / v.per_trade_budget:.0%} of the "
            f"{_rs(v.per_trade_budget)} per-trade budget). The stop rests at the broker"
            + (" (confirmed)." if p.protective_confirmed else " (NOT confirmed yet).")
        ]
    elif p is not None:
        lines = [f"The open {p.symbol} position; its stop level is not known to the dashboard."]
    else:
        lines = ["No capital is at risk: we are flat."]
    lines.append(
        f"Daily loss used {_rs(v.daily_loss)} of the {_rs(v.daily_stop)} stop; drawdown {v.dd_frac:.2%} "
        f"(warning {v.dd_warning:.0%})."
    )
    if att.level is not Level.NORMAL:
        lines.append("Open concerns: " + "; ".join(c.text for c in att.concerns if c.level is not ConcernLevel.INFO))
    put("largest_risk", lines, ["kernel state", "risk limits", "attention rules"])

    # last rejection
    rej = next((d for d in reversed(v.decisions) if not d.approved), None)
    if rej is None:
        lines = ["No intent has been rejected today."]
    else:
        lines = [
            f"{hhmm(rej.at)}: {rej.strategy} ({rej.stage}) proposed BUY {rej.symbol} at {pts(rej.limit)}.",
            "The Risk Governor rejected it: "
            + "; ".join(explain_reasons(rej.reasons, rej.risk_at_stop, rej.budget))
            + ".",
            "Reason codes: " + ", ".join(rej.reasons) + ".",
        ]
    put("last_rejection", lines, ["Risk Governor verdicts (journal)"])

    # researching
    research = [
        f"{s.id} {s.name} ({s.stage})" for s in v.strategies if s.stage in ("RESEARCH", "BACKTESTED", "VALIDATED")
    ]
    paper = [f"{s.id} {s.name} ({s.stage})" for s in v.strategies if s.stage in ("PAPER", "SHADOW")]
    lines = [
        "Researching: " + (", ".join(research) if research else "none") + ".",
        "Paper / shadow, simulate-only (no capital): " + (", ".join(paper) if paper else "none") + ".",
    ]
    lines.append(
        "Stages are SIMULATED on this dashboard; every real StrategySpec is still RESEARCH "
        "(docs/research/strategy-hypotheses.md)."
    )
    put("researching", lines, ["strategy roster (SIMULATED stages)"])

    # next trigger
    lines = [str(b["next"])]
    w = v.vwap
    if w.sent < w.max_sent and v.phase == "ENTRY_ALLOWED" and not v.latches:
        lines.append(
            f"S-VWAPC-001 (SHADOW, simulate-only) proposes at 15-minute marks {hhmm(w.start)}-{hhmm(w.end)} when NIFTY "
            f"is within {w.band:.0f} points of VWAP" + (f" (VWAP {pts(w.vwap)})." if w.vwap is not None else ".")
        )
    put("next_trigger", lines, ["strategy watches", "trading window"])
    return out


def payload(v: SystemView) -> dict[str, Any]:
    att = attention(v)
    b = brief(v, att)
    return {
        "attention": {
            "level": att.level.value,
            "headline": att.headline,
            "concerns": [{"level": c.level.value, "code": c.code, "text": c.text} for c in att.concerns],
        },
        **b,
        "strategies": strategy_rows(v),
        "answers": answers(v, b, att),
        "questions": [{"id": k, "text": t} for k, t in QUESTIONS],
        "signals": {
            "orb_status": v.orb.status,
            "or_high": v.orb.or_high,
            "or_low": v.orb.or_low,
            "or_buffer": v.orb.buffer,
            "range_end": hhmm(v.orb.range_end),
            "vwap": v.vwap.vwap,
            "vwap_band": v.vwap.band,
        },
        "system": {
            "broker_connected": v.broker_connected,
            "feed_age_s": v.feed_age_s,
            "phase": v.phase,
            "integrity_failed": v.integrity_failed,
        },
        "as_of": v.now,
        "labels": list(v.labels),
        "generated_by": "deterministic templates over system state (no language model)",
    }


def material(p: Mapping[str, Any]) -> tuple[Any, ...]:
    """The part of a payload whose change must be shown at once (anything else may wait for the refresh)."""
    a = p["attention"]
    return (
        a["level"],
        tuple(c["code"] for c in a["concerns"]),
        p["now"],
        p["why"],
        p["next"],
        p["ai_status"],
        (p["cognition"]["verb"], p["cognition"]["subject"]),
        tuple((f["label"], f["value"]) for f in p["facts"]),
        p["market"]["text"],
        p["system"]["broker_connected"],
        tuple((s["id"], s["status"], s["killed"], s["trades"], str(s["last_decision"])) for s in p["strategies"]),
    )

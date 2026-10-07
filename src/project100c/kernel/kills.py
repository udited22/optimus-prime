"""The nine kill switches (docs/risk/risk-engine.md §9.4), trading halts, and their reset rules. Pure, no I/O.

Kills and halts are LATCHED: they live in the journal-derived KernelState, so a process restart never clears
them. Only an explicit reset event that satisfies the switch's reset rule clears a latch; `check_reset` raises
GovernorError otherwise (never a silent no-op).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from project100c.errors import GovernorError


class KillSwitch(StrEnum):
    STRATEGY = "STRATEGY_KILL"
    PORTFOLIO = "PORTFOLIO_KILL"
    DAILY_LOSS = "DAILY_LOSS_KILL"
    DATA_QUALITY = "DATA_QUALITY_KILL"
    BROKER_CONNECTIVITY = "BROKER_CONNECTIVITY_KILL"
    ABNORMAL_MARKET = "ABNORMAL_MARKET_KILL"
    POSITION_RECONCILIATION = "POSITION_RECONCILIATION_KILL"
    SYSTEM_INTEGRITY = "SYSTEM_INTEGRITY_KILL"
    MANUAL_MASTER = "MANUAL_MASTER_KILL"


class HaltKind(StrEnum):
    WEEKLY_FREEZE = "WEEKLY_FREEZE"  # 8% weekly loss: frozen until owner review + diagnostic report
    DD_SUSPENSION = "DD_SUSPENSION"  # drawdown from HWM >= dd_suspend (12.5%, OD-010)
    EXIT_ALL_FAILED = "EXIT_ALL_FAILED"  # OD-007: Exit-All failed/partial -> halt ALL trading, keep alerting
    RESIDUAL_AT_HARD_FLAT = "RESIDUAL_AT_HARD_FLAT"  # fallback policy HALT_AND_ALERT only


class KillAction(StrEnum):
    BLOCK_ENTRIES = "BLOCK_ENTRIES"
    CANCEL_ALL = "CANCEL_ALL"
    FLATTEN = "FLATTEN"
    FLATTEN_UNEXPECTED = "FLATTEN_UNEXPECTED"  # broker is truth; exit positions the journal does not explain
    VERIFY_PROTECTIVE = "VERIFY_PROTECTIVE"  # confirm the protective SL still rests at the broker
    HALT = "HALT"  # after cancel/flatten, no further automated activity until reset
    ALERT_OWNER = "ALERT_OWNER"


class ResetRule(StrEnum):
    AUTO_NEXT_DAY_PREFLIGHT = "AUTO_NEXT_DAY_PREFLIGHT"
    AUTO_DQ_HEALTHY = "AUTO_DQ_HEALTHY"  # healthy for dq_healthy_reset_s + resync
    RECONNECT_AND_RECONCILE = "RECONNECT_AND_RECONCILE"
    OWNER_OR_COOLOFF = "OWNER_OR_COOLOFF"
    VALIDATION_REPORT_AND_OWNER = "VALIDATION_REPORT_AND_OWNER"
    OWNER_ONLY = "OWNER_ONLY"


_A = KillAction
KILL_ACTIONS: dict[KillSwitch, frozenset[KillAction]] = {
    KillSwitch.STRATEGY: frozenset({_A.BLOCK_ENTRIES, _A.CANCEL_ALL, _A.FLATTEN, _A.ALERT_OWNER}),  # scoped
    KillSwitch.PORTFOLIO: frozenset({_A.BLOCK_ENTRIES, _A.CANCEL_ALL, _A.FLATTEN, _A.ALERT_OWNER}),
    KillSwitch.DAILY_LOSS: frozenset({_A.BLOCK_ENTRIES, _A.FLATTEN, _A.ALERT_OWNER}),
    KillSwitch.DATA_QUALITY: frozenset({_A.BLOCK_ENTRIES, _A.VERIFY_PROTECTIVE, _A.ALERT_OWNER}),
    KillSwitch.BROKER_CONNECTIVITY: frozenset({_A.BLOCK_ENTRIES, _A.VERIFY_PROTECTIVE, _A.ALERT_OWNER}),
    KillSwitch.ABNORMAL_MARKET: frozenset({_A.BLOCK_ENTRIES, _A.VERIFY_PROTECTIVE, _A.ALERT_OWNER}),
    KillSwitch.POSITION_RECONCILIATION: frozenset({_A.BLOCK_ENTRIES, _A.FLATTEN_UNEXPECTED, _A.ALERT_OWNER}),
    KillSwitch.SYSTEM_INTEGRITY: frozenset({_A.BLOCK_ENTRIES, _A.FLATTEN, _A.HALT, _A.ALERT_OWNER}),
    KillSwitch.MANUAL_MASTER: frozenset({_A.BLOCK_ENTRIES, _A.CANCEL_ALL, _A.FLATTEN, _A.HALT, _A.ALERT_OWNER}),
}

RESET_RULES: dict[KillSwitch, ResetRule] = {
    KillSwitch.STRATEGY: ResetRule.VALIDATION_REPORT_AND_OWNER,
    KillSwitch.PORTFOLIO: ResetRule.OWNER_ONLY,
    KillSwitch.DAILY_LOSS: ResetRule.AUTO_NEXT_DAY_PREFLIGHT,
    KillSwitch.DATA_QUALITY: ResetRule.AUTO_DQ_HEALTHY,
    KillSwitch.BROKER_CONNECTIVITY: ResetRule.RECONNECT_AND_RECONCILE,
    KillSwitch.ABNORMAL_MARKET: ResetRule.OWNER_OR_COOLOFF,
    KillSwitch.POSITION_RECONCILIATION: ResetRule.OWNER_ONLY,  # owner review of the reconciliation report
    KillSwitch.SYSTEM_INTEGRITY: ResetRule.OWNER_ONLY,
    KillSwitch.MANUAL_MASTER: ResetRule.OWNER_ONLY,
}
if not set(KILL_ACTIONS) == set(KillSwitch) == set(RESET_RULES):  # pragma: no cover - import-time guard
    raise GovernorError("kill tables are not exhaustive over KillSwitch")


@dataclass(frozen=True, slots=True)
class KillLatch:
    switch: KillSwitch
    scope: str  # "" = global; strategy_id for STRATEGY_KILL
    reason: str
    latched_at: datetime
    trading_date: date


@dataclass(frozen=True, slots=True)
class HaltLatch:
    kind: HaltKind
    reason: str
    latched_at: datetime


class ResetActor(StrEnum):
    OWNER = "OWNER"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class ResetEvidence:
    """Facts offered to justify a reset. Absent facts are None; check_reset decides."""

    actor: ResetActor
    now: datetime
    preflight_trading_date: date | None = None  # DAILY_LOSS: a pre-flight for a LATER session completed
    dq_healthy_since: datetime | None = None
    dq_resynced: bool = False
    broker_connected: bool = False
    full_reconciliation_ok_at: datetime | None = None
    validation_report_id: str | None = None
    owner_ack_id: str | None = None  # id of the owner's signed/recorded acknowledgement


def check_reset(latch: KillLatch, ev: ResetEvidence, *, dq_healthy_reset_s: Decimal, abnormal_cooloff_min: int) -> None:
    """Raise GovernorError unless `ev` satisfies the latch's reset rule (docs/risk/risk-engine.md §9.4)."""
    rule = RESET_RULES[latch.switch]
    owner_ok = ev.actor is ResetActor.OWNER and bool(ev.owner_ack_id)
    if rule is ResetRule.OWNER_ONLY:
        if not owner_ok:
            raise GovernorError(f"{latch.switch} resets only by the owner (with an acknowledgement id)")
    elif rule is ResetRule.VALIDATION_REPORT_AND_OWNER:
        if not (owner_ok and ev.validation_report_id):
            raise GovernorError(f"{latch.switch} needs a validation-agent report AND owner OK")
    elif rule is ResetRule.AUTO_NEXT_DAY_PREFLIGHT:
        if ev.preflight_trading_date is None or ev.preflight_trading_date <= latch.trading_date:
            raise GovernorError(f"{latch.switch} resets only at a later session's pre-flight")
    elif rule is ResetRule.AUTO_DQ_HEALTHY:
        need = timedelta(seconds=float(dq_healthy_reset_s))
        if ev.dq_healthy_since is None or not ev.dq_resynced or ev.now - ev.dq_healthy_since < need:
            raise GovernorError(f"{latch.switch} needs DQ healthy >= {dq_healthy_reset_s}s after latch + resync")
        if ev.dq_healthy_since < latch.latched_at:
            raise GovernorError(f"{latch.switch}: healthy period must start after the latch")
    elif rule is ResetRule.RECONNECT_AND_RECONCILE:
        if not ev.broker_connected or ev.full_reconciliation_ok_at is None:
            raise GovernorError(f"{latch.switch} needs reconnect + a full clean reconciliation")
        if ev.full_reconciliation_ok_at < latch.latched_at:
            raise GovernorError(f"{latch.switch}: reconciliation predates the latch")
    elif rule is ResetRule.OWNER_OR_COOLOFF:
        cooled = ev.now - latch.latched_at >= timedelta(minutes=abnormal_cooloff_min)
        if not (owner_ok or cooled):
            raise GovernorError(f"{latch.switch} needs the owner or a {abnormal_cooloff_min} min cool-off")
    else:  # pragma: no cover - exhaustive over ResetRule
        raise GovernorError(f"unhandled reset rule {rule}")


def check_halt_reset(halt: HaltLatch, ev: ResetEvidence) -> None:
    """Every halt is owner-only (weekly freeze: owner review + diagnostic report; DD suspension; OD-007 halt)."""
    if not (ev.actor is ResetActor.OWNER and ev.owner_ack_id):
        raise GovernorError(f"{halt.kind} resets only by the owner (with an acknowledgement id)")

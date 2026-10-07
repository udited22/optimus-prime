"""Global kill-switch triggers from health observations (docs/risk/risk-engine.md §9.4). Pure; thresholds from
RiskLimits.

STRATEGY_KILL and DAILY_LOSS_KILL are derived from journal state in RiskGovernor.required_actions; this module
covers the observation-driven kills. A default HealthObservation is fully healthy. Unknown/unmeasurable inputs
must be passed as unhealthy by the caller (e.g. an unreadable disk -> disk_free_frac=0), never omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from project100c.kernel.kills import KillSwitch
from project100c.kernel.limits import RiskLimits


@dataclass(frozen=True, slots=True)
class HealthObservation:
    # DATA_QUALITY
    atm_quote_age_s: Decimal = Decimal(0)
    feed_disagreement: bool = False
    timestamp_drift_s_sustained: Decimal = Decimal(0)
    instrument_master_consistent: bool = True
    # BROKER_CONNECTIVITY
    broker_errors_in_window: int = 0
    broker_consecutive_errors: int = 0  # order/API errors in a row, no success in between (OD-017)
    ws_down_s: Decimal = Decimal(0)
    session_valid: bool = True
    unknown_order_age_s: Decimal = Decimal(0)
    # ABNORMAL_MARKET
    index_move_frac_in_window: Decimal = Decimal(0)  # |move| inside abnormal_index_move_window_min
    index_move_from_open_frac: Decimal = Decimal(0)  # |index - session open| / open (OD-017); see market_moves
    vix_jump_frac: Decimal = Decimal(0)  # India VIX rise since the session open, a fraction (OD-017)
    market_halt_or_circuit: bool = False
    cas_anomaly: bool = False
    # POSITION_RECONCILIATION
    reconciliation_mismatch: str | None = None
    # SYSTEM_INTEGRITY
    config_hash_ok: bool = True
    clock_drift_ms: int = 0
    disk_free_frac: Decimal = Decimal(1)
    journal_write_failed: bool = False
    crash_loop: bool = False
    memory_pressure: bool = False
    # PORTFOLIO
    exposure_limit_breached: bool = False
    unexplained_pnl_jump: Decimal = Decimal(0)  # absolute rupees
    nav: Decimal = Decimal(0)
    # MANUAL_MASTER
    manual_master_command: str | None = None


def market_moves(
    index_open: Decimal | None, index_now: Decimal | None, vix_open: Decimal | None, vix_now: Decimal | None
) -> tuple[Decimal, Decimal]:
    """(|index - open| / open, volatility-index rise since the open) for the OD-017 abnormal-market rules. A
    missing or non-positive open or last value cannot be measured and returns 0 for that leg; the data-quality kill
    (stale quotes) is what covers a dead feed, so this never invents a move."""
    move = Decimal(0)
    if index_open is not None and index_now is not None and index_open > 0 and index_now > 0:
        move = abs(index_now - index_open) / index_open
    jump = Decimal(0)
    if vix_open is not None and vix_now is not None and vix_open > 0 and vix_now > 0:
        jump = max(Decimal(0), (vix_now - vix_open) / vix_open)
    return move, jump


def detect_kills(obs: HealthObservation, limits: RiskLimits) -> list[tuple[KillSwitch, str]]:
    L = limits
    out: list[tuple[KillSwitch, str]] = []
    dq: list[str] = []
    if obs.atm_quote_age_s > L.dq_stale_s:
        dq.append(f"ATM quotes stale {obs.atm_quote_age_s}s > {L.dq_stale_s}s")
    if obs.feed_disagreement:
        dq.append("feed disagreement")
    if obs.timestamp_drift_s_sustained > L.dq_stale_s:
        dq.append(f"timestamp drift {obs.timestamp_drift_s_sustained}s sustained")
    if not obs.instrument_master_consistent:
        dq.append("instrument master inconsistent")
    if dq:
        out.append((KillSwitch.DATA_QUALITY, "; ".join(dq)))
    br: list[str] = []
    if obs.broker_errors_in_window >= L.broker_error_threshold:
        br.append(f"{obs.broker_errors_in_window} order-API errors/timeouts in {L.broker_error_window_s}s")
    cons = L.broker_consecutive_error_threshold
    if cons is not None and obs.broker_consecutive_errors >= cons:
        br.append(f"{obs.broker_consecutive_errors} consecutive order-API errors (limit {cons})")
    if obs.ws_down_s > L.broker_ws_down_s:
        br.append(f"stream/link down {obs.ws_down_s}s > {L.broker_ws_down_s}s")
    if not obs.session_valid:
        br.append("broker session invalid")
    if obs.unknown_order_age_s > L.broker_unknown_order_s:
        br.append(f"order state UNKNOWN for {obs.unknown_order_age_s}s")
    if br:
        out.append((KillSwitch.BROKER_CONNECTIVITY, "; ".join(br)))
    ab: list[str] = []
    if obs.index_move_frac_in_window > L.abnormal_index_move_frac:
        ab.append(f"index move {obs.index_move_frac_in_window} in {L.abnormal_index_move_window_min} min")
    frm = L.abnormal_index_from_open_frac
    if frm is not None and obs.index_move_from_open_frac > frm:
        ab.append(f"index {obs.index_move_from_open_frac:.4f} from the open > {frm}")
    if obs.vix_jump_frac > L.abnormal_vix_jump_frac:
        ab.append(f"VIX jump {obs.vix_jump_frac:.4f} > {L.abnormal_vix_jump_frac}")
    if obs.market_halt_or_circuit:
        ab.append("market-wide circuit / exchange halt")
    if obs.cas_anomaly:
        ab.append("CAS anomaly")
    if ab:
        out.append((KillSwitch.ABNORMAL_MARKET, "; ".join(ab)))
    if obs.reconciliation_mismatch:
        out.append((KillSwitch.POSITION_RECONCILIATION, obs.reconciliation_mismatch))
    si: list[str] = []
    if not obs.config_hash_ok:
        si.append("config/code hash mismatch")
    if obs.clock_drift_ms > L.clock_drift_ms:
        si.append(f"clock drift {obs.clock_drift_ms} ms")
    if obs.disk_free_frac < L.disk_free_min_frac:
        si.append(f"disk free {obs.disk_free_frac}")
    if obs.journal_write_failed:
        si.append("journal write failure")
    if obs.crash_loop:
        si.append("process crash-loop")
    if obs.memory_pressure:
        si.append("memory pressure")
    if si:
        out.append((KillSwitch.SYSTEM_INTEGRITY, "; ".join(si)))
    pf: list[str] = []
    if obs.exposure_limit_breached:
        pf.append("aggregate exposure / Greek limit breach")
    if obs.nav > 0 and obs.unexplained_pnl_jump > L.portfolio_unexplained_pnl_frac * obs.nav:
        pf.append(f"unexplained P&L jump {obs.unexplained_pnl_jump}")
    if pf:
        out.append((KillSwitch.PORTFOLIO, "; ".join(pf)))
    if obs.manual_master_command:
        out.append((KillSwitch.MANUAL_MASTER, f"owner command: {obs.manual_master_command}"))
    return out

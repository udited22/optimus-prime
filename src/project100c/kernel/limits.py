"""Versioned Risk Governor limits (configs/risk/limits.toml). Validated strictly; Decimal only."""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from project100c.errors import ConfigError

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=False)
DIRECTIVE_DD_CEILING = Decimal("0.15")
_DEC_FIELDS = (
    "per_trade_max_loss_frac",
    "daily_stop_frac",
    "weekly_freeze_frac",
    "dd_warning_frac",
    "dd_suspend_frac",
    "dd_hard_ceiling_frac",
    "buying_power_buffer",
    "spread_max_frac_of_mid",
    "quote_max_age_s",
    "protective_stop_confirm_s",
    "ticket_ttl_s",
    "strategy_slippage_multiple",
    "portfolio_unexplained_pnl_frac",
    "dq_stale_s",
    "dq_healthy_reset_s",
    "broker_ws_down_s",
    "broker_unknown_order_s",
    "broker_error_window_s",
    "abnormal_index_move_frac",
    "abnormal_vix_jump_frac",
    "disk_free_min_frac",
)


class RiskLimits(BaseModel):
    model_config = _FROZEN

    version: str = Field(min_length=1)
    adopted_on: date
    owner_decisions: tuple[str, ...] = ()
    pending_signoff: tuple[str, ...] = ()

    per_trade_max_loss_frac: Decimal
    daily_stop_frac: Decimal
    weekly_freeze_frac: Decimal
    dd_warning_frac: Decimal
    dd_suspend_frac: Decimal
    dd_hard_ceiling_frac: Decimal

    max_lots: int = Field(ge=1)
    max_open_entry_orders: int = Field(ge=1)
    # per-strategy lot cap for multi-leg specs (one lot per leg, at most 2 legs); unlisted strategies use max_lots
    strategy_max_lots: dict[str, int] = Field(default_factory=dict)
    max_trades_per_day: int = Field(ge=1)
    cooldown_after_stop_min: int = Field(ge=0)
    martingale_lookback_trades: int = Field(ge=1)
    min_slippage_ticks_per_side: int = Field(ge=0)
    buying_power_buffer: Decimal

    spread_max_ticks: int = Field(ge=1)
    spread_max_frac_of_mid: Decimal
    min_top_of_book_lots: int = Field(ge=0)
    min_oi: int = Field(ge=0)
    limit_below_bid_ticks: int = Field(ge=0)
    limit_above_ask_ticks: int = Field(ge=0)
    quote_max_age_s: Decimal

    protective_stop_confirm_s: Decimal
    ticket_ttl_s: Decimal

    strategy_consecutive_stops: int = Field(ge=1)
    strategy_slippage_multiple: Decimal
    strategy_slippage_breach_trades: int = Field(ge=1)
    strategy_invalid_intents: int = Field(ge=1)
    portfolio_unexplained_pnl_frac: Decimal
    dq_stale_s: Decimal
    dq_healthy_reset_s: Decimal
    broker_ws_down_s: Decimal
    broker_unknown_order_s: Decimal
    broker_error_threshold: int = Field(ge=1)
    broker_error_window_s: Decimal
    abnormal_index_move_frac: Decimal
    abnormal_index_move_window_min: int = Field(ge=1)
    abnormal_vix_jump_frac: Decimal
    abnormal_cooloff_min: int = Field(ge=1)
    clock_drift_ms: int = Field(ge=1)
    disk_free_min_frac: Decimal
    # OD-017 (2-Oct-2026): risk limits set by capital-preservation practice. None = the rule is not in this version.
    # ABNORMAL_MARKET: |index - session open| / open above this fraction (abnormal_vix_jump_frac is then measured
    # as the VIX rise since the session open)
    abnormal_index_from_open_frac: Decimal | None = None
    # BROKER_CONNECTIVITY: this many order/API errors in a row (with no success in between)
    broker_consecutive_error_threshold: int | None = Field(default=None, ge=1)
    # STRATEGY kill on realised slippage: sum(realised) > strategy_slippage_multiple x sum(modelled) over the last
    # slippage_window_fills fills, or any single fill > slippage_single_fill_multiple x its modelled slippage
    slippage_window_fills: int | None = Field(default=None, ge=1)
    slippage_single_fill_multiple: Decimal | None = None

    @field_validator("abnormal_index_from_open_frac", "slippage_single_fill_multiple", mode="before")
    @classmethod
    def _opt_dec(cls, v: Any) -> Decimal | None:
        return None if v is None else cls._dec(v)

    @field_validator(*_DEC_FIELDS, mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        if isinstance(v, bool) or isinstance(v, float) or not isinstance(v, (str, int, Decimal)):
            raise ValueError(f"must be a decimal string or int, got {type(v).__name__} (floats forbidden)")
        d = Decimal(str(v))
        if not d.is_finite() or d < 0:
            raise ValueError(f"must be finite and >= 0, got {v!r}")
        return d

    def lot_cap(self, strategy_id: str) -> int:
        """The lot cap for one strategy: its ``strategy_max_lots`` entry, else ``max_lots``."""
        return self.strategy_max_lots.get(strategy_id, self.max_lots)

    @model_validator(mode="after")
    def _check(self) -> RiskLimits:
        for name in (
            "per_trade_max_loss_frac",
            "daily_stop_frac",
            "weekly_freeze_frac",
            "dd_warning_frac",
            "dd_suspend_frac",
            "dd_hard_ceiling_frac",
            "spread_max_frac_of_mid",
            "disk_free_min_frac",
            "portfolio_unexplained_pnl_frac",
            "abnormal_index_move_frac",
            "abnormal_vix_jump_frac",
        ):
            v: Decimal = getattr(self, name)
            if not Decimal(0) < v < Decimal(1):
                raise ValueError(f"{self.version}: {name}={v} must be in (0, 1)")
        if self.dd_hard_ceiling_frac > DIRECTIVE_DD_CEILING:
            raise ValueError(f"{self.version}: dd_hard_ceiling_frac above the directive ceiling of 15%")
        if not self.dd_warning_frac < self.dd_suspend_frac < self.dd_hard_ceiling_frac:
            raise ValueError(f"{self.version}: need dd_warning < dd_suspend < dd_hard_ceiling")
        if not self.per_trade_max_loss_frac <= self.daily_stop_frac < self.weekly_freeze_frac:
            raise ValueError(f"{self.version}: need per_trade <= daily_stop < weekly_freeze")
        if self.max_lots != 1:
            raise ValueError(f"{self.version}: canary phase allows exactly 1 lot (docs/risk/risk-engine.md §9.2)")
        for sid, n in self.strategy_max_lots.items():
            if not sid or not 1 <= n <= 2:
                raise ValueError(f"{self.version}: strategy_max_lots[{sid!r}]={n}: one lot per leg, at most 2 legs")
        for name in (
            "quote_max_age_s",
            "protective_stop_confirm_s",
            "ticket_ttl_s",
            "dq_stale_s",
            "broker_ws_down_s",
            "broker_unknown_order_s",
            "broker_error_window_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{self.version}: {name} must be > 0")
        if self.abnormal_index_from_open_frac is not None and not Decimal(0) < self.abnormal_index_from_open_frac < 1:
            raise ValueError(f"{self.version}: abnormal_index_from_open_frac must be in (0, 1)")
        if (self.slippage_window_fills is None) != (self.slippage_single_fill_multiple is None):
            raise ValueError(f"{self.version}: slippage_window_fills and slippage_single_fill_multiple go together")
        if self.slippage_single_fill_multiple is not None and not (
            Decimal(1) < self.strategy_slippage_multiple <= self.slippage_single_fill_multiple
        ):
            raise ValueError(f"{self.version}: need 1 < strategy_slippage_multiple <= slippage_single_fill_multiple")
        unknown = set(self.pending_signoff) - set(type(self).model_fields)
        if unknown:
            raise ValueError(f"{self.version}: pending_signoff names unknown fields {sorted(unknown)}")
        return self


def load_risk_limits(path: Path, version: str | None = None) -> RiskLimits:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read risk limits {path}: {e}") from e
    items = raw.get("limits")
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path}: no [[limits]] tables")
    try:
        parsed = [RiskLimits.model_validate(x) for x in items]
    except ValidationError as e:
        raise ConfigError(f"{path}: invalid risk limits: {e}") from e
    versions = [p.version for p in parsed]
    if len(set(versions)) != len(versions):
        raise ConfigError(f"{path}: duplicate limit versions")
    if version is None:
        # the latest adopted_on; on the same date the later table in the file wins
        return max(enumerate(parsed), key=lambda ip: (ip[1].adopted_on, ip[0]))[1]
    for p in parsed:
        if p.version == version:
            return p
    raise ConfigError(f"{path}: no limits version {version!r}")

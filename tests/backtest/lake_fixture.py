"""Fixture lake for backtest tests: the fake Dhan server (SYNTHETIC prices, tests/data/dhan_fakes.py) run through
the real downloader, so the backtester reads exactly what D-06 writes. No network, no token."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.data.dhan import DhanConfig
from project100c.data.dhan.jobs import CandleJobSpec, RollingOptionJobSpec
from project100c.instruments.lot_history import LotSizeHistory, load_lot_history
from tests.data.dhan_fakes import CONFIGS, FakeDhan, Rig, make_rig

# Futures label for the Aug-2026 monthly contract; the security id is a TEST placeholder, not a real Dhan id.
FUT_LABEL = "NIFTY-FUT-2026-08-25"
INDEX_LABEL = "NIFTY-INDEX"
VIX_LABEL = "INDIA-VIX"  # placeholder id below; the real India VIX security id on Dhan is not yet verified


def expiry_calendar() -> ExpiryCalendar:
    cal = TradingCalendar(load_holiday_book(CONFIGS / "calendar" / "nse_fo_holidays.toml"))
    return ExpiryCalendar(cal, load_expiry_rules(CONFIGS / "calendar" / "nifty_expiry_rules.toml"))


def lot_history() -> LotSizeHistory:
    return load_lot_history(CONFIGS / "instruments" / "nifty_lot_sizes.toml")


def rolling_spec(**kw: Any) -> RollingOptionJobSpec:
    base: dict[str, Any] = dict(from_date=date(2026, 8, 3), to_date=date(2026, 8, 10), strike_offsets=(-1, 0, 1))
    base.update(kw)
    return RollingOptionJobSpec(**base)


def candle_specs(from_date: date, to_date: date) -> list[CandleJobSpec]:
    common: dict[str, Any] = dict(from_date=from_date, to_date=to_date, window_days=90)
    return [
        CandleJobSpec(label=INDEX_LABEL, security_id="13", exchange_segment="IDX_I", instrument="INDEX", **common),
        CandleJobSpec(
            label=FUT_LABEL, security_id="90001", exchange_segment="NSE_FNO", instrument="FUTIDX", oi=True, **common
        ),
        CandleJobSpec(label=VIX_LABEL, security_id="90002", exchange_segment="IDX_I", instrument="INDEX", **common),
    ]


@dataclass
class FixtureLake:
    rig: Rig
    rolling: list[RollingOptionJobSpec]
    candles: list[CandleJobSpec]

    @property
    def config(self) -> DhanConfig:
        return self.rig.cfg


def build(
    tmp: Path,
    rolling: list[RollingOptionJobSpec],
    candles: list[CandleJobSpec],
    fake: FakeDhan | None = None,
) -> FixtureLake:
    rig = make_rig(tmp, fake=fake)
    specs: list[RollingOptionJobSpec | CandleJobSpec] = [*rolling, *candles]
    for spec in specs:
        rig.downloader.run(rig.downloader.create(spec))
    return FixtureLake(rig, rolling, candles)

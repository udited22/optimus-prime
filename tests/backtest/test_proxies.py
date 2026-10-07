"""ASSUMED futures-volume proxy for H01 on Dhan history (no expired-futures candles)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from project100c.backtest.proxies import FUTURES_PROXY_ASSUMPTION, futures_volume_proxy
from project100c.backtest.types import OptionContract
from project100c.core_types import OptionRight
from project100c.errors import BacktestDataError
from project100c.market_types import Bar
from project100c.sessions import IST

D = Decimal
T0 = datetime(2026, 9, 28, 9, 15, tzinfo=IST)


def _c(exp: date, k: int, r: OptionRight) -> OptionContract:
    return OptionContract(f"NIFTY|{exp}|{k}|{r.value}", "NIFTY", exp, D(k), r, 65)


def test_proxy_sums_nearest_expiry_atm_band_volume_and_keeps_index_prices() -> None:
    near, nxt = date(2026, 9, 29), date(2026, 10, 6)
    cs = [
        _c(near, 25000, OptionRight.CE),
        _c(near, 25000, OptionRight.PE),
        _c(near, 25050, OptionRight.CE),
        _c(near, 25150, OptionRight.CE),
        _c(nxt, 25000, OptionRight.CE),
    ]
    contracts = {c.instrument_key: c for c in cs}
    idx = [
        Bar("NIFTY-INDEX", T0, D(25010), D(25020), D(25000), D(25012), 999),
        Bar("NIFTY-INDEX", T0 + timedelta(minutes=1), D(25012), D(25013), D(25001), D(25005), 999),
    ]
    vols = [10, 20, 30, 1000, 5000]  # 25150 is outside +/-1 strike; next expiry is ignored
    opts = [Bar(c.instrument_key, T0, D(100), D(100), D(100), D(100), v) for c, v in zip(cs, vols, strict=True)]
    out = futures_volume_proxy(label="P", index_bars=idx, option_bars=opts, contracts=contracts, strike_step=D(50))
    assert [b.volume for b in out] == [60, 0]  # minute 2: no option bar -> 0
    assert (out[0].open, out[0].high, out[0].low, out[0].close) == (D(25010), D(25020), D(25000), D(25012))
    assert all(b.instrument_key == "P" and b.oi is None for b in out)
    assert "ASSUMED" in FUTURES_PROXY_ASSUMPTION


def test_proxy_refuses_unknown_contracts_and_bad_params() -> None:
    idx = [Bar("NIFTY-INDEX", T0, D(1), D(1), D(1), D(1), 0)]
    with pytest.raises(BacktestDataError):
        futures_volume_proxy(
            label="P",
            index_bars=idx,
            option_bars=[Bar("X", T0, D(1), D(1), D(1), D(1), 1)],
            contracts={},
            strike_step=D(50),
        )
    with pytest.raises(BacktestDataError):
        futures_volume_proxy(label="P", index_bars=idx, option_bars=[], contracts={}, strike_step=D(0))

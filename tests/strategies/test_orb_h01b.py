"""H01b (S-ORB-002): its own RESEARCH spec, H01's mechanics, and the near-the-money option-volume signal series."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.backtest.proxies import futures_volume_proxy
from project100c.backtest.types import OptionContract
from project100c.core_types import OptionRight
from project100c.market_types import Bar
from project100c.sessions import IST
from project100c.spec.io import load_spec_file
from project100c.spec.models import Lifecycle
from project100c.strategies.orb_h01 import OrbParams
from project100c.strategies.orb_h01b import H01B_SIGNAL_DEFINITION, h01b_metadata, h01b_signal_bars

REPO = Path(__file__).resolve().parents[2]
D = Decimal


def test_h01b_spec_is_research_and_h01_is_unchanged() -> None:
    b = load_spec_file(REPO / "specs" / "S-ORB-002.yaml")
    a = load_spec_file(REPO / "specs" / "S-ORB-001.yaml")
    assert b.status is Lifecycle.RESEARCH and b.id == "S-ORB-002" and not b.event_certified
    assert "option volume" in b.hypothesis and "futures" in a.hypothesis
    assert {d.dataset for d in b.required_data} >= {"nifty_index_1m", "nifty_opt_1m_atm_band"}
    assert "nifty_fut_1m" in {d.dataset for d in a.required_data}
    # same mechanics and priors as H01
    pa, pb = OrbParams.from_spec(a), OrbParams.from_spec(b)
    assert (pa.or_minutes, pa.theta_v, pa.stop_pct, pa.target_r) == (
        pb.or_minutes,
        pb.theta_v,
        pb.stop_pct,
        pb.target_r,
    )
    meta = h01b_metadata(pb)
    assert meta["hypothesis"] == "H01b" and meta["signal_series"] == H01B_SIGNAL_DEFINITION
    with pytest.raises(ValueError, match="S-ORB-002"):
        h01b_metadata(pa)


def test_signal_series_is_index_price_with_atm_band_option_volume() -> None:
    t0 = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
    exp = date(2026, 9, 29)
    cs = {
        f"K{k}{r.value}": OptionContract(f"K{k}{r.value}", "NIFTY", exp, D(k), r, 65)
        for k in (25000, 25050, 25100)
        for r in (OptionRight.CE, OptionRight.PE)
    }
    idx = [Bar("IDX", t0, D(25010), D(25020), D(25000), D(25012), 0)]
    opts = [Bar(k, t0, D(1), D(1), D(1), D(1), 10) for k in cs]
    out = h01b_signal_bars(label="SIG", index_bars=idx, option_bars=opts, contracts=cs, strike_step=D(50))
    assert out == futures_volume_proxy(label="SIG", index_bars=idx, option_bars=opts, contracts=cs, strike_step=D(50))
    assert out[0].volume == 40 and out[0].close == D(25012)  # 25000 and 25050, CE+PE; 25100 is 2 strikes away
    assert out[0].start == t0 and timedelta(0) == out[0].start - idx[0].start

"""H01b (``S-ORB-002``, RESEARCH): H01 with the volume filter defined on near-the-money OPTION volume.

Approved by the owner on 2-Oct-2026 (with OD-015) because Dhan has no 1-minute history for expired NIFTY futures.
H01 (``S-ORB-001``) is unchanged; H01b is a separate hypothesis with its own spec and its own evidence.

The signal series (``h01b_signal_bars``) is, by definition: price = NIFTY index OHLC (the breakout is measured on
the index), volume = the summed traded volume of the nearest-expiry CE and PE options within one strike of the
index close in the same minute. That is the same computation as the ASSUMED H01 futures proxy, but here it is the
hypothesis's own input, not a stand-in. The mechanics (opening range, 5-minute closes, 20-day same-slot median,
VIX filter, option selection, stops, targets, sizing) are H01's, so the strategy class is reused unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

from project100c.backtest.proxies import futures_volume_proxy
from project100c.backtest.types import OptionContract
from project100c.market_types import Bar
from project100c.strategies.orb_h01 import OrbH01Strategy, OrbParams, h01_metadata

H01B_SPEC_ID = "S-ORB-002"
H01B_SIGNAL_VERSION = "H01B-SIGNAL-2026-10-02.1"
H01B_SIGNAL_DEFINITION = (
    "H01b signal series (definition, not a proxy): price = NIFTY index OHLC; volume = summed nearest-expiry CE+PE "
    f"option volume within +/-1 strike of the index close in the same minute ({H01B_SIGNAL_VERSION})"
)


def h01b_signal_bars(
    *,
    label: str,
    index_bars: Sequence[Bar],
    option_bars: Iterable[Bar],
    contracts: Mapping[str, OptionContract],
    strike_step: Decimal,
) -> tuple[Bar, ...]:
    """One signal bar per index bar (same start); volume 0 where no near-the-money option traded that minute."""
    return futures_volume_proxy(
        label=label,
        index_bars=index_bars,
        option_bars=option_bars,
        contracts=contracts,
        strike_step=strike_step,
        atm_band=1,
    )


class OrbH01bStrategy(OrbH01Strategy):
    """H01's mechanics on the H01b signal series: pass the ``h01b_signal_bars`` label as ``fut_key``."""


def h01b_metadata(params: OrbParams) -> dict[str, object]:
    if params.spec_id != H01B_SPEC_ID:
        raise ValueError(f"H01b needs spec {H01B_SPEC_ID}, got {params.spec_id}")
    meta = h01_metadata(params)
    meta["hypothesis"] = "H01b"
    meta["signal_series"] = H01B_SIGNAL_DEFINITION
    return meta

"""Proxy series for running bar strategies on vendor history that lacks an input (labelled ASSUMED).

``futures_volume_proxy``: Dhan cannot serve 1-minute candles for EXPIRED NIFTY futures (no security id for expired
contracts in the public scrip master, verified 2-Oct-2026), but H01 (S-ORB-001) needs a futures-like 1-minute series
with volume. The proxy takes the NIFTY index OHLC for price and, for volume, the summed traded volume of the
nearest-expiry options within ``atm_band`` strikes of the index close in the same minute (CE + PE). It is an
assumption, not data: the index has no basis to futures, and option volume is not futures volume. Every run that
uses it must carry ``FUTURES_PROXY_ASSUMPTION`` in its metadata.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from project100c.backtest.types import OptionContract
from project100c.errors import BacktestDataError
from project100c.market_types import Bar
from project100c.sessions.model import IST

FUTURES_PROXY_VERSION = "FUTPROXY-2026-10-02.1"
FUTURES_PROXY_ASSUMPTION = (
    "ASSUMED: H01's NIFTY-futures 1-minute input is a PROXY (no expired-futures history from Dhan): price = NIFTY "
    "index OHLC (no futures basis), volume = summed volume of nearest-expiry CE+PE options within +/-1 strike of the "
    f"index close in the same minute ({FUTURES_PROXY_VERSION})"
)


def futures_volume_proxy(
    *,
    label: str,
    index_bars: Sequence[Bar],
    option_bars: Iterable[Bar],
    contracts: Mapping[str, OptionContract],
    strike_step: Decimal,
    atm_band: int = 1,
) -> tuple[Bar, ...]:
    """One proxy bar per index bar (same start); volume 0 where no qualifying option traded that minute."""
    if strike_step <= 0 or atm_band < 0:
        raise BacktestDataError("strike_step must be > 0 and atm_band >= 0")
    by_ts: dict[datetime, list[tuple[OptionContract, int]]] = defaultdict(list)
    for b in option_bars:
        c = contracts.get(b.instrument_key)
        if c is None:
            raise BacktestDataError(f"{b.instrument_key}: option bar without a contract")
        by_ts[b.start].append((c, b.volume))
    out: list[Bar] = []
    band = strike_step * atm_band
    for ib in index_bars:
        day = ib.start.astimezone(IST).date()
        cands = by_ts.get(ib.start, [])
        expiries = sorted({c.expiry for c, _ in cands if c.expiry >= day})
        vol = 0
        if expiries:
            near = expiries[0]
            vol = sum(v for c, v in cands if c.expiry == near and abs(c.strike - ib.close) <= band)
        out.append(Bar(label, ib.start, ib.open, ib.high, ib.low, ib.close, vol, None))
    return tuple(out)

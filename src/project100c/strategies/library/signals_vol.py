"""Plug-ins for the volatility-information round (H32-H34).

Pre-registered before any of them ran on data. Same
contract as ``signals.py``: closed bars only, at most one ``EntrySignal`` a bar, long options only.

The volatility specs read the day's alternative-model labels as point-in-time daily features (``Session.features``,
built by scripts/vol_features.py from the H25/H26 label files):

* ``h25`` = 0 / 1 / 2 for TERM_LOW / TERM_MID / TERM_HIGH, usable from ``h25_from`` minutes after 09:15 (09:30 = 15);
* ``h26`` = 0 / 1 / 2 for HMM_CALM / HMM_MID / HMM_TURBULENT, known before the open; only days from the HMM's first
  out-of-sample fit carry it.

The K-11 v0 volatility label is the classifier's own (``Session.label``). "High forecast volatility" (pre-registered)
is ANY of: K-11 VOLATILITY_EXPANSION at the decision bar, H25 TERM_LOW, H26 HMM_TURBULENT. Directional labels are
never used.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from decimal import Decimal

from project100c.core_types import OptionRight
from project100c.strategies.library.base import BasePlugin, EntrySignal, Session
from project100c.strategies.library.signals_intel import DayVolatility, _mins, _q, _t

CE, PE = OptionRight.CE, OptionRight.PE


def high_vol(s: Session) -> tuple[str, ...]:
    """Which of the three volatility labels call for high volatility at this bar (empty = none)."""
    out: list[str] = []
    if s.label is not None and any(t.value == "VOLATILITY_EXPANSION" for t in s.label.tags()):
        out.append("K11_EXPANSION")
    f = s.features
    if "h25" in f and int(f["h25"]) == 0 and _mins(_t(s)) >= int(f.get("h25_from", Decimal(15))):
        out.append("H25_TERM_LOW")
    if "h26" in f and int(f["h26"]) == 2:
        out.append("H26_TURBULENT")
    return tuple(out)


# ---------------------------------------------------------------------------------------------- H32
class VolCheapStraddle(DayVolatility):
    """H32 S-VOLCHEAP-001: on a high-forecast-volatility day (any of the three labels), a long ATM straddle when the
    HAR forecast of today's open-to-close variance is at least ``theta_ratio`` x the one-session variance implied by
    the nearest-weekly ATM IV; premium-aware stop (the widest that fits the 2% budget); H19's thesis check at
    12:30."""

    code = "H32"
    deviations = (
        *DayVolatility.deviations[:2],
        "TERM: no term guard (H25 TERM_LOW, a flat or inverted front, is one of the qualifying labels).",
        "STOP: premium-aware per-leg stop (budget_stop_min_pct .. the spec %), the same % on both legs.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        self.h.see(s, self._summary)
        now = _t(s)
        if s.expiry_day or not (time(9, 45) <= now <= time(11, 0)) or s.state.get("done"):
            return None
        why = high_vol(s)
        if not why:
            return None
        fc = self._forecast(s, int(p["har_lookback_days"]))
        one = self._atm_iv(s, 0)
        if fc is None or one is None:
            return None
        iv1, _, strike = one
        ratio = fc / (iv1 * iv1 / 252)
        if ratio < float(p["theta_ratio"]):
            return None
        s.state["done"] = True
        facts = {"forecast_var": _q(fc, "0.000000001"), "iv_atm": _q(iv1, "0.0001"), "ratio": _q(ratio, "0.001"),
                 "strike": strike, "high_vol": ",".join(why)}  # fmt: skip
        return EntrySignal((CE, PE), "VOL_CHEAP_HIGH_FORECAST", facts)


# ---------------------------------------------------------------------------------------------- H33
class VolHoldStraddle(BasePlugin):
    """H33 S-VOLHOLD-001: the first bar from 09:31 to 09:45 on which a high-volatility label holds (no price
    condition) -> long ATM straddle, held toward 14:45 (premium-aware stop, no target, no thesis exit)."""

    code = "H33"
    deviations = ("STOP: premium-aware per-leg stop (budget_stop_min_pct .. the spec %), the same % on both legs.",)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if s.expiry_day or not (time(9, 31) <= now <= time(9, 45)) or s.state.get("done"):
            return None
        why = high_vol(s)
        if not why:
            return None
        s.state["done"] = True
        return EntrySignal((CE, PE), "HIGH_FORECAST_VOL_HOLD", {"high_vol": ",".join(why)})


# ---------------------------------------------------------------------------------------------- H34
class ExpiryVolStraddle(BasePlugin):
    """H34 S-EXPVOL-001: on a weekly expiry day, the first bar from 09:31 to 10:30 on which a high-volatility label
    holds -> long ATM straddle of the expiring contract (0 DTE), premium-aware stop, time exit 14:30."""

    code = "H34"
    deviations = (
        "EVENTS: event days are excluded (no event certification; the calendar starts Apr-2025).",
        "STOP: premium-aware per-leg stop (budget_stop_min_pct .. the spec %), the same % on both legs.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if not s.expiry_day or s.event_day or not (time(9, 31) <= now <= time(10, 30)) or s.state.get("done"):
            return None
        why = high_vol(s)
        if not why:
            return None
        s.state["done"] = True
        return EntrySignal((CE, PE), "EXPIRY_HIGH_FORECAST_VOL", {"high_vol": ",".join(why)})


VOL_PLUGINS: dict[str, type[BasePlugin]] = {
    "S-VOLCHEAP-001": VolCheapStraddle,
    "S-VOLHOLD-001": VolHoldStraddle,
    "S-EXPVOL-001": ExpiryVolStraddle,
}

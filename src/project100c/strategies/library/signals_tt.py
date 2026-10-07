"""Plug-ins for the top-traders candidates H37-H40 (TT-1..TT-4), promoted from specs/drafts/top-traders/.

Pre-registered before any run. Same contract
as ``signals.py``: closed bars only, at most one ``EntrySignal`` a bar, long options only.

Daily inputs come from ``Session.features`` (scripts/daily_index_features.py, completed sessions only): ``prev_high``,
``prev_low``, ``prev_range``, ``nr7`` (1 when the previous session's range is the narrowest of the last 7), ``r20``
(the 20-session return to the previous close), ``sma200`` (the 200-session average of closes to the previous close)
and ``rsi2`` (Wilder RSI(2) of daily closes at the previous close). A day without a feature produces no signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from decimal import Decimal

from project100c.core_types import OptionRight
from project100c.strategies.library.base import BasePlugin, EntrySignal, OpenTrade, Session
from project100c.strategies.library.signals_intel import _t

CE, PE = OptionRight.CE, OptionRight.PE


def _feat(s: Session, *keys: str) -> list[Decimal] | None:
    f = s.features
    return [f[k] for k in keys] if all(k in f for k in keys) else None


def _best_since(s: Session, t: OpenTrade, up: bool) -> Decimal:
    bars = s.window(t.signal_at, s.end)
    if not bars:
        return t.spot_at_signal
    return max(b.high for b in bars) if up else min(b.low for b in bars)


def _trail_hit(s: Session, t: OpenTrade, width: Decimal) -> bool:
    up = t.signal.rights == (CE,)
    best = _best_since(s, t, up)
    c = s.last.close
    return c <= best - width if up else c >= best + width


# ---------------------------------------------------------------------------------------------- H37
class TrendDay(BasePlugin):
    """H37 S-TRDAY-001 (TT-1): trend day at 11:00-12:30 -> ATM CE/PE, no target, index trailing stop."""

    code = "H37"
    deviations = (
        "VWAP: the index TWAP proxy (the index has no volume; the futures-volume series is UNVERIFIED in the draft).",
        "TREND: the 20-session return is the daily feature r20 (completed sessions only).",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if not (time(11, 0) <= now <= time(12, 30)) or s.state.get("done"):
            return None
        f = _feat(s, "r20")
        if f is None:
            return None
        ib = s.window(s.at(9, 15), s.at(10, 15))
        late = s.window(s.at(10, 15), s.end)
        morning = s.window(s.at(9, 15), s.at(11, 0))
        if len(ib) < 30 or not late or not morning:
            return None
        ib_hi, ib_lo = s.hi_lo(ib)
        mh, ml = s.hi_lo(morning)
        c = s.last.close
        r_open = (c / s.open - 1) * 100
        vwap, _ = s.vwap(use_futures_volume=False)
        mv = p["move_min_pct"]
        facts = {"ib_high": ib_hi, "ib_low": ib_lo, "trail": p["trail_k"] * (mh - ml), "r20": f[0],
                 "r_open_pct": r_open.quantize(Decimal("0.001"))}  # fmt: skip
        hi_late, lo_late = s.hi_lo(late)
        if r_open >= mv and hi_late > ib_hi and c > vwap and f[0] > 0:
            s.state["done"] = True
            return EntrySignal((CE,), "TREND_DAY_UP", facts)
        if r_open <= -mv and lo_late < ib_lo and c < vwap and f[0] < 0:
            s.state["done"] = True
            return EntrySignal((PE,), "TREND_DAY_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        c = s.last.close
        up = t.signal.rights == (CE,)
        if (up and c < f["ib_high"]) or (not up and c > f["ib_low"]):
            return "INVALIDATED_BACK_IN_IB"
        return "TRAIL_STOP" if _trail_hit(s, t, f["trail"]) else None


# ---------------------------------------------------------------------------------------------- H38
class NarrowRange7(BasePlugin):
    """H38 S-NR7-001 (TT-2): after an NR7 session, the first 1m close beyond its high/low (09:30-12:00)."""

    code = "H38"

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if not (time(9, 30) <= now <= time(12, 0)) or s.state.get("done"):
            return None
        if int(p["nr_lookback"]) != 7:
            raise ValueError("the daily feature is NR7; another lookback needs its own feature")
        f = _feat(s, "nr7", "prev_high", "prev_low", "prev_range")
        if f is None or int(f[0]) != 1:
            return None
        _, hi, lo, rng = f
        buf = p["buffer_frac"] * rng
        c = s.last.close
        facts = {"nr7_high": hi, "nr7_low": lo, "width": rng}
        if c > hi + buf:
            s.state["done"] = True
            return EntrySignal((CE,), "NR7_BREAK_UP", facts)
        if c < lo - buf:
            s.state["done"] = True
            return EntrySignal((PE,), "NR7_BREAK_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        c = s.last.close
        up = t.signal.rights == (CE,)
        if (up and c <= f["nr7_high"]) or (not up and c >= f["nr7_low"]):
            return "INVALIDATED_BACK_IN_RANGE"
        return "TRAIL_STOP" if _trail_hit(s, t, f["width"]) else None


# ---------------------------------------------------------------------------------------------- H39
class OopsGap(BasePlugin):
    """H39 S-OOPS-001 (TT-3): a gap beyond the previous extreme that re-enters the previous range before 11:00 ->
    buy against the gap; all out at the previous close."""

    code = "H39"
    deviations = ("EXIT: all at the previous close (the draft's half-out + trail needs partial exits).",)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if not (time(9, 20) <= now <= time(11, 0)) or s.state.get("done") or s.prev_close is None:
            return None
        f = _feat(s, "prev_high", "prev_low")
        if f is None:
            return None
        hi, lo = f
        g = p["gap_min_pct"] / 100
        c = s.last.close
        day_hi, day_lo = s.hi_lo(s.idx)
        if s.open >= hi * (1 + g) and c <= hi:
            s.state["done"] = True
            return EntrySignal((PE,), "OOPS_UP_GAP_FAILED", {"prev_close": s.prev_close, "extreme": day_hi})
        if s.open <= lo * (1 - g) and c >= lo:
            s.state["done"] = True
            return EntrySignal((CE,), "OOPS_DOWN_GAP_FAILED", {"prev_close": s.prev_close, "extreme": day_lo})
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        c = s.last.close
        if t.signal.rights == (PE,):
            return (
                "TARGET_GAP_FILLED"
                if c <= f["prev_close"]
                else "INVALIDATED_EXTREME_RETAKEN"
                if c > f["extreme"]
                else None
            )
        return (
            "TARGET_GAP_FILLED" if c >= f["prev_close"] else "INVALIDATED_EXTREME_RETAKEN" if c < f["extreme"] else None
        )


# ---------------------------------------------------------------------------------------------- H40
class PullbackTrend(BasePlugin):
    """H40 S-PBTREND-001 (TT-4): daily RSI(2) pullback in the 200-session trend, confirmed by a break of the
    09:15-09:45 range in the trend direction (09:45-11:30)."""

    code = "H40"

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if not (time(9, 45) <= now <= time(11, 30)) or s.state.get("done") or s.prev_close is None:
            return None
        if int(p["sma_len"]) != 200:
            raise ValueError("the daily feature is the 200-session average")
        f = _feat(s, "sma200", "rsi2")
        if f is None:
            return None
        sma, rsi2 = f
        orb = s.window(s.at(9, 15), s.at(9, 45))
        if len(orb) < 20:
            return None
        hi, lo = s.hi_lo(orb)
        c = s.last.close
        lo_r = p["rsi_low"]
        facts = {"or_high": hi, "or_low": lo, "width": hi - lo, "sma200": sma, "rsi2": rsi2}
        if s.prev_close > sma and rsi2 < lo_r and c > hi:
            s.state["done"] = True
            return EntrySignal((CE,), "PULLBACK_UPTREND_CONFIRMED", facts)
        if s.prev_close < sma and rsi2 > 100 - lo_r and c < lo:
            s.state["done"] = True
            return EntrySignal((PE,), "PULLBACK_DOWNTREND_CONFIRMED", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        c = s.last.close
        up = t.signal.rights == (CE,)
        if (up and c < f["or_low"]) or (not up and c > f["or_high"]):
            return "INVALIDATED_RANGE_BROKEN"
        return "TRAIL_STOP" if _trail_hit(s, t, f["width"]) else None


TT_PLUGINS: dict[str, type[BasePlugin]] = {
    "S-TRDAY-001": TrendDay,
    "S-NR7-001": NarrowRange7,
    "S-OOPS-001": OopsGap,
    "S-PBTREND-001": PullbackTrend,
}

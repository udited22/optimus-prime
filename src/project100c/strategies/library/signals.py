"""Signal plug-ins for the long-option strategy library. Each reads its numbers from the spec's ``signal.params``.

Every plug-in sees only closed bars of the current session (``Session``) and returns at most one ``EntrySignal``
per bar. ``check_exit`` returns the reason when the plug-in's own invalidation or target fires. Direction is always
expressed by *which option to buy*: up -> BUY CE, down -> BUY PE (OD-006, never a sale).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from decimal import Decimal
from itertools import pairwise

from project100c.core_types import OptionRight
from project100c.market_types import Bar
from project100c.regime import OpenCharacter
from project100c.strategies.library.base import BasePlugin, EntrySignal, OpenTrade, Session
from project100c.strategies.library.signals_intel import (
    DayVolatility,
    GammaMomentum,
    IntradayMomentum,
    NoiseAreaBreakout,
)
from project100c.strategies.library.signals_tt import TT_PLUGINS
from project100c.strategies.library.signals_vol import VOL_PLUGINS

CE, PE = OptionRight.CE, OptionRight.PE
_HUNDRED = Decimal(100)


def _pct(a: Decimal, b: Decimal) -> Decimal:
    return (a / b - 1) * _HUNDRED


def _int(p: Mapping[str, Decimal], k: str) -> int:
    return int(p[k])


def _opening_range(s: Session, minutes: int) -> tuple[Decimal, Decimal] | None:
    if s.minutes < minutes:
        return None
    return s.hi_lo(s.idx[:minutes])


def _mean_range(bars: list[Bar]) -> Decimal:
    return sum((b.high - b.low for b in bars), Decimal(0)) / len(bars)


# ---------------------------------------------------------------------------------------------- H02
class FailedBreakout(BasePlugin):
    """H02 S-FBO-001: a break beyond the opening range that is back inside by ``reentry_depth`` x OR-width within
    ``window_minutes`` -> buy the option pointing back across the range."""

    code = "H02"
    deviations = (
        "VOLUME: 'declining volume on re-entry' is not checked (futures volume UNCERTAIN); price-only rule.",
        "LEVELS: only the opening range is used, not the previous-day high/low.",
        "TARGET: the range midpoint is the first and only target (no scale-out to the opposite edge).",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        orng = _opening_range(s, _int(p, "or_window_minutes"))
        if orng is None or s.minutes == _int(p, "or_window_minutes"):
            return None
        hi, lo = orng
        w = hi - lo
        if w <= 0:
            return None
        b = s.last
        brk = s.state.get("break")
        if brk is None:
            if b.close > hi + p["min_break_frac"] * w:
                s.state["break"] = ("UP", s.minutes, b.high)
            elif b.close < lo - p["min_break_frac"] * w:
                s.state["break"] = ("DOWN", s.minutes, b.low)
            return None
        side, at, ext = brk
        ext = max(ext, b.high) if side == "UP" else min(ext, b.low)
        s.state["break"] = (side, at, ext)
        if s.minutes - at > _int(p, "window_minutes"):
            s.state["break"] = None  # it did not fail in time: not this setup
            return None
        depth = p["reentry_depth"] * w
        facts = {"or_high": hi, "or_low": lo, "extreme": ext, "mid": (hi + lo) / 2}
        if side == "UP" and b.close < hi - depth:
            s.state["break"] = None
            return EntrySignal((PE,), "FAILED_UPSIDE_BREAK", facts)
        if side == "DOWN" and b.close > lo + depth:
            s.state["break"] = None
            return EntrySignal((CE,), "FAILED_DOWNSIDE_BREAK", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f, c = t.signal.facts, s.last.close
        if t.signal.rights[0] is PE:
            return (
                "INVALIDATED_EXTREME_RECLAIMED" if c > f["extreme"] else "TARGET_RANGE_MID" if c <= f["mid"] else None
            )
        return "INVALIDATED_EXTREME_RECLAIMED" if c < f["extreme"] else "TARGET_RANGE_MID" if c >= f["mid"] else None


# ---------------------------------------------------------------------------------------------- H03
class VwapContinuation(BasePlugin):
    """H03 S-VWAPC-001: on a day that has held one side of VWAP for ``trend_share`` of the minutes since 09:30, a
    pullback that touches VWAP (within ``touch_band_pct``) and closes back in the trend direction."""

    code = "H03"
    uses_futures_volume = True
    deviations = (
        "VWAP: futures-volume VWAP when futures volume is supplied, else the index TWAP proxy (the substitute); "
        "the run metadata records which basis was used.",
        "BANDS: the touch band and the stop are in % of VWAP, not in intraday sigma.",
        "TARGET: 1.5 R only (the prior-swing target is not implemented).",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        if s.minutes < _int(p, "start_minute"):
            return None
        vwap, basis = s.vwap(use_futures_volume=bool(p.get("use_futures_volume", Decimal(1))))
        s.state.setdefault("sides", []).append(1 if s.last.close > vwap else -1)
        sides = s.state["sides"]
        share_up = Decimal(sum(1 for x in sides if x > 0)) / len(sides)
        b, prev = s.last, s.idx[-2]
        band = vwap * p["touch_band_pct"] / _HUNDRED
        facts = {"vwap": vwap.quantize(Decimal("0.01")), "vwap_basis": basis, "share_up": share_up}
        if share_up >= p["trend_share"] and b.low <= vwap + band and b.close > vwap and b.close > prev.close:
            return EntrySignal((CE,), "VWAP_PULLBACK_UP", facts)
        if 1 - share_up >= p["trend_share"] and b.high >= vwap - band and b.close < vwap and b.close < prev.close:
            return EntrySignal((PE,), "VWAP_PULLBACK_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        vwap, _ = s.vwap(use_futures_volume=bool(p.get("use_futures_volume", Decimal(1))))
        stop = vwap * p["stop_band_pct"] / _HUNDRED
        c = s.last.close
        lost = c < vwap - stop if t.signal.rights[0] is CE else c > vwap + stop
        return "INVALIDATED_VWAP_LOST" if lost else None


# ---------------------------------------------------------------------------------------------- H04
class VwapMeanReversion(BasePlugin):
    """H04 S-VWAPMR-001: a deviation from VWAP beyond ``entry_sigma`` that starts to turn (the bar closes back
    toward VWAP) -> buy the option pointing to VWAP; target the VWAP touch, invalidate beyond ``stop_sigma``."""

    code = "H04"
    deviations = (
        "VWAP: the index TWAP proxy (no futures volume); sigma is the session standard deviation of close - VWAP.",
        "VIX_TERCILE: 'VIX in the bottom tercile of its 1-year range' is replaced by the classifier's "
        "volatility label.",
    )

    @staticmethod
    def _z(s: Session) -> tuple[Decimal, Decimal] | None:
        vwap, _ = s.vwap(use_futures_volume=False)
        devs = s.state.setdefault("devs", [])
        devs.append(s.last.close - vwap)
        if len(devs) < 30:
            return None
        sd = Decimal(repr(statistics.pstdev(float(x) for x in devs)))
        return ((s.last.close - vwap) / sd, vwap) if sd > 0 else None

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        zv = self._z(s)
        s.state["zv"] = zv
        if zv is None:
            return None
        z, vwap = zv
        b = s.last
        facts = {"z": z.quantize(Decimal("0.01")), "vwap": vwap.quantize(Decimal("0.01"))}
        if z >= p["entry_sigma"] and b.close < b.open:
            return EntrySignal((PE,), "STRETCHED_ABOVE_VWAP", facts)
        if z <= -p["entry_sigma"] and b.close > b.open:
            return EntrySignal((CE,), "STRETCHED_BELOW_VWAP", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        zv = s.state.get("zv")
        if zv is None:
            return None
        z, vwap = zv
        c = s.last.close
        if t.signal.rights[0] is PE:
            return "TARGET_VWAP" if c <= vwap else "INVALIDATED_DEVIATION_EXTENDED" if z >= p["stop_sigma"] else None
        return "TARGET_VWAP" if c >= vwap else "INVALIDATED_DEVIATION_EXTENDED" if z <= -p["stop_sigma"] else None


# ---------------------------------------------------------------------------------------------- H05
class CompressionBreakout(BasePlugin):
    """H05 S-VOLX-001: after ``comp_minutes`` whose high-low box is within ``comp_max_pct`` of price, a close beyond
    the box on a bar whose range is ``expansion_mult`` x the box's mean bar range (and, with futures volume, whose
    volume is ``volume_mult`` x the box mean) -> buy in the breakout direction."""

    code = "H05"
    uses_futures_volume = True
    deviations = (
        "VOLUME: 'with volume' uses futures volume when supplied; without it the bar-range expansion filter alone is "
        "the substitute.",
        "COMPRESSION: an absolute box height (% of price) replaces 'bottom decile of the day's 15-min ranges'.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        n = _int(p, "comp_minutes")
        if s.minutes <= n:
            return None
        t = s.end
        if p.get("lunch_filter", Decimal(1)) and s.at(12, 0) <= t < s.at(13, 30):
            return None
        box = s.idx[-n - 1 : -1]
        hi, lo = s.hi_lo(box)
        if (hi - lo) / s.last.close * _HUNDRED > p["comp_max_pct"]:
            return None
        b = s.last
        if b.high - b.low < p["expansion_mult"] * _mean_range(box):
            return None
        fbox = [f for f in s.fut[-n - 1 : -1] if f is not None]
        fnow = s.fut[-1]
        vol_ok, basis = True, "RANGE_ONLY"
        if fbox and fnow is not None and all(f.volume > 0 for f in fbox):
            basis = "FUT_VOLUME"
            vol_ok = fnow.volume >= p["volume_mult"] * Decimal(sum(f.volume for f in fbox)) / len(fbox)
        if not vol_ok:
            return None
        facts = {"box_high": hi, "box_low": lo, "confirmation": basis}
        if b.close > hi:
            return EntrySignal((CE,), "COMPRESSION_BREAK_UP", facts)
        if b.close < lo:
            return EntrySignal((PE,), "COMPRESSION_BREAK_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        mid = (f["box_high"] + f["box_low"]) / 2
        c = s.last.close
        back = c < (f["box_high"] + mid) / 2 if t.signal.rights[0] is CE else c > (f["box_low"] + mid) / 2
        return "INVALIDATED_BACK_IN_BOX" if back else None


# ---------------------------------------------------------------------------------------------- H06
class CheapGamma(BasePlugin):
    """H06 S-IVRV-001: when implied vol (India VIX as the proxy) is below trailing realised vol by more than
    ``iv_discount_pts`` vol points, buy one ATM option in the direction of the VWAP side + 15-minute momentum."""

    code = "H06"
    deviations = (
        "IV: India VIX stands in for the own Black-76 ATM weekly IV (D-11 not built).",
        "RV: trailing realised vol over rv_minutes stands in for the HAR-RV forecast.",
        "DIRECTION: VWAP side from the index TWAP proxy plus the sign of the 15-minute return.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        n = _int(p, "rv_minutes")
        if s.minutes < max(n + 1, _int(p, "start_minute")):
            return None
        vix = s.vix_now()
        if vix is None:
            return None
        closes = [float(b.close) for b in s.idx[-n - 1 :]]
        rets = [math.log(b / a) for a, b in pairwise(closes)]
        rv = Decimal(repr(statistics.pstdev(rets) * (252 * 375) ** 0.5 * 100))
        if vix > rv - p["iv_discount_pts"]:
            return None
        vwap, _ = s.vwap(use_futures_volume=False)
        mom = s.last.close - s.idx[-16].close
        facts = {"iv_proxy": vix, "rv": rv.quantize(Decimal("0.01")), "vwap": vwap.quantize(Decimal("0.01"))}
        if s.last.close > vwap and mom > 0:
            return EntrySignal((CE,), "CHEAP_GAMMA_UP", facts)
        if s.last.close < vwap and mom < 0:
            return EntrySignal((PE,), "CHEAP_GAMMA_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        vwap, _ = s.vwap(use_futures_volume=False)
        c = s.last.close
        flipped = c < vwap if t.signal.rights[0] is CE else c > vwap
        return "INVALIDATED_DIRECTION" if flipped else None


# ---------------------------------------------------------------------------------------------- H07
class ExpiryAfternoonMomentum(BasePlugin):
    """H07 S-EXP0-001: on expiry day after 13:00, a new session extreme with a 15-minute move of at least
    ``mom_pct`` -> buy the near-OTM same-day option in that direction."""

    code = "H07"
    deviations = (
        "MOMENTUM: a fixed 15-minute move threshold replaces 'above the 80th percentile (time-matched)'.",
        "PREMIUM_BAND: the INR 5-20 premium band is not enforced; the strike is otm_steps from spot.",
        "UNDERLYING: index bars stand in for futures.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        if not s.expiry_day or s.end < s.at(13, 0) or s.minutes < 20:
            return None
        k = _int(p, "mom_minutes")
        b = s.last
        before = s.idx[:-1]
        mom = _pct(b.close, s.idx[-1 - k].close)
        facts = {"mom_pct": mom.quantize(Decimal("0.001")), "swing_low": min(x.low for x in s.idx[-k:]),
                 "swing_high": max(x.high for x in s.idx[-k:])}  # fmt: skip
        if b.close > max(x.high for x in before) and mom >= p["mom_pct"]:
            return EntrySignal((CE,), "EXPIRY_NEW_HIGH_MOMENTUM", facts)
        if b.close < min(x.low for x in before) and mom <= -p["mom_pct"]:
            return EntrySignal((PE,), "EXPIRY_NEW_LOW_MOMENTUM", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f, c = t.signal.facts, s.last.close
        broke = c < f["swing_low"] if t.signal.rights[0] is CE else c > f["swing_high"]
        return "INVALIDATED_SWING_BROKEN" if broke else None


# ---------------------------------------------------------------------------------------------- new: gap and go
class GapAndGo(BasePlugin):
    """S-GAPGO-001: a gap of at least ``gap_min_pct`` whose open was a drive in the gap's direction; buy on a close
    beyond the first ``or_window_minutes`` range in that direction."""

    code = "GAPGO"
    deviations = ("DRIVE: the opening character comes from the K-11 classifier (UNVALIDATED) at 09:45.",)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        orng = _opening_range(s, _int(p, "or_window_minutes"))
        if orng is None or s.prev_close is None or s.label is None:
            return None
        if s.label.opening is not OpenCharacter.OPENING_DRIVE:
            return None
        gap = _pct(s.open, s.prev_close)
        hi, lo = orng
        facts = {"gap_pct": gap.quantize(Decimal("0.001")), "or_high": hi, "or_low": lo, "open": s.open}
        if gap >= p["gap_min_pct"] and s.last.close > hi:
            return EntrySignal((CE,), "GAP_UP_AND_GO", facts)
        if gap <= -p["gap_min_pct"] and s.last.close < lo:
            return EntrySignal((PE,), "GAP_DOWN_AND_GO", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        o, c = t.signal.facts["open"], s.last.close
        failing = c < o if t.signal.rights[0] is CE else c > o
        return "INVALIDATED_BACK_THROUGH_OPEN" if failing else None


# ---------------------------------------------------------------------------------------------- new: gap fade
class GapFade(BasePlugin):
    """S-GAPFADE-001: a gap of at least ``gap_min_pct`` whose open reverted; buy against the gap on a close beyond
    the opening price toward the previous close; target the gap fill, invalidate beyond the opening range."""

    code = "GAPFADE"
    deviations = ("REVERSION: the opening character comes from the K-11 classifier (UNVALIDATED) at 09:45.",)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        orng = _opening_range(s, _int(p, "or_window_minutes"))
        if orng is None or s.prev_close is None or s.label is None:
            return None
        if s.label.opening is not OpenCharacter.MEAN_REVERSION:
            return None
        gap = _pct(s.open, s.prev_close)
        hi, lo = orng
        c = s.last.close
        facts = {"gap_pct": gap.quantize(Decimal("0.001")), "prev_close": s.prev_close, "or_high": hi, "or_low": lo}
        if gap >= p["gap_min_pct"] and c < s.open and c > s.prev_close:
            return EntrySignal((PE,), "GAP_UP_FADE", facts)
        if gap <= -p["gap_min_pct"] and c > s.open and c < s.prev_close:
            return EntrySignal((CE,), "GAP_DOWN_FADE", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f, c = t.signal.facts, s.last.close
        if t.signal.rights[0] is PE:
            return (
                "TARGET_GAP_FILLED" if c <= f["prev_close"] else "INVALIDATED_GAP_RESUMED" if c > f["or_high"] else None
            )
        return "TARGET_GAP_FILLED" if c >= f["prev_close"] else "INVALIDATED_GAP_RESUMED" if c < f["or_low"] else None


# ---------------------------------------------------------------------------------------------- new: VIX-spike straddle
class VixSpikeStraddle(BasePlugin):
    """S-VIXSTR-001: India VIX up at least ``vix_jump_pct`` over ``vix_minutes`` while the index has moved at least
    ``move_pct`` over 15 minutes -> buy the ATM CE and the ATM PE (a long straddle: two BUY legs, two lots)."""

    code = "VIXSTR"
    deviations = (
        "LOTS: two concurrent lots. The 1-lot canary cap (limits.toml max_lots = 1, Governor MAX_POSITION) blocks it "
        "in canary; mechanics tests run the engine with max_lots = 2.",
        "STOP: a premium stop per leg, not one on the combined premium.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        k = _int(p, "vix_minutes")
        now, ago = s.vix_now(), s.vix_ago(k)
        if now is None or ago is None or ago <= 0 or s.minutes < 16:
            return None
        jump = _pct(now, ago)
        move = abs(_pct(s.last.close, s.idx[-16].close))
        if jump >= p["vix_jump_pct"] and move >= p["move_pct"]:
            return EntrySignal((CE, PE), "VIX_SPIKE", {"vix": now, "vix_before": ago, "jump_pct": jump.quantize(
                Decimal("0.01")), "move_pct": move.quantize(Decimal("0.001"))})  # fmt: skip
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        now = s.vix_now()
        return "INVALIDATED_VIX_REVERTED" if now is not None and now <= t.signal.facts["vix_before"] else None


# ---------------------------------------------------------------------------------------------- new: event breakout
class EventBreakout(BasePlugin):
    """S-EVTBO-001: on a listed event day, the range of the ``pre_minutes`` before the announcement minute; after it,
    a close beyond that range by ``buffer_pct`` -> buy in the break direction."""

    code = "EVTBO"
    deviations = (
        "EVENT_TIME: the announcement time is the spec param event_minute (ASSUMED 10:00 IST for RBI MPC); the event "
        "calendar lists dates only.",
        "CERTIFICATION: the Governor needs event_certified intents on event days; that flag is not set by research.",
    )

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        if not s.event_day:
            return None
        ev = _int(p, "event_minute")
        if s.minutes <= ev:
            return None
        box = s.idx[ev - _int(p, "pre_minutes") : ev]
        hi, lo = s.hi_lo(box)
        buf = s.last.close * p["buffer_pct"] / _HUNDRED
        facts = {"pre_high": hi, "pre_low": lo}
        if s.last.close > hi + buf:
            return EntrySignal((CE,), "EVENT_BREAK_UP", facts)
        if s.last.close < lo - buf:
            return EntrySignal((PE,), "EVENT_BREAK_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f, c = t.signal.facts, s.last.close
        back = c < f["pre_high"] if t.signal.rights[0] is CE else c > f["pre_low"]
        return "INVALIDATED_BACK_IN_RANGE" if back else None


# ---------------------------------------------------------------------------------------------- new: lunch compression
class LunchCompressionBreakout(BasePlugin):
    """S-LUNCH-001: a 12:00-13:00 box no taller than ``box_max_pct`` of price and ``box_vs_morning`` x the morning
    range; after 13:00, a close beyond it on a bar ``expansion_mult`` x the box's mean bar range -> buy that way."""

    code = "LUNCH"
    deviations = ("VOLUME: no volume confirmation (index bars only); bar-range expansion is the confirmation.",)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        if s.end <= s.at(13, 0):
            return None
        box = s.window(s.at(12, 0), s.at(13, 0))
        morning = s.window(s.at(9, 15), s.at(12, 0))
        if len(box) < 55 or not morning:
            return None
        hi, lo = s.hi_lo(box)
        mhi, mlo = s.hi_lo(morning)
        if (hi - lo) / s.last.close * _HUNDRED > p["box_max_pct"] or hi - lo > p["box_vs_morning"] * (mhi - mlo):
            return None
        b = s.last
        if b.high - b.low < p["expansion_mult"] * _mean_range(box):
            return None
        facts = {"box_high": hi, "box_low": lo}
        if b.close > hi:
            return EntrySignal((CE,), "LUNCH_BOX_BREAK_UP", facts)
        if b.close < lo:
            return EntrySignal((PE,), "LUNCH_BOX_BREAK_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f, c = t.signal.facts, s.last.close
        back = c < f["box_high"] if t.signal.rights[0] is CE else c > f["box_low"]
        return "INVALIDATED_BACK_IN_BOX" if back else None


PLUGINS: dict[str, type[BasePlugin]] = {
    "S-FBO-001": FailedBreakout,
    "S-VWAPC-001": VwapContinuation,
    "S-VWAPMR-001": VwapMeanReversion,
    "S-VOLX-001": CompressionBreakout,
    "S-IVRV-001": CheapGamma,
    "S-EXP0-001": ExpiryAfternoonMomentum,
    "S-GAPGO-001": GapAndGo,
    "S-GAPFADE-001": GapFade,
    "S-VIXSTR-001": VixSpikeStraddle,
    "S-EVTBO-001": EventBreakout,
    "S-LUNCH-001": LunchCompressionBreakout,
    "S-DAYVOL-001": DayVolatility,
    "S-NOISE-001": NoiseAreaBreakout,
    "S-IMOM-001": IntradayMomentum,
    "S-GEXMO-001": GammaMomentum,
}
PLUGINS.update(VOL_PLUGINS)  # H32-H34 (strategies/library/signals_vol.py)
PLUGINS.update(TT_PLUGINS)  # H37-H40 top-traders candidates (strategies/library/signals_tt.py)

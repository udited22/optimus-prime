"""Deterministic intraday regime classifier (backlog K-11). One label per closed bar, plus a session summary.

Dimensions (exactly one value each):
* **trend** UP / DOWN / RANGE: a vote of three voters (Wilder ADX with +DI/-DI, the least-squares slope in ATR
  units, and the distance from the session VWAP in sigma units). Majority of the non-abstaining voters.
* **volatility** COMPRESSION / NORMAL / EXPANSION: a vote of realised volatility, India VIX (level, and a jump over
  ``vix_change_bars`` that votes EXPANSION outright) and the rolling range as a percentile of recent rolling ranges.
* **opening character** OPENING_DRIVE / MEAN_REVERSION / UNDETERMINED: decided once, ``decide_at_minutes`` after
  the open, from where the price closes inside the opening range and how often it crossed the opening price.

Conditions: the gap at the open (vs the previous close), the weekly-expiry day (from the trading calendar), a
scheduled event day (from the event calendar), and ABNORMAL_MARKET (sticky for the session).

Hysteresis, so labels do not flip-flop: every voter has an enter and a (looser) exit threshold relative to the label
currently adopted, and a new trend or volatility label must win the vote on ``confirm_bars`` consecutive bars.

``classifier_agreement`` is ONLY the share of individual voters that agree with the adopted trend and volatility
labels. It is not a probability, not a forecast and not a confidence that any trade will work.

The classifier is UNVALIDATED (docs/research/validation.md §13.5): ``status`` travels on every label, and the kernel's
regime gate
turns an unvalidated label into NO_EDGE wherever money is at risk.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any

from project100c.calendar import ExpiryCalendar
from project100c.errors import ConfigError
from project100c.market_types import Bar
from project100c.regime.config import EventCalendar, RegimeConfig
from project100c.regime.features import atr, ls_slope, percentile_rank, realised_vol_pct, stdev, wilder_adx
from project100c.sessions.model import IST
from project100c.spec.models import Regime

SESSION_OPEN = time(9, 15)
_HISTORY_SESSIONS = 5  # rolling-range percentiles use up to 5 sessions of history (cross-day)


class Trend(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    RANGE = "RANGE"


class VolState(StrEnum):
    COMPRESSION = "COMPRESSION"
    NORMAL = "NORMAL"
    EXPANSION = "EXPANSION"


class GapType(StrEnum):
    GAP_UP_LARGE = "GAP_UP_LARGE"
    GAP_UP = "GAP_UP"
    FLAT = "FLAT"
    GAP_DOWN = "GAP_DOWN"
    GAP_DOWN_LARGE = "GAP_DOWN_LARGE"
    UNKNOWN = "UNKNOWN"  # no previous close


class OpenCharacter(StrEnum):
    OPENING_DRIVE = "OPENING_DRIVE"
    MEAN_REVERSION = "MEAN_REVERSION"
    UNDETERMINED = "UNDETERMINED"


_TREND_TAG = {Trend.UP: Regime.TRENDING_UP, Trend.DOWN: Regime.TRENDING_DOWN, Trend.RANGE: Regime.MEAN_REVERTING}
_VOL_TAG = {
    VolState.COMPRESSION: Regime.VOLATILITY_COMPRESSION,
    VolState.NORMAL: Regime.VOLATILITY_NORMAL,
    VolState.EXPANSION: Regime.VOLATILITY_EXPANSION,
}
_OPEN_TAG = {OpenCharacter.OPENING_DRIVE: Regime.OPENING_DRIVE, OpenCharacter.MEAN_REVERSION: Regime.OPENING_REVERSION}


@dataclass(frozen=True, slots=True)
class RegimeLabel:
    """The classifier's output for one closed bar (``ts`` = the bar's END, when the label becomes usable)."""

    ts: datetime
    version: str
    status: str  # UNVALIDATED | VALIDATED (config status)
    warmup: bool
    trend: Trend
    volatility: VolState
    gap: GapType
    gap_pct: Decimal | None
    opening: OpenCharacter
    expiry_day: bool
    event_day: bool
    events: tuple[str, ...]
    abnormal: bool
    abnormal_reason: str
    classifier_agreement: Decimal  # share of voters agreeing with the adopted labels (NOT a confidence)
    trend_votes: tuple[tuple[str, str], ...]
    vol_votes: tuple[tuple[str, str], ...]
    measures: tuple[tuple[str, Decimal | None], ...]

    @property
    def validated(self) -> bool:
        return self.status == "VALIDATED"

    def tags(self) -> frozenset[Regime]:
        """The spec-level regime tags. During warm-up the only tag is NO_EDGE (plus any sticky conditions)."""
        out: set[Regime] = set()
        if self.warmup:
            out.add(Regime.NO_EDGE)
        else:
            out.add(_TREND_TAG[self.trend])
            out.add(_VOL_TAG[self.volatility])
            if self.opening in _OPEN_TAG:
                out.add(_OPEN_TAG[self.opening])
        if self.gap not in (GapType.FLAT, GapType.UNKNOWN):
            out.add(Regime.GAP_REGIME)
        if self.expiry_day:
            out.add(Regime.EXPIRY_REGIME)
        if self.event_day:
            out.add(Regime.EVENT_REGIME)
        if self.abnormal:
            out.add(Regime.ABNORMAL_MARKET)
        return frozenset(out)

    def as_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "classifier": self.version,
            "status": self.status,
            "warmup": self.warmup,
            "trend": self.trend.value,
            "volatility": self.volatility.value,
            "gap": self.gap.value,
            "gap_pct": None if self.gap_pct is None else str(self.gap_pct),
            "opening": self.opening.value,
            "expiry_day": self.expiry_day,
            "event_day": self.event_day,
            "events": list(self.events),
            "abnormal": self.abnormal,
            "abnormal_reason": self.abnormal_reason,
            "classifier_agreement": str(self.classifier_agreement),
            "classifier_agreement_note": "share of the classifier's voters agreeing with its labels; not a confidence",
            "tags": sorted(t.value for t in self.tags()),
            "trend_votes": dict(self.trend_votes),
            "vol_votes": dict(self.vol_votes),
            "measures": {k: (None if v is None else str(v)) for k, v in self.measures},
        }


@dataclass(frozen=True, slots=True)
class SessionRegime:
    day: date
    version: str
    status: str
    bars: int
    trend: Trend  # the trend label held for the most bars after warm-up
    trend_share: dict[str, Decimal]
    volatility: VolState  # likewise
    gap: GapType
    gap_pct: Decimal | None
    opening: OpenCharacter
    expiry_day: bool
    event_day: bool
    abnormal: bool
    trend_flips: int
    vol_flips: int


def _d(x: float | None, q: str = "0.0001") -> Decimal | None:
    return None if x is None else Decimal(repr(x)).quantize(Decimal(q))


def _majority(votes: list[str | None], keep: str) -> str:
    """The vote won by more than half of the non-abstaining voters; otherwise the label currently adopted."""
    cast = [v for v in votes if v is not None]
    if not cast:
        return keep
    for v in sorted(set(cast)):
        if cast.count(v) * 2 > len(cast):
            return v
    return keep


@dataclass(slots=True)
class _Session:
    day: date
    prev_close: Decimal | None
    gap: GapType = GapType.UNKNOWN
    gap_pct: Decimal | None = None
    open_px: float | None = None
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    vix: list[float | None] = field(default_factory=list)
    pv: float = 0.0
    vol_sum: float = 0.0
    tp_sum: float = 0.0
    vwap_dev: list[float] = field(default_factory=list)
    vwap_basis: str = "twap_proxy"
    crosses: int = 0
    side: int = 0
    opening: OpenCharacter = OpenCharacter.UNDETERMINED
    opening_decided: bool = False
    abnormal: bool = False
    abnormal_reason: str = ""
    trend: Trend | None = None
    vol: VolState | None = None
    trend_cand: tuple[str, int] = ("", 0)
    vol_cand: tuple[str, int] = ("", 0)
    labels: list[RegimeLabel] = field(default_factory=list)
    last_start: datetime | None = None


class RegimeClassifier:
    """Streaming classifier: ``start_session(day, prev_close)`` then ``on_bar(bar, vix=..., volume=...)`` per
    closed 1-minute index bar, in time order. Deterministic for the same inputs and config."""

    def __init__(
        self,
        config: RegimeConfig,
        *,
        expiries: ExpiryCalendar | None = None,
        events: EventCalendar | None = None,
    ) -> None:
        self.cfg = config
        self._expiries = expiries
        self._events = events
        self._range_hist: deque[float] = deque(maxlen=_HISTORY_SESSIONS * 375)
        self._s: _Session | None = None

    @property
    def version(self) -> str:
        return self.cfg.version

    # ------------------------------------------------------------------ session
    def start_session(self, day: date, prev_close: Decimal | None) -> None:
        if self._s is not None:
            self._archive()
        self._s = _Session(day, prev_close)

    def _archive(self) -> None:
        assert self._s is not None
        n = self.cfg.volatility.range_bars
        h, lo = self._s.highs, self._s.lows
        for i in range(n, len(h) + 1):
            self._range_hist.append(max(h[i - n : i]) - min(lo[i - n : i]))

    @property
    def labels(self) -> tuple[RegimeLabel, ...]:
        return () if self._s is None else tuple(self._s.labels)

    @property
    def last(self) -> RegimeLabel | None:
        return None if self._s is None or not self._s.labels else self._s.labels[-1]

    # ------------------------------------------------------------------ per bar
    def on_bar(self, bar: Bar, *, vix: Decimal | None = None, volume: int | None = None) -> RegimeLabel:
        s = self._s
        if s is None:
            raise ConfigError("start_session() must be called before on_bar()")
        st = bar.start.astimezone(IST)
        if st.date() != s.day:
            raise ConfigError(f"bar {st.isoformat()} is not in the session {s.day}")
        if s.last_start is not None and bar.start <= s.last_start:
            raise ConfigError("bars must arrive in strictly increasing time order")
        s.last_start = bar.start
        hi, lo, cl, op = float(bar.high), float(bar.low), float(bar.close), float(bar.open)
        a = self.cfg.abnormal
        # the gap (first bar of the session)
        if s.open_px is None:
            s.open_px = op
            if s.prev_close is not None and s.prev_close > 0:
                g = (bar.open / s.prev_close - 1) * 100
                s.gap_pct = g.quantize(Decimal("0.001"))
                ag = abs(g)
                gc = self.cfg.gap
                if ag < gc.flat_pct:
                    s.gap = GapType.FLAT
                elif g > 0:
                    s.gap = GapType.GAP_UP_LARGE if ag >= gc.large_pct else GapType.GAP_UP
                else:
                    s.gap = GapType.GAP_DOWN_LARGE if ag >= gc.large_pct else GapType.GAP_DOWN
        # the one-bar move of the first bar is measured from its own open: an opening gap is not a one-minute move
        # (the gap is classified above, and |close / previous close - 1| >= index_move_pct still trips ABNORMAL)
        prev = s.closes[-1] if s.closes else op
        s.highs.append(hi)
        s.lows.append(lo)
        s.closes.append(cl)
        s.vix.append(None if vix is None else float(vix))
        # VWAP: volume-weighted when the input has volume (futures), else an equal-weight typical-price proxy
        tp = (hi + lo + cl) / 3
        if volume:
            s.pv += tp * volume
            s.vol_sum += volume
            s.vwap_basis = "volume"
        s.tp_sum += tp
        vwap = s.pv / s.vol_sum if s.vol_sum > 0 else s.tp_sum / len(s.closes)
        s.vwap_dev.append(cl - vwap)
        # opening-price crosses (for the opening character)
        side = 1 if cl > s.open_px else -1 if cl < s.open_px else 0
        if side and s.side and side != s.side:
            s.crosses += 1
        if side:
            s.side = side
        # abnormal (sticky)
        if not s.abnormal:
            reason = ""
            if (
                s.prev_close is not None
                and s.prev_close > 0
                and abs(bar.close / s.prev_close - 1) * 100 >= a.index_move_pct
            ):
                reason = f"index {((bar.close / s.prev_close - 1) * 100):+.2f}% vs previous close"
            elif prev > 0 and abs(cl / prev - 1) * 100 >= float(a.one_bar_move_pct):
                reason = f"one-minute move {(cl / prev - 1) * 100:+.2f}%"
            elif vix is not None and vix >= a.vix_level:
                reason = f"India VIX {vix} >= {a.vix_level}"
            if reason:
                s.abnormal, s.abnormal_reason = True, reason
        end = bar.start + timedelta(minutes=self.cfg.bar_minutes)
        self._decide_opening(s, end)
        warm = len(s.closes) < self.cfg.warmup_bars
        tv, t_meas = self._trend_votes(s)
        vv, v_meas = self._vol_votes(s)
        if not warm:
            s.trend = Trend(self._adopt(s, "trend", [v for _, v in tv], self.cfg.trend.confirm_bars))
            s.vol = VolState(self._adopt(s, "vol", [v for _, v in vv], self.cfg.volatility.confirm_bars))
        trend = s.trend or Trend.RANGE
        vol = s.vol or VolState.NORMAL
        cast = [v for _, v in tv if v is not None] + [v for _, v in vv if v is not None]
        agree = sum(1 for _, v in tv if v == trend.value) + sum(1 for _, v in vv if v == vol.value)
        agreement = Decimal(0) if warm or not cast else (Decimal(agree) / Decimal(len(cast))).quantize(Decimal("0.01"))
        evs = self._events.on(s.day) if self._events is not None else ()
        lab = RegimeLabel(
            ts=end,
            version=self.cfg.version,
            status=self.cfg.status,
            warmup=warm,
            trend=trend,
            volatility=vol,
            gap=s.gap,
            gap_pct=s.gap_pct,
            opening=s.opening,
            expiry_day=self._expiries.is_expiry_day(s.day) if self._expiries is not None else False,
            event_day=bool(evs),
            events=tuple(e.kind for e in evs),
            abnormal=s.abnormal,
            abnormal_reason=s.abnormal_reason,
            classifier_agreement=agreement,
            trend_votes=tuple((k, v or "ABSTAIN") for k, v in tv),
            vol_votes=tuple((k, v or "ABSTAIN") for k, v in vv),
            measures=(
                *t_meas,
                *v_meas,
                ("vwap", _d(vwap, "0.01")),
                ("vwap_basis_volume", Decimal(1 if s.vwap_basis == "volume" else 0)),
            ),
        )
        s.labels.append(lab)
        return lab

    def _adopt(self, s: _Session, which: str, votes: list[str | None], confirm: int) -> str:
        cur = s.trend if which == "trend" else s.vol
        default = Trend.RANGE.value if which == "trend" else VolState.NORMAL.value
        keep = cur.value if cur is not None else default
        cand = _majority(votes, keep)
        if cur is None:  # first label after warm-up: adopt the vote directly
            return cand
        if cand == cur.value:
            new: tuple[str, int] = ("", 0)
            result = cur.value
        else:
            prev_c, n = s.trend_cand if which == "trend" else s.vol_cand
            n = n + 1 if prev_c == cand else 1
            new = (cand, n)
            result = cand if n >= confirm else cur.value
            if n >= confirm:
                new = ("", 0)
        if which == "trend":
            s.trend_cand = new
        else:
            s.vol_cand = new
        return result

    # ------------------------------------------------------------------ voters
    def _trend_votes(self, s: _Session) -> tuple[list[tuple[str, str | None]], list[tuple[str, Decimal | None]]]:
        c = self.cfg.trend
        cur = s.trend
        votes: list[tuple[str, str | None]] = []
        res = wilder_adx(s.highs, s.lows, s.closes, c.adx_period)
        adx_v: str | None = None
        if res is not None:
            adx, pdi, mdi = res
            direction = Trend.UP if pdi >= mdi else Trend.DOWN
            thr = float(c.adx_exit) if cur is direction else float(c.adx_enter)
            adx_v = direction.value if adx >= thr else Trend.RANGE.value
        votes.append(("adx", adx_v))
        slope_v: str | None = None
        slope_atr: float | None = None
        a = atr(s.highs, s.lows, s.closes, min(c.slope_bars, len(s.closes) - 1)) if len(s.closes) > 2 else None
        if len(s.closes) >= c.slope_bars and a:
            slope_atr = ls_slope(s.closes[-c.slope_bars :]) / a
            direction = Trend.UP if slope_atr > 0 else Trend.DOWN
            thr = float(c.slope_exit_atr) if cur is direction else float(c.slope_enter_atr)
            slope_v = direction.value if abs(slope_atr) >= thr else Trend.RANGE.value
        votes.append(("slope", slope_v))
        vw_v: str | None = None
        z: float | None = None
        if len(s.vwap_dev) >= 10:
            sd = stdev(s.vwap_dev)
            if sd > 0:
                z = s.vwap_dev[-1] / sd
                direction = Trend.UP if z > 0 else Trend.DOWN
                thr = float(c.vwap_exit_sigma) if cur is direction else float(c.vwap_enter_sigma)
                vw_v = direction.value if abs(z) >= thr else Trend.RANGE.value
        votes.append(("vwap", vw_v))
        meas = [
            ("adx", _d(res[0] if res else None, "0.01")),
            ("plus_di", _d(res[1] if res else None, "0.01")),
            ("minus_di", _d(res[2] if res else None, "0.01")),
            ("slope_atr_per_bar", _d(slope_atr)),
            ("vwap_sigma", _d(z, "0.01")),
        ]
        return votes, meas

    def _vol_votes(self, s: _Session) -> tuple[list[tuple[str, str | None]], list[tuple[str, Decimal | None]]]:
        c = self.cfg.volatility
        cur = s.vol
        band = float(c.band_pct) / 100

        def banded(x: float, lo_thr: float, hi_thr: float) -> str:
            lo_t = lo_thr * (1 + band) if cur is VolState.COMPRESSION else lo_thr
            hi_t = hi_thr * (1 - band) if cur is VolState.EXPANSION else hi_thr
            if x <= lo_t:
                return VolState.COMPRESSION.value
            if x >= hi_t:
                return VolState.EXPANSION.value
            return VolState.NORMAL.value

        votes: list[tuple[str, str | None]] = []
        closes = s.closes
        if s.prev_close is not None and len(closes) < c.rv_bars + 1:
            closes = [float(s.prev_close), *closes]
        rv = realised_vol_pct(closes, c.rv_bars)
        votes.append(
            ("realised_vol", None if rv is None else banded(rv, float(c.rv_compression), float(c.rv_expansion)))
        )
        vix_now = s.vix[-1]
        vix_v: str | None = None
        jump: float | None = None
        if vix_now is not None:
            vix_v = banded(vix_now, float(c.vix_compression), float(c.vix_expansion))
            if len(s.vix) > c.vix_change_bars:
                ago = s.vix[-1 - c.vix_change_bars]
                if ago:
                    jump = (vix_now / ago - 1) * 100
                    if jump >= float(c.vix_jump_pct):
                        vix_v = VolState.EXPANSION.value
        votes.append(("india_vix", vix_v))
        rng_v: str | None = None
        pct: float | None = None
        n = c.range_bars
        if len(s.highs) >= n:
            now_r = max(s.highs[-n:]) - min(s.lows[-n:])
            hist = list(self._range_hist) + [
                max(s.highs[i - n : i]) - min(s.lows[i - n : i]) for i in range(n, len(s.highs))
            ]
            if len(hist) >= c.range_min_history:
                pct = percentile_rank(hist, now_r)
                rng_v = banded(pct, float(c.range_pct_compression), float(c.range_pct_expansion))
        votes.append(("range_percentile", rng_v))
        meas = [
            ("realised_vol_pct", _d(rv, "0.01")),
            ("india_vix", _d(vix_now, "0.01")),
            ("vix_change_pct", _d(jump, "0.01")),
            ("range_percentile", _d(pct, "0.1")),
        ]
        return votes, meas

    def _decide_opening(self, s: _Session, end: datetime) -> None:
        if s.opening_decided or s.open_px is None:
            return
        o = self.cfg.opening
        decide = datetime.combine(s.day, SESSION_OPEN, IST) + timedelta(minutes=o.decide_at_minutes)
        if end < decide:
            return
        s.opening_decided = True
        hi, lo, cl = max(s.highs), min(s.lows), s.closes[-1]
        if hi <= lo:
            return
        frac = (cl - lo) / (hi - lo)
        up = cl > s.open_px
        down = cl < s.open_px
        dfrac = float(o.drive_close_frac)
        gap_faded = (
            s.gap_pct is not None
            and s.prev_close is not None
            and s.gap not in (GapType.FLAT, GapType.UNKNOWN)
            and abs(cl - float(s.prev_close)) <= 0.5 * abs(s.open_px - float(s.prev_close))
        )
        if s.crosses <= o.drive_max_open_crosses and ((up and frac >= dfrac) or (down and frac <= 1 - dfrac)):
            s.opening = OpenCharacter.OPENING_DRIVE
        elif s.crosses >= o.reversion_min_crosses or abs(frac - 0.5) <= float(o.reversion_mid_frac) / 2 or gap_faded:
            s.opening = OpenCharacter.MEAN_REVERSION

    # ------------------------------------------------------------------ summary
    def session_summary(self) -> SessionRegime:
        s = self._s
        if s is None or not s.labels:
            raise ConfigError("no bars classified in this session")
        live = [x for x in s.labels if not x.warmup]
        share: dict[str, Decimal] = {}
        for t in Trend:
            n = sum(1 for x in live if x.trend is t)
            share[t.value] = (Decimal(n) / Decimal(len(live))).quantize(Decimal("0.01")) if live else Decimal(0)
        trend = max(Trend, key=lambda t: (share[t.value], t is Trend.RANGE))
        vcount = {v: sum(1 for x in live if x.volatility is v) for v in VolState}
        vol = max(VolState, key=lambda v: (vcount[v], v is VolState.NORMAL))
        tf = sum(1 for a, b in pairwise(live) if a.trend is not b.trend)
        vf = sum(1 for a, b in pairwise(live) if a.volatility is not b.volatility)
        last = s.labels[-1]
        return SessionRegime(
            s.day, self.cfg.version, self.cfg.status, len(s.labels), trend, share, vol, s.gap, s.gap_pct, s.opening,
            last.expiry_day, last.event_day, s.abnormal, tf, vf,
        )  # fmt: skip


def classify_session(
    clf: RegimeClassifier,
    day: date,
    bars: list[Bar],
    *,
    prev_close: Decimal | None,
    vix: dict[datetime, Decimal] | None = None,
) -> list[RegimeLabel]:
    """Convenience: classify one session of 1-minute index bars (``vix`` keyed by bar start)."""
    clf.start_session(day, prev_close)
    return [clf.on_bar(b, vix=(vix or {}).get(b.start)) for b in bars]

"""Plug-ins for the intelligence-layer specs H19-H22 (docs/research/institutional-intelligence-layer.md).

Same contract as ``signals.py``: closed bars only, at most one ``EntrySignal`` a bar, direction by which option to
buy. These four need more than one session of history, so each plug-in keeps a small per-day record of the sessions
it has seen (``_History``); the record of a day is written when the next day's first bar arrives, so a decision only
ever uses completed earlier sessions plus today's closed bars. H19 and H22 also read the option chain of the current
minute (``Session.option_bar``) or a point-in-time daily feature (``Session.features``).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from datetime import time, timedelta
from decimal import Decimal
from itertools import pairwise

from project100c.core_types import OptionRight
from project100c.market_types import Bar
from project100c.sessions.model import IST
from project100c.strategies.library.base import BasePlugin, EntrySignal, OpenTrade, Session
from project100c.strategies.library.optmath import implied_vol, years_to_expiry

CE, PE = OptionRight.CE, OptionRight.PE
OPEN = time(9, 15)


def _t(s: Session) -> time:
    return s.end.astimezone(IST).time()


def _mins(t: time) -> int:
    return (t.hour * 60 + t.minute) - (OPEN.hour * 60 + OPEN.minute)


def _end_t(b: Bar) -> time:
    return (b.start + timedelta(minutes=1)).astimezone(IST).time()


def rv5(bars: list[Bar], start_px: Decimal | None = None) -> float:
    """Realised variance from 5-minute log returns: closes of the bars ending on a 5-minute mark, starting from
    ``start_px`` (default: the first bar's open). Not annualised (a one-session variance)."""
    if not bars:
        return 0.0
    px = [float(start_px if start_px is not None else bars[0].open)]
    px += [float(b.close) for b in bars if _end_t(b).minute % 5 == 0]
    return sum(math.log(b / a) ** 2 for a, b in pairwise(px) if a > 0 and b > 0)


def _q(x: float, places: str = "0.000001") -> Decimal:
    return Decimal(repr(x)).quantize(Decimal(places))


class _History:
    """Per-day summaries of completed sessions, newest last (bounded)."""

    def __init__(self, keep: int = 80) -> None:
        self.days: deque[dict[str, object]] = deque(maxlen=keep)
        self._cur: Session | None = None

    def see(self, s: Session, summarise: object) -> None:
        if self._cur is not None and self._cur.day != s.day and self._cur.idx:
            assert callable(summarise)
            self.days.append(summarise(self._cur))
        self._cur = s


# ---------------------------------------------------------------------------------------------- H19
class DayVolatility(BasePlugin):
    """H19 S-DAYVOL-001: when the forecast of today's open-to-close realised variance is at least ``theta_ratio`` x
    the one-session variance implied by the nearest weekly ATM IV, and the short end is not strongly inverted, buy the
    ATM CE and the ATM PE (a long straddle: OD-013 allows its 2 lots)."""

    code = "H19"
    deviations = (
        "HAR: fixed weights, no fitted regression: forecast = 1/2 x (RV_yesterday + RV_5d + RV_22d) / 3 + 1/2 x "
        "today's 09:15-09:45 RV scaled by the trailing ratio of full-session to first-30-minute RV (22 sessions).",
        "IV: own Black-Scholes inversion of the ATM CE and PE closes (rate 6.5%), averaged; not the vendor's IV field.",
        "TERM: term = per-day variance of code-1 ATM IV / per-day forward variance code 1 -> code 2 - 1 (a relative "
        "inversion); a non-positive forward variance blocks the entry.",
        "STOP: a premium stop per leg (library base), not one on the combined premium.",
    )

    def __init__(self) -> None:
        self.h = _History()

    @staticmethod
    def _summary(s: Session) -> dict[str, object]:
        morn = [b for b in s.idx if _end_t(b) <= time(9, 45)]
        return {"day": s.day, "rv_oc": rv5(s.idx), "rv_morn": rv5(morn)}

    def _forecast(self, s: Session, n: int) -> float | None:
        hist = list(self.h.days)[-n:]
        if len(hist) < n:
            return None
        oc = [float(str(d["rv_oc"])) for d in hist]
        mo = [float(str(d["rv_morn"])) for d in hist]
        if sum(mo) <= 0:
            return None
        har = (oc[-1] + sum(oc[-5:]) / 5 + sum(oc) / n) / 3
        morn_today = rv5([b for b in s.idx if _end_t(b) <= time(9, 45)])
        return 0.5 * har + 0.5 * morn_today * (sum(oc) / sum(mo))

    def _atm_iv(self, s: Session, nth: int) -> tuple[float, float, Decimal] | None:
        exp = s.expiry(1, nth)
        if exp is None:
            return None
        spot = s.last.close
        ce, pe = s.near(exp, CE, spot), s.near(exp, PE, spot)
        if not ce or not pe or ce[0].strike != pe[0].strike:
            return None
        t = years_to_expiry(s.end, exp)
        ivs = []
        for c in (ce[0], pe[0]):
            b = s.option_bar(c.instrument_key)
            if b is None:
                return None
            iv = implied_vol(float(b.close), float(spot), float(c.strike), t, c.right is CE)
            if iv is None:
                return None
            ivs.append(iv)
        return sum(ivs) / 2, t, ce[0].strike

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        self.h.see(s, self._summary)
        now = _t(s)
        if not (time(9, 45) <= now <= time(11, 0)) or s.state.get("done"):
            return None
        fc = self._forecast(s, int(p["har_lookback_days"]))
        one = self._atm_iv(s, 0)
        if fc is None or one is None:
            return None
        iv1, t1, strike = one
        ratio = fc / (iv1 * iv1 / 252)
        if ratio < float(p["theta_ratio"]):
            return None
        two = self._atm_iv(s, 1)
        if two is None:
            return None
        iv2, t2, _ = two
        fwd = (iv2 * iv2 * t2 - iv1 * iv1 * t1) / (t2 - t1) if t2 > t1 else 0.0
        term = (iv1 * iv1) / fwd - 1 if fwd > 0 else math.inf
        if term > float(p["term_max"]):
            return None
        s.state["done"] = True
        facts = {"forecast_var": _q(fc, "0.000000001"), "iv_atm": _q(iv1, "0.0001"), "ratio": _q(ratio, "0.001"),
                 "term": _q(term, "0.001"), "strike": strike}  # fmt: skip
        return EntrySignal((CE, PE), "INTRADAY_VOL_CHEAP", facts)

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        if _t(s) < time(12, 30) or s.state.get("rv_checked"):
            return None
        s.state["rv_checked"] = True  # the thesis check runs once, on the first bar at or after 12:30
        since = s.window(t.signal_at, s.end)
        realised = rv5(since, t.spot_at_signal)
        elapsed = (s.end - t.signal_at).total_seconds() / 60
        pro_rata = float(t.signal.facts["forecast_var"]) * elapsed / 375
        return "INVALIDATED_LOW_REALISED_VOL" if realised < 0.5 * pro_rata else None


# ---------------------------------------------------------------------------------------------- H20
class NoiseAreaBreakout(BasePlugin):
    """H20 S-NOISE-001: a check-minute close outside the time-of-day noise area -> buy the option in the breakout
    direction; a later check-minute close back inside the area closes it (the band is the trailing stop)."""

    code = "H20"
    deviations = ("CHECKS: on the closes ending every check_minutes from 10:00 (missing bars: that check is skipped).",)

    def __init__(self) -> None:
        self.h = _History()

    @staticmethod
    def _summary(s: Session) -> dict[str, object]:
        o = s.open
        return {"day": s.day, "moves": {_mins(_end_t(b)): abs(float(b.close / o) - 1) for b in s.idx}}

    def _band(self, s: Session, p: Mapping[str, Decimal]) -> tuple[Decimal, Decimal] | None:
        n = int(p["lookback_sessions"])
        m = _mins(_t(s))
        vals = [mv[m] for d in list(self.h.days)[-n:] if isinstance(mv := d["moves"], dict) and m in mv]
        if len(vals) < n or s.prev_close is None:
            return None
        sig = Decimal(repr(sum(vals) / n)) * p["band_mult"]
        o, pc = s.open, s.prev_close
        return max(o, pc) * (1 + sig), min(o, pc) * (1 - sig)

    @staticmethod
    def _check(s: Session, p: Mapping[str, Decimal]) -> bool:
        now = _t(s)
        return now >= time(10, 0) and (_mins(now) - _mins(time(10, 0))) % int(p["check_minutes"]) == 0

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        self.h.see(s, self._summary)
        if not self._check(s, p) or _t(s) > time(13, 30):
            return None
        band = self._band(s, p)
        if band is None:
            return None
        up, lo = band
        c = s.last.close
        facts = {"upper": up.quantize(Decimal("0.01")), "lower": lo.quantize(Decimal("0.01")), "close": c}
        if c > up:
            return EntrySignal((CE,), "NOISE_BREAK_UP", facts)
        if c < lo:
            return EntrySignal((PE,), "NOISE_BREAK_DOWN", facts)
        return None

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        if not self._check(s, p):
            return None
        band = self._band(s, p)
        if band is None:
            return None
        up, lo = band
        c = s.last.close
        back = c <= up if t.signal.rights[0] is CE else c >= lo
        return "INVALIDATED_BACK_IN_NOISE" if back else None


# ---------------------------------------------------------------------------------------------- H21
class IntradayMomentum(BasePlugin):
    """H21 S-IMOM-001: previous-close-to-13:30 return of at least ``rod_min_pct`` on a day whose 09:15-13:30 realised
    vol is at or above the ``rv_pct_min`` percentile of its last 20 sessions -> buy in the direction of that return
    (signalled from 13:30, re-sent each minute to 13:50 while no trade is open)."""

    code = "H21"
    deviations = (
        "RV PERCENTILE: share of the last 20 sessions whose 09:15-13:30 RV is <= today's (5-minute returns).",
        "TARGET: the 1.5 R target is on the option premium (library base).",
    )
    SIGNAL_AT = time(13, 30)

    def __init__(self) -> None:
        self.h = _History()

    @classmethod
    def _summary(cls, s: Session) -> dict[str, object]:
        return {"day": s.day, "rv_rod": rv5([b for b in s.idx if _end_t(b) <= cls.SIGNAL_AT])}

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        self.h.see(s, self._summary)
        now = _t(s)
        if now == self.SIGNAL_AT and s.prev_close is not None:
            hist = [float(str(d["rv_rod"])) for d in list(self.h.days)[-20:]]
            if len(hist) == 20:
                rv = rv5(s.idx)
                pct = 100 * sum(1 for h in hist if h <= rv) / 20
                r = float(s.last.close / s.prev_close - 1) * 100
                if abs(r) >= float(p["rod_min_pct"]) and pct >= float(p["rv_pct_min"]):
                    s.state["imom"] = {"right": CE if r > 0 else PE, "c1330": s.last.close, "r_rod_pct": _q(r, "0.001"),
                                       "rv_pct": _q(pct, "0.1"), "prev_close": s.prev_close}  # fmt: skip
        st = s.state.get("imom")
        if st is None or not (self.SIGNAL_AT <= now <= time(13, 50)):
            return None
        right = st["right"]
        return EntrySignal((right,), "ROD_MOMENTUM_UP" if right is CE else "ROD_MOMENTUM_DOWN",
                           {k: v for k, v in st.items() if k != "right"})  # fmt: skip

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        pc, c1330, c = f["prev_close"], f["c1330"], s.last.close
        half = pc + (c1330 - pc) / 2
        up = t.signal.rights[0] is CE
        if (up and c < half) or (not up and c > half):
            return "INVALIDATED_GAVE_BACK_HALF"
        if _t(s) >= time(14, 15):
            since = s.window(t.signal_at, s.end)
            further = any(b.high > c1330 for b in since) if up else any(b.low < c1330 for b in since)
            if not further:
                return "INVALIDATED_NO_FOLLOW_THROUGH"
        return None


# ---------------------------------------------------------------------------------------------- H22
class GammaMomentum(BasePlugin):
    """H22 S-GEXMO-001: on a day whose 10:15 partial-chain gamma concentration ranks at or above ``g_pct_min`` of its
    last ``g_lookback`` sessions (feature ``gex_rank``, point in time), the first 15-minute close beyond the
    09:15-10:15 range -> buy in the break direction."""

    code = "H22"
    deviations = (
        "G: pre-computed per day from the 10:14 option bars (OI x own-IV gamma, code-1 ATM+-10 and code-2 ATM+-3) and "
        "ranked against the previous g_lookback sessions; supplied as the point-in-time feature gex_rank.",
        "TRAILING: the '15-min swing' trailing exit is not built; the 1.5 R target, the invalidations, the time exit "
        "and the maximum hold apply.",
    )
    RANGE_END = time(10, 15)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        now = _t(s)
        if now <= self.RANGE_END or now > time(13, 0) or (_mins(now) % 15) != 0 or "gex_first" in s.state:
            return None
        rng = [b for b in s.idx if _end_t(b) <= self.RANGE_END]
        if len(rng) < 55:  # a range needs (almost) all of its 60 minutes
            return None
        hi, lo = s.hi_lo(rng)
        c = s.last.close
        if lo <= c <= hi:
            return None
        s.state["gex_first"] = "UP" if c > hi else "DOWN"  # only the first close beyond the range counts
        rank = s.features.get("gex_rank")
        if rank is None or rank < p["g_pct_min"]:
            return None
        facts = {"range_high": hi, "range_low": lo, "gex_rank": rank}
        return EntrySignal((CE,), "GAMMA_BREAK_UP", facts) if c > hi else EntrySignal((PE,), "GAMMA_BREAK_DOWN", facts)

    def check_exit(self, s: Session, p: Mapping[str, Decimal], t: OpenTrade) -> str | None:
        f = t.signal.facts
        hi, lo = f["range_high"], f["range_low"]
        up = t.signal.rights[0] is CE
        now = _t(s)
        if _mins(now) % 15 == 0 and lo <= s.last.close <= hi:
            return "INVALIDATED_BACK_IN_RANGE"
        if s.end >= t.signal_at + timedelta(minutes=60) and not s.state.get("followed"):
            need = p["follow_fraction"] * (hi - lo)
            since = s.window(t.signal_at, s.end)
            ok = any(b.high >= hi + need for b in since) if up else any(b.low <= lo - need for b in since)
            if not ok:
                return "INVALIDATED_NO_FOLLOW_THROUGH"
            s.state["followed"] = True
        return None

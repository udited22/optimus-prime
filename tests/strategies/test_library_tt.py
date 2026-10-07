"""H37-H40 top-traders plug-ins (signals_tt.py): signals from closed bars and completed-session features only."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from project100c.core_types import OptionRight
from project100c.market_types import Bar
from project100c.sessions.model import IST
from project100c.spec.io import load_spec_file
from project100c.strategies.library import PLUGINS
from project100c.strategies.library.base import EntrySignal, OpenTrade, Session
from project100c.strategies.library.signals_tt import NarrowRange7, OopsGap, PullbackTrend, TrendDay
from tests.data.dhan_fakes import REPO

DAY = date(2025, 3, 5)
D = Decimal


def bars(path: list[float], start: str = "09:15") -> list[Bar]:
    t0 = datetime.combine(DAY, datetime.strptime(start, "%H:%M").time(), IST)
    out, prev = [], path[0]
    for i, c in enumerate(path):
        o = prev
        out.append(Bar("IDX", t0 + timedelta(minutes=i), D(str(o)), D(str(max(o, c))), D(str(min(o, c))), D(str(c)), 0))
        prev = c
    return out


def sess(path: list[float], **feat: str) -> Session:
    b = bars(path)
    return Session(DAY, D("24000"), idx=b, fut=[None] * len(b), vix=[None] * len(b),
                   features={k: D(v) for k, v in feat.items()})  # fmt: skip


def params(sid: str) -> dict[str, Decimal]:
    return {k: v.value for k, v in load_spec_file(REPO / "specs" / f"{sid}.yaml").signal.params.items()}


def test_the_four_promoted_specs_have_plugins_and_one_entry_a_day() -> None:
    for sid in ("S-TRDAY-001", "S-NR7-001", "S-OOPS-001", "S-PBTREND-001"):
        sp = load_spec_file(REPO / "specs" / f"{sid}.yaml")
        assert sid in PLUGINS and sp.version == "0.2.0" and sp.entry.max_entries_per_day == 1


def test_nr7_needs_the_feature_and_breaks_beyond_the_buffer() -> None:
    p = params("S-NR7-001")
    flat = [24000.0] * 15 + [24000 + i for i in range(1, 80)]  # up to 24079 by 10:49
    f = {"nr7": "1", "prev_high": "24050", "prev_low": "23950", "prev_range": "100"}
    plug = NarrowRange7()
    sigs: list[tuple[int, EntrySignal]] = []
    for n in range(16, len(flat) + 1):
        s = sess(flat[:n], **f)
        s.state = {"done": bool(sigs)}
        x = plug.on_bar(s, p)
        if x is not None:
            sigs.append((n, x))
    assert len(sigs) == 1 and sigs[0][1].rights == (OptionRight.CE,)
    assert float(bars(flat[: sigs[0][0]])[-1].close) > 24060  # 24050 + 0.1 x 100
    assert NarrowRange7().on_bar(sess(flat, **{**f, "nr7": "0"}), p) is None
    assert NarrowRange7().on_bar(sess(flat, prev_high="24050"), p) is None  # no feature, no signal


def test_oops_buys_against_a_failed_up_gap_and_exits_at_the_gap_fill() -> None:
    p = params("S-OOPS-001")
    path = [24200.0, 24210, 24190, 24150, 24120, 24100, 24060, 24040]  # opens > 0.3% above 24100, back below
    s = sess(path, prev_high="24100", prev_low="23900")
    sig = OopsGap().on_bar(s, p)
    assert sig is not None and sig.rights == (OptionRight.PE,) and sig.facts["prev_close"] == D("24000")
    t = OpenTrade(sig, s.end, s.last.close, s.end)
    assert OopsGap().check_exit(sess([*path, 23999], prev_high="24100", prev_low="23900"), p, t) == "TARGET_GAP_FILLED"
    assert OopsGap().check_exit(sess([*path, 24300], prev_high="24100", prev_low="23900"), p, t) == (
        "INVALIDATED_EXTREME_RETAKEN"
    )


def test_pullback_needs_trend_and_rsi2_and_a_confirmed_break() -> None:
    p = params("S-PBTREND-001")
    path = [24000.0] * 30 + [24000 + 2 * i for i in range(1, 20)]
    up = {"sma200": "23000", "rsi2": "5"}
    assert PullbackTrend().on_bar(sess(path, **up), p) is not None
    assert PullbackTrend().on_bar(sess(path, **{**up, "rsi2": "50"}), p) is None
    assert PullbackTrend().on_bar(sess(path, **{**up, "sma200": "25000"}), p) is None


@pytest.mark.parametrize("r20,want", [("0.02", OptionRight.CE), ("-0.02", None)])
def test_trend_day_needs_the_multi_week_sign(r20: str, want: OptionRight | None) -> None:
    p = params("S-TRDAY-001")
    path = [24000 + i * 0.9 for i in range(110)]  # +0.4% by 11:05 is not enough; extend to +0.6%
    path += [path[-1] + 0.6 * i for i in range(1, 60)]
    s = sess(path, r20=r20)
    sig = TrendDay().on_bar(s, p)
    assert (sig.rights[0] if sig else None) == want

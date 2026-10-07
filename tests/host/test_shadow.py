"""Shadow strategies, the quote-to-bar builder and the replay feed. SYNTHETIC data; nothing is traded."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path

from project100c.market_types import Quote
from project100c.ops.host_main import load_library_specs
from project100c.ops.shadow import BarBuilder, FeedKeys, ReplayFeed, ShadowStrategies
from project100c.paper.loop import PaperKernel
from project100c.sessions import IST
from project100c.synthetic import DayPlan, generate_day

ROOT = Path(__file__).resolve().parents[2]
MON = date(2026, 10, 5)
KEYS = FeedKeys("IDX", "VIX", "FUT")


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(MON, time(h, m, s), tzinfo=IST)


def q(key: str, ts: datetime, px: str) -> Quote:
    return Quote(key, ts, ts, None, None, None, None, D(px), None, is_option=False)


def test_bar_builder_makes_minute_bars_from_quotes() -> None:
    bb = BarBuilder(frozenset({"IDX"}))
    assert bb.on_quotes({"IDX": q("IDX", at(9, 15, 1), "100"), "OTHER": q("OTHER", at(9, 15, 1), "1")}) == []
    assert bb.on_quotes({"IDX": q("IDX", at(9, 15, 20), "103")}) == []
    assert bb.on_quotes({"IDX": q("IDX", at(9, 15, 40), "99")}) == []
    assert bb.on_quotes({"IDX": q("IDX", at(9, 15, 10), "500")}) == []  # out of order within the minute: still counted
    (b,) = bb.on_quotes({"IDX": q("IDX", at(9, 16, 2), "101")})
    assert (b.start, b.open, b.high, b.low, b.close, b.volume) == (at(9, 15), D(100), D(500), D(99), D(500), 0)
    assert bb.on_quotes({"IDX": q("IDX", at(9, 15, 59), "1")}) == []  # a quote for a closed minute is dropped
    (late,) = bb.flush_before(at(9, 17, 0))
    assert late.start == at(9, 16) and late.close == D(101) and bb.flush_before(at(9, 18)) == []


def test_replay_feed_releases_bars_as_their_minute_closes() -> None:
    sd = generate_day(DayPlan(MON, 7))
    c = Clock(at(9, 15, 30))
    f = ReplayFeed(sd, c, KEYS)
    assert f.closed_bars() == []
    qs = f.poll()
    assert set(qs) == {"IDX", "VIX", "FUT"} and all(not x.is_option for x in qs.values())
    first = sd.index[0]
    assert qs["IDX"].ltp in (first.high, first.low)  # intrabar O-H-L-C path
    c.t = at(9, 17)
    bars = f.closed_bars()
    assert [b[0].start for b in bars] == [at(9, 15), at(9, 16)] and bars[0][0].instrument_key == "IDX"
    assert bars[0][1] is not None and bars[0][1].instrument_key == "FUT" and bars[0][2] is not None
    assert f.closed_bars() == []
    c.t = at(16, 0)
    assert len(f.closed_bars()) == len(sd.index) - 2 and f.poll() == {}


def test_shadow_records_signals_and_never_returns_an_intent(tmp_path: Path) -> None:
    k = PaperKernel.load(ROOT / "configs")
    specs = load_library_specs(ROOT / "specs")
    assert specs and all(s.status.value == "RESEARCH" for s in specs)
    out = tmp_path / "shadow.jsonl"
    sh = ShadowStrategies(specs, k.regime, k.expiries, journal=out)
    sd = generate_day(DayPlan(MON, 7))
    sh.start_day(MON, D(repr(sd.plan.prev_close)))
    vix = {b.start: b for b in sd.vix}
    fut = {b.start: b for b in sd.fut}
    for b in sd.index:
        sh.on_bar(b, fut.get(b.start), vix.get(b.start))
    assert sh.bars == len(sd.index) == 375
    assert sh.signals, "the seeded day produces at least one shadow signal"
    lines = out.read_text().splitlines()
    assert len(lines) == len(sh.signals) and all("SHADOW" in ln for ln in lines)
    assert any(s.repeats > 1 for s in sh.signals)  # repeated signals are folded, not re-recorded
    sh.start_day(MON + timedelta(days=1), None)
    assert sh.signals == [] and sh.bars == 0

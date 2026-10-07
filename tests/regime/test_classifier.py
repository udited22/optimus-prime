"""Regime classifier (K-11) on seeded SYNTHETIC days. The scenarios check mechanics, not that the labels are right
about the real market: the classifier is UNVALIDATED (docs/research/validation.md §13.5)."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from project100c.calendar import ExpiryCalendar, TradingCalendar, load_expiry_rules, load_holiday_book
from project100c.calendar.events import load_event_book
from project100c.errors import ConfigError
from project100c.regime import (
    EventCalendar,
    GapType,
    OpenCharacter,
    RegimeClassifier,
    RegimeConfig,
    RegimeLabel,
    ScheduledEvent,
    Trend,
    VolState,
    classify_session,
    load_regime_config,
)
from project100c.spec.models import Regime
from project100c.synthetic import DayPlan, Segment, generate_day

WED = date(2026, 10, 7)
TUE = date(2026, 10, 6)


@pytest.fixture(scope="module")
def cfg(configs_dir: Path) -> RegimeConfig:
    return load_regime_config(configs_dir / "regime" / "classifier.toml")


@pytest.fixture(scope="module")
def expiries(configs_dir: Path) -> ExpiryCalendar:
    cal = TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml"))
    return ExpiryCalendar(cal, load_expiry_rules(configs_dir / "calendar" / "nifty_expiry_rules.toml"))


def run(cfg: RegimeConfig, plan: DayPlan, **kw: object) -> tuple[RegimeClassifier, list[RegimeLabel]]:
    d = generate_day(plan)
    clf = RegimeClassifier(cfg, **kw)  # type: ignore[arg-type]
    prev = Decimal(repr(plan.prev_close))
    return clf, classify_session(clf, plan.day, list(d.index), prev_close=prev, vix=d.vix_at())


def flat(seed: int, *, vol: float = 12.0, revert: float = 0.0, vix: float = 14.0, day: date = WED) -> DayPlan:
    return DayPlan(day, seed, segments=(Segment(375, 0.0, vol, revert=revert),), vix_open=vix)


# ---------------------------------------------------------------------------------------------- config


def test_the_shipped_config_is_versioned_assumed_and_unvalidated(cfg: RegimeConfig) -> None:
    assert cfg.version.startswith("RC-")
    assert cfg.status == "UNVALIDATED"
    assert not cfg.validated
    assert cfg.trend.adx_exit < cfg.trend.adx_enter  # hysteresis bands


def test_versions_are_selectable_and_the_latest_adopted_wins(tmp_path: Path, configs_dir: Path) -> None:
    text = (configs_dir / "regime" / "classifier.toml").read_text()
    newer = text.replace('version = "RC-2026-10-02.1"', 'version = "RC-2026-10-09.1"').replace(
        "adopted_on = 2026-10-02", "adopted_on = 2026-10-09"
    )
    assert newer != text
    p = tmp_path / "c.toml"
    p.write_text(text + "\n" + newer)
    assert load_regime_config(p).version == "RC-2026-10-09.1"
    assert load_regime_config(p, version="RC-2026-10-02.1").version == "RC-2026-10-02.1"
    with pytest.raises(ConfigError, match="not found"):
        load_regime_config(p, version="RC-1999")
    p.write_text(text + "\n" + text)
    with pytest.raises(ConfigError, match="duplicate"):
        load_regime_config(p)


def test_inverted_hysteresis_bands_are_rejected(tmp_path: Path, configs_dir: Path) -> None:
    text = (configs_dir / "regime" / "classifier.toml").read_text()
    assert text.count('adx_exit = "18"') == 1
    p = tmp_path / "c.toml"
    p.write_text(text.replace('adx_exit = "18"', 'adx_exit = "30"'))
    with pytest.raises(ConfigError, match="hysteresis"):
        load_regime_config(p)


def test_the_shipped_event_book_feeds_the_classifier_verified_entries_only(configs_dir: Path) -> None:
    cal = TradingCalendar(load_holiday_book(configs_dir / "calendar" / "nse_fo_holidays.toml"))
    ev = load_event_book(configs_dir / "calendar" / "events.yaml").to_calendar(cal)
    assert ev.status == "VERIFIED" and ev.events
    assert all(e.source.startswith("https://") for e in ev.events)


# ---------------------------------------------------------------------------------------------- trend


@pytest.mark.parametrize(
    ("drift", "want"),
    [(0.006, Trend.UP), (-0.006, Trend.DOWN)],
)
def test_a_steady_drift_reads_as_a_trend(cfg: RegimeConfig, drift: float, want: Trend) -> None:
    clf, labs = run(cfg, DayPlan(WED, 1 if drift > 0 else 2, segments=(Segment(375, drift, 10),)))
    s = clf.session_summary()
    assert s.trend is want
    assert s.trend_share[want.value] >= Decimal("0.75")
    tag = Regime.TRENDING_UP if want is Trend.UP else Regime.TRENDING_DOWN
    assert tag in labs[-1].tags()


def test_a_mean_reverting_day_reads_as_range(cfg: RegimeConfig) -> None:
    clf, _ = run(cfg, flat(3, revert=0.3))
    s = clf.session_summary()
    assert s.trend is Trend.RANGE
    assert s.trend_share["RANGE"] >= Decimal("0.75")


def test_hysteresis_and_confirmation_limit_flip_flopping(cfg: RegimeConfig) -> None:
    plan = flat(3, revert=0.3)
    loose = cfg.model_copy(update={"trend": cfg.trend.model_copy(update={"confirm_bars": 1})})
    clf_loose, _ = run(loose, plan)
    clf, labs = run(cfg, plan)
    assert clf.session_summary().trend_flips < clf_loose.session_summary().trend_flips
    assert clf.session_summary().trend_flips <= 10
    # a change of label is only adopted after the new label has won the vote on confirm_bars consecutive bars
    live = [x for x in labs if not x.warmup]
    for i in range(1, len(live)):
        if live[i].trend is not live[i - 1].trend:
            assert i >= cfg.trend.confirm_bars - 1


# ---------------------------------------------------------------------------------------------- volatility


def test_quiet_tape_and_low_vix_read_as_compression(cfg: RegimeConfig) -> None:
    clf, labs = run(cfg, flat(4, vol=5, revert=0.3, vix=10))
    assert clf.session_summary().volatility is VolState.COMPRESSION
    assert Regime.VOLATILITY_COMPRESSION in labs[-1].tags()


def test_wild_tape_and_high_vix_read_as_expansion(cfg: RegimeConfig) -> None:
    clf, labs = run(cfg, flat(5, vol=28, vix=20))
    assert clf.session_summary().volatility is VolState.EXPANSION
    assert Regime.VOLATILITY_EXPANSION in labs[-1].tags()


def test_an_ordinary_day_reads_as_normal_volatility(cfg: RegimeConfig) -> None:
    clf, _ = run(cfg, flat(8))
    assert clf.session_summary().volatility is VolState.NORMAL


def test_a_vix_jump_pushes_the_vote_towards_expansion(cfg: RegimeConfig) -> None:
    base = flat(8)
    jump = DayPlan(WED, 8, segments=base.segments, vix_open=14.0, vix_moves=((200, 2.5),))
    _, before = run(cfg, base)
    _, after = run(cfg, jump)
    i = 215
    assert after[i].ts == before[i].ts
    assert dict(after[i].vol_votes)["india_vix"] == "EXPANSION"  # a >= 6% rise over 30 bars overrides the level
    assert dict(before[i].vol_votes)["india_vix"] != "EXPANSION"
    jumped = dict(after[i].measures)["vix_change_pct"]
    assert jumped is not None
    assert jumped >= Decimal(6)


# ---------------------------------------------------------------------------------------------- gap and opening


@pytest.mark.parametrize(
    ("gap", "want"),
    [
        (1.0, GapType.GAP_UP_LARGE),
        (0.4, GapType.GAP_UP),
        (0.1, GapType.FLAT),
        (-0.4, GapType.GAP_DOWN),
        (-1.0, GapType.GAP_DOWN_LARGE),
    ],
)
def test_the_gap_is_typed_from_the_previous_close(cfg: RegimeConfig, gap: float, want: GapType) -> None:
    _, labs = run(cfg, DayPlan(WED, 9, gap_pct=gap, segments=(Segment(60, 0, 10),)))
    assert labs[0].gap is want
    assert (Regime.GAP_REGIME in labs[-1].tags()) is (want is not GapType.FLAT)


def test_no_previous_close_means_an_unknown_gap(cfg: RegimeConfig) -> None:
    d = generate_day(DayPlan(WED, 9, gap_pct=1.0, segments=(Segment(30, 0, 10),)))
    clf = RegimeClassifier(cfg)
    labs = classify_session(clf, WED, list(d.index), prev_close=None)
    assert labs[-1].gap is GapType.UNKNOWN
    assert labs[-1].gap_pct is None
    assert Regime.GAP_REGIME not in labs[-1].tags()


def test_a_gap_that_keeps_running_is_an_opening_drive(cfg: RegimeConfig) -> None:
    plan = DayPlan(WED, 6, gap_pct=1.0, segments=(Segment(30, 0.03, 12), Segment(345, 0.002, 12)))
    _, labs = run(cfg, plan)
    assert labs[10].opening is OpenCharacter.UNDETERMINED  # decided once, at the 30-minute mark
    assert labs[-1].opening is OpenCharacter.OPENING_DRIVE
    assert Regime.OPENING_DRIVE in labs[-1].tags()


def test_a_gap_that_fades_is_an_opening_reversion(cfg: RegimeConfig) -> None:
    plan = DayPlan(WED, 7, gap_pct=0.6, segments=(Segment(40, -0.02, 12), Segment(335, 0, 12, revert=0.3)))
    _, labs = run(cfg, plan)
    assert labs[-1].opening is OpenCharacter.MEAN_REVERSION
    assert Regime.OPENING_REVERSION in labs[-1].tags()


def test_the_opening_label_never_changes_once_decided(cfg: RegimeConfig) -> None:
    _, labs = run(cfg, DayPlan(WED, 6, gap_pct=1.0, segments=(Segment(30, 0.03, 12), Segment(345, -0.01, 12))))
    decided = [x.opening for x in labs if x.opening is not OpenCharacter.UNDETERMINED]
    assert decided
    assert len(set(decided)) == 1


# ---------------------------------------------------------------------------------------------- day conditions


def test_expiry_day_comes_from_the_expiry_calendar(cfg: RegimeConfig, expiries: ExpiryCalendar) -> None:
    assert expiries.is_expiry_day(TUE)
    assert not expiries.is_expiry_day(WED)
    _, on = run(cfg, flat(8, day=TUE), expiries=expiries)
    _, off = run(cfg, flat(8, day=WED), expiries=expiries)
    assert Regime.EXPIRY_REGIME in on[-1].tags()
    assert Regime.EXPIRY_REGIME not in off[-1].tags()


def test_a_listed_event_sets_the_event_regime_all_day(cfg: RegimeConfig) -> None:
    ev = EventCalendar(
        version="T", status="UNVERIFIED", events=(ScheduledEvent(date=WED, kind="RBI_MPC", source="SYNTHETIC"),)
    )
    _, labs = run(cfg, flat(8), events=ev)
    assert all(x.event_day and x.events == ("RBI_MPC",) for x in labs)
    assert Regime.EVENT_REGIME in labs[0].tags()


def test_a_violent_bar_makes_the_rest_of_the_session_abnormal(cfg: RegimeConfig) -> None:
    plan = DayPlan(WED, 8, segments=(Segment(375, 0, 12),), shocks=((120, -1.2),))
    _, labs = run(cfg, plan)
    assert not labs[110].abnormal
    first = next(i for i, x in enumerate(labs) if x.abnormal)
    assert 119 <= first <= 121
    assert all(x.abnormal for x in labs[first:])  # sticky
    assert "one-minute move" in labs[-1].abnormal_reason
    assert Regime.ABNORMAL_MARKET in labs[-1].tags()


def test_an_opening_gap_is_not_a_one_minute_move(cfg: RegimeConfig) -> None:
    # regression: the first bar's one-bar move used to be measured from the previous close, so every gap >= 0.8%
    # read as ABNORMAL_MARKET and blocked the gap strategies for the whole session
    _, labs = run(cfg, DayPlan(WED, 11, gap_pct=1.5, segments=(Segment(60, 0, 10),)))
    assert labs[0].gap is GapType.GAP_UP_LARGE
    assert not any(x.abnormal for x in labs)
    _, big = run(cfg, DayPlan(WED, 11, gap_pct=3.2, segments=(Segment(30, 0, 10),)))
    assert big[0].abnormal  # a gap beyond index_move_pct is still abnormal
    assert "vs previous close" in big[0].abnormal_reason


def test_a_vix_above_the_ceiling_is_abnormal(cfg: RegimeConfig) -> None:
    _, labs = run(cfg, flat(8, vix=31))
    assert labs[0].abnormal
    assert "India VIX" in labs[0].abnormal_reason


# ---------------------------------------------------------------------------------------------- outputs


def test_warmup_bars_are_no_edge(cfg: RegimeConfig) -> None:
    _, labs = run(cfg, flat(8))
    n = cfg.warmup_bars
    assert all(x.warmup for x in labs[: n - 1])
    assert not labs[n - 1].warmup
    assert labs[0].tags() == frozenset({Regime.NO_EDGE})
    assert Regime.NO_EDGE not in labs[n].tags()
    assert labs[0].classifier_agreement == 0


def test_agreement_is_a_voter_share_and_is_not_called_a_confidence(cfg: RegimeConfig) -> None:
    _, labs = run(cfg, flat(8))
    lab = labs[200]
    votes = [v for _, v in lab.trend_votes + lab.vol_votes if v != "ABSTAIN"]
    agree = sum(1 for _, v in lab.trend_votes if v == lab.trend.value) + sum(
        1 for _, v in lab.vol_votes if v == lab.volatility.value
    )
    assert lab.classifier_agreement == (Decimal(agree) / Decimal(len(votes))).quantize(Decimal("0.01"))
    j = lab.as_json()
    assert "not a confidence" in j["classifier_agreement_note"]
    assert "confidence" not in {k.lower() for k in j}
    assert j["status"] == "UNVALIDATED"


def test_labels_are_stamped_at_the_bar_end(cfg: RegimeConfig) -> None:
    d = generate_day(flat(8))
    _, labs = run(cfg, flat(8))
    assert [x.ts for x in labs[:-1]] == [b.start for b in d.index[1:]]


def test_the_classifier_is_deterministic(cfg: RegimeConfig) -> None:
    _, a = run(cfg, flat(11, vol=15))
    _, b = run(cfg, flat(11, vol=15))
    assert [x.as_json() for x in a] == [x.as_json() for x in b]


def test_futures_volume_switches_vwap_to_volume_weighting(cfg: RegimeConfig) -> None:
    d = generate_day(flat(8))
    clf = RegimeClassifier(cfg)
    clf.start_session(WED, Decimal(25000))
    lab_tw = clf.on_bar(d.index[0])
    clf2 = RegimeClassifier(cfg)
    clf2.start_session(WED, Decimal(25000))
    lab_vw = clf2.on_bar(d.index[0], volume=d.fut[0].volume)
    assert dict(lab_tw.measures)["vwap_basis_volume"] == 0
    assert dict(lab_vw.measures)["vwap_basis_volume"] == 1


def test_bars_must_be_in_order_and_in_session(cfg: RegimeConfig) -> None:
    d = generate_day(flat(8))
    clf = RegimeClassifier(cfg)
    with pytest.raises(ConfigError, match="start_session"):
        clf.on_bar(d.index[0])
    clf.start_session(WED, None)
    clf.on_bar(d.index[1])
    with pytest.raises(ConfigError, match="increasing"):
        clf.on_bar(d.index[0])
    clf.start_session(TUE, None)
    with pytest.raises(ConfigError, match="not in the session"):
        clf.on_bar(d.index[5])


def test_range_history_carries_across_sessions(cfg: RegimeConfig) -> None:
    clf = RegimeClassifier(cfg)
    d1 = generate_day(flat(8, day=TUE))
    classify_session(clf, TUE, list(d1.index), prev_close=Decimal(25000))
    d2 = generate_day(flat(9))
    labs = classify_session(clf, WED, list(d2.index), prev_close=d1.close)
    fresh = classify_session(RegimeClassifier(cfg), WED, list(d2.index), prev_close=d1.close)
    # with a prior session of history the range-percentile voter speaks as soon as one window exists;
    # a fresh classifier must first build range_min_history windows inside the session
    i = cfg.volatility.range_bars - 1
    assert dict(labs[i].vol_votes)["range_percentile"] != "ABSTAIN"
    assert dict(fresh[i].vol_votes)["range_percentile"] == "ABSTAIN"
    assert Counter(x.volatility for x in labs)

"""Tony (the Trading CIO): deterministic NOW / WHY / NEXT, attention rules and Ask-Tony answers.

Every rule documented in tony.py's docstring is exercised here on hand-built SystemViews."""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.kernel.kills import KillSwitch
from project100c.observability.dashboard import tony
from project100c.observability.dashboard.tony import (
    ConcernLevel,
    DecisionView,
    LatchView,
    Level,
    OrbWatch,
    PositionView,
    StrategyView,
    SystemView,
    VwapWatch,
)
from project100c.sessions import IST

DAY = date(2026, 10, 5)


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(DAY, time(h, m, s), IST)


def base(**kw: object) -> SystemView:
    v = SystemView(
        now=at(11, 0),
        session_day=DAY,
        next_session=date(2026, 10, 6),
        phase="ENTRY_ALLOWED",
        entry_start=time(9, 20),
        entry_cutoff=time(14, 0),
        flatten_start=time(14, 50),
        hard_flat=time(15, 0),
        spot=D("25000"),
        vix=D("13.5"),
        ret_30m_pct=D("-0.30"),
        regime_tags=("TRENDING_DOWN", "VOLATILITY_COMPRESSION"),
        nav=D("10000"),
        sod_nav=D("10000"),
        realised=D(0),
        unrealised=D(0),
        daily_loss=D(0),
        daily_stop=D(400),
        dd_frac=D(0),
        dd_warning=D("0.10"),
        per_trade_budget=D(200),
        position=None,
        latches=(),
        integrity_failed=False,
        broker_connected=True,
        feed_age_s=D(0),
        decisions=(),
        closed=(),
        strategies=(StrategyView("S-ORB-001", "H01", "Opening-range breakout", "CANARY", False),),
        orb=OrbWatch("ARMED", D("25050"), D("24950"), D(8), 0, 2, time(14, 5), time(9, 30)),
        vwap=VwapWatch(D("25010"), D(25), 0, 3, time(10, 30), time(13, 30)),
    )
    return replace(v, **kw)  # type: ignore[arg-type]


def dec(
    t: datetime, verdict: str = "REJECT", reasons: tuple[str, ...] = ("SMALLEST_LOT_EXCEEDS_BUDGET",)
) -> DecisionView:
    return DecisionView(
        t, "i", "S-VWAPC-001", "SHADOW", "NIFTY X CE", verdict, reasons, D("3700"), D("200"), False, D(180)
    )


POS = PositionView(
    "K", "NIFTY 06OCT26 24950 PE", "S-ORB-001", 65, D("29.45"), D("29.35"), D("27.95"), D("27.45"), D("31.70"),
    True, at(9, 55), D("-6.50"), "S-ORB-001 bought it after a breakdown.",
)  # fmt: skip


RANGE = OrbWatch("BUILDING_RANGE", None, None, D(8), 0, 2, time(14, 5), time(9, 30))


def codes(v: SystemView) -> set[str]:
    return {c.code for c in tony.attention(v).concerns}


# ---------------------------------------------------------------- attention
def test_calm_state_is_normal() -> None:
    a = tony.attention(base())
    assert a.level is Level.NORMAL and a.concerns == () and a.headline == "All systems normal"


@pytest.mark.parametrize("sw", list(KillSwitch))
def test_any_latched_kill_requires_intervention(sw: KillSwitch) -> None:
    v = base(latches=(LatchView("KILL", sw.value, "", "because", at(10, 0)),))
    a = tony.attention(v)
    assert a.level is Level.INTERVENTION and f"KILL:{sw.value}" in codes(v)
    b = tony.brief(v)
    resp, reset = tony.kill_response(sw.value)
    assert resp and reset and reset in b["next"] and b["ai_status"] == "Halted"
    assert b["now"].startswith("Trading halted")


def test_halt_and_integrity_failure_require_intervention() -> None:
    assert tony.attention(base(latches=(LatchView("HALT", "DD_SUSPENSION", "", "dd", at(10, 0)),))).level is (
        Level.INTERVENTION
    )
    assert tony.attention(base(integrity_failed=True)).level is Level.INTERVENTION


def test_unprotected_position_escalates_with_time() -> None:
    p = replace(POS, protective_confirmed=False)
    assert "STOP_PENDING" not in codes(base(position=p, protective_unconfirmed_s=D(30)))
    assert tony.attention(base(position=p, protective_unconfirmed_s=D(31))).level is Level.ATTENTION
    assert tony.attention(base(position=p, protective_unconfirmed_s=D(61))).level is Level.INTERVENTION


def test_rejection_streak_needs_three_in_a_row_within_the_window() -> None:
    three = (dec(at(10, 0)), dec(at(10, 15)), dec(at(10, 30)))
    assert tony.attention(base(decisions=three)).level is Level.ATTENTION
    assert "REJECTION_STREAK" not in codes(base(decisions=three[1:]))
    broken = (*three[:2], dec(at(10, 20), "APPROVE", ()), three[2])
    assert "REJECTION_STREAK" not in codes(base(decisions=broken))
    assert "REJECTION_STREAK" not in codes(base(decisions=three, now=at(11, 31)))


def test_broker_down_feed_stale_near_flatten_loss_and_drawdown_are_attention() -> None:
    assert codes(base(broker_connected=False)) == {"BROKER_DOWN"}
    kill = LatchView("KILL", "BROKER_CONNECTIVITY_KILL", "", "down", at(11, 0))
    assert codes(base(broker_connected=False, latches=(kill,))) == {"KILL:BROKER_CONNECTIVITY_KILL"}
    assert codes(base(feed_age_s=D(5))) == {"FEED_STALE"} and codes(base(feed_age_s=D(4))) == set()
    assert codes(base(feed_age_s=None)) == set()  # no tick yet is not a stale feed
    assert codes(base(position=POS, now=at(14, 20))) == {"NEAR_FLATTEN"}
    assert codes(base(position=POS, now=at(14, 19))) == set() and codes(base(now=at(14, 40))) == set()
    assert codes(base(daily_loss=D(200))) == {"DAILY_LOSS"} and codes(base(daily_loss=D(199))) == set()
    assert codes(base(dd_frac=D("0.10"))) == {"DRAWDOWN"}


def test_cost_advisory_below_threshold_is_attention_but_structural_is_only_a_notice() -> None:
    below = base(economics={"status": "BELOW_THRESHOLD", "raised": True, "headline": "net < 0.5% for 3 months"})
    assert tony.attention(below).level is Level.ATTENTION
    structural = tony.attention(base(economics={"status": "INSUFFICIENT_DATA", "raised": True, "headline": "x"}))
    assert structural.level is Level.NORMAL
    assert [c.level for c in structural.concerns] == [ConcernLevel.INFO]
    assert structural.headline == "All systems normal"


def test_concerns_are_ordered_most_severe_first() -> None:
    v = base(daily_loss=D(300), latches=(LatchView("KILL", "MANUAL_MASTER_KILL", "", "owner", at(10, 5)),))
    a = tony.attention(v)
    assert [c.level for c in a.concerns] == [ConcernLevel.INTERVENTION, ConcernLevel.ATTENTION]
    assert a.headline.endswith("(+1 more)")


# ---------------------------------------------------------------- NOW / WHY / NEXT
def test_watching_state_publishes_the_real_trigger_levels_and_no_confidence_number() -> None:
    b = tony.brief(base())
    assert b["now"] == "Watching for an opening-range breakout (S-ORB-001)"
    assert "25,058.00" in b["next"] and "24,942.00" in b["next"] and "(11:05)" in b["next"]
    assert b["levels"] == {"trigger_up": D("25058"), "trigger_down": D("24942")}
    conf = next(f for f in b["facts"] if f["label"] == "Confidence")
    assert "not produced" in conf["value"] and not re.search(r"\d+\s*%", conf["value"])
    assert "₹200.00" in next(f for f in b["facts"] if f["label"] == "Capital considered")["value"]


def test_market_read_is_rule_based_and_says_when_unknown() -> None:
    assert tony.market_read(base())["text"].startswith("Moderate bearish pressure; volatility compressed")
    assert tony.market_read(base(ret_30m_pct=D("0.6")))["text"].startswith("Strong bullish")
    assert tony.market_read(base(ret_30m_pct=D("0.1")))["text"].startswith("Range-bound")
    assert tony.market_read(base(ret_30m_pct=None))["text"].startswith("No market read yet")


def test_position_brief_uses_the_recorded_thesis_and_static_risk() -> None:
    b = tony.brief(base(position=POS))
    assert b["now"] == "Managing NIFTY 06OCT26 24950 PE long (S-ORB-001)" and b["why"] == POS.thesis
    risk = next(f for f in b["facts"] if f["label"] == "Capital at risk")["value"]
    assert risk.startswith("₹130.00 to the stop limit (65% of")  # (29.45 - 27.45) x 65
    assert "31.70" in b["next"] and "14:30" in b["next"] and "14:50" in b["next"]
    unknown = tony.brief(base(position=replace(POS, thesis="", stop_limit=None)))
    assert "not recorded" in unknown["why"] and any("unknown" in f["value"] for f in unknown["facts"])


@pytest.mark.parametrize(
    ("kw", "now"),
    [
        ({"phase": "OPENING_NO_ENTRY", "orb": RANGE},
         "Observing the open"),
        ({"orb": RANGE}, "Building the opening"),
        ({"orb": OrbWatch("DONE", D(1), D(1), D(8), 2, 2, time(14, 5), time(9, 30))}, "No new entries"),
        ({"now": at(14, 6)}, "No new entries"),
        ({"orb": OrbWatch("WORKING", D(1), D(1), D(8), 1, 2, time(14, 5), time(9, 30))}, "Entry order working"),
        ({"phase": "EXIT_ONLY"}, "Exit-only"),
        ({"phase": "FLATTENING"}, "Forced-flatten window"),
        ({"phase": "CLOSED"}, "Session closed"),
    ],
)  # fmt: skip
def test_every_session_state_has_an_explanation(kw: dict[str, object], now: str) -> None:
    b = tony.brief(base(**kw))
    assert b["now"].startswith(now) and b["why"] and b["next"] and b["ai_status"]


def test_stood_down_explains_the_actual_rejection() -> None:
    d = replace(dec(at(10, 0), reasons=("COOLDOWN",)), strategy="S-ORB-001", stage="CANARY")
    b = tony.brief(base(decisions=(d,), orb=OrbWatch("STOOD_DOWN", D(1), D(1), D(8), 1, 2, time(14, 5), time(9, 30))))
    assert b["now"] == "Not trading: S-ORB-001 stood down for the day"
    assert "cool-down after a stop-out" in b["why"] and "10:00" in b["why"]


def test_closed_session_names_the_next_session() -> None:
    assert "Tue 06 Oct" in tony.brief(base(phase="CLOSED"))["next"]
    assert tony.brief(base(phase="CLOSED", next_session=None))["next"] == "Next session date unknown."


# ---------------------------------------------------------------- answers
def test_answers_cover_every_question_and_use_real_numbers() -> None:
    from project100c.observability.dashboard.tony import ClosedView

    v = base(
        nav=D("10234.96"),
        realised=D("234.96"),
        closed=(ClosedView("S-ORB-001", "NIFTY X PE", D("234.96"), False, at(10, 2)),),
        decisions=(dec(at(10, 30)),),
    )
    p = tony.payload(v)
    assert set(p["answers"]) == {k for k, _ in tony.QUESTIONS}
    pnl = p["answers"]["explain_pnl"]["lines"]
    assert pnl[0] == "NAV ₹10,234.96 against ₹10,000.00 at the start of day: +₹234.96."
    assert any("10:02 S-ORB-001 NIFTY X PE: +₹234.96" in x for x in pnl)
    rej = p["answers"]["last_rejection"]["lines"]
    assert "₹3,700.00" in rej[1] and "₹200.00" in rej[1] and rej[2] == "Reason codes: SMALLEST_LOT_EXCEEDS_BUDGET."
    assert tony.payload(base())["answers"]["last_rejection"]["lines"] == ["No intent has been rejected today."]
    assert p["generated_by"].endswith("(no language model)") and p["labels"] == ["SIMULATED"]


def test_reason_explanations_use_numbers_only_when_known() -> None:
    assert tony.explain_reasons(["SMALLEST_LOT_EXCEEDS_BUDGET"], D(1), D(2)) == [
        "one lot would risk ₹1.00 at the stop, more than the ₹2.00 per-trade budget"
    ]
    assert tony.explain_reasons(["SMALLEST_LOT_EXCEEDS_BUDGET"], None, None) == ["smallest lot exceeds budget"]
    assert tony.explain_reasons(["SOMETHING_NEW"], None, None) == ["something new"]


def test_material_key_ignores_the_timestamp() -> None:
    a, b = tony.payload(base(now=at(11, 0, 10))), tony.payload(base(now=at(11, 0, 40)))
    assert tony.material(a) == tony.material(b) and a["as_of"] != b["as_of"]
    assert tony.material(a) != tony.material(tony.payload(base(phase="EXIT_ONLY")))
    assert tony.material(a) != tony.material(tony.payload(base(broker_connected=False)))
    # a fact flipping (e.g. the broker stop confirmed) is shown at once
    p1 = tony.payload(base(position=replace(POS, protective_confirmed=False)))
    assert tony.material(p1) != tony.material(tony.payload(base(position=POS)))
    assert a["signals"]["or_high"] == D("25050") and a["signals"]["range_end"] == "09:30"
    assert a["system"] == {
        "broker_connected": True,
        "feed_age_s": D(0),
        "phase": "ENTRY_ALLOWED",
        "integrity_failed": False,
    }


def test_tony_has_no_language_model_or_network_path() -> None:
    src = Path(tony.__file__).read_text()
    mods: set[str] = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
    allowed = ("__future__", "collections", "dataclasses", "datetime", "decimal", "enum", "typing")
    allowed_p = ("project100c.economics.fmt", "project100c.kernel.kills", "project100c.sessions")
    assert all(m.startswith(allowed) or m in allowed_p for m in mods), mods
    for word in ("openai", "anthropic", "urllib", "requests", "socket", "http"):
        assert word not in mods


def test_flatten_warning_window_constant_matches_the_doc() -> None:
    assert tony.FLATTEN_WARNING == timedelta(minutes=30) and tony.REJECTION_STREAK == 3


def test_researching_answer_includes_the_validated_stage() -> None:
    """VALIDATED sits between BACKTESTED and PAPER in the directive; the dashboard files it under Researching."""
    v = base(
        strategies=(
            StrategyView("S-A", "H01", "Alpha", "VALIDATED", False),
            StrategyView("S-B", "H02", "Beta", "SHADOW", False),
        )
    )
    lines = tony.payload(v)["answers"]["researching"]["lines"]
    assert lines[0] == "Researching: S-A Alpha (VALIDATED)."
    assert lines[1] == "Paper / shadow, simulate-only (no capital): S-B Beta (SHADOW)."


# ---------------------------------------------------------------- cognition (the neural hero's "current thought")
WORKING = OrbWatch("WORKING", D(1), D(1), D(8), 1, 2, time(14, 5), time(9, 30))
DONE = OrbWatch("DONE", D(1), D(1), D(8), 2, 2, time(14, 5), time(9, 30))
EXPANSION = ("VOLATILITY_EXPANSION",)


@pytest.mark.parametrize(
    ("kw", "verb", "subject"),
    [
        ({}, "WATCHING", "Opening-range breakout · volatility compression / NIFTY"),
        ({"regime_tags": EXPANSION}, "WATCHING", "Opening-range breakout · volatility expansion / NIFTY"),
        ({"regime_tags": ()}, "WATCHING", "Opening-range breakout / NIFTY"),
        ({"phase": "OPENING_NO_ENTRY", "orb": RANGE}, "OBSERVING", "The open / NIFTY"),
        ({"orb": RANGE}, "OBSERVING", "Opening range / NIFTY"),
        ({"orb": WORKING}, "EXECUTING", "S-ORB-001 entry order"),
        ({"position": POS}, "MANAGING", "S-ORB-001 / NIFTY 06OCT26 24950 PE"),
        ({"orb": DONE}, "WAITING", "No valid opportunity / S-ORB-001 done for the day"),
        ({"phase": "EXIT_ONLY"}, "WAITING", "Exit-only / no new entries"),
        ({"phase": "CLOSED"}, "RESTING", "Session closed / flat"),
        ({"latches": (LatchView("KILL", "BROKER_CONNECTIVITY_KILL", "", "down", at(11, 0)),)}, "HALTED", None),
    ],
)  # fmt: skip
def test_cognition_is_a_verb_and_subject_from_the_real_state(
    kw: dict[str, object], verb: str, subject: str | None
) -> None:
    p = tony.payload(base(**kw))
    assert p["cognition"]["verb"] == verb
    if subject is not None:
        assert p["cognition"]["subject"] == subject
    assert not re.search(r"\d+\s*%", p["cognition"]["subject"])  # never a confidence number


def test_cognition_change_is_material() -> None:
    a = tony.payload(base())
    b = tony.payload(base(regime_tags=("VOLATILITY_EXPANSION",)))
    assert tony.material(a) != tony.material(b)


def test_the_market_read_uses_the_classifier_label_when_there_is_one() -> None:
    label = {
        "classifier": "RC-2026-10-02.1",
        "status": "UNVALIDATED",
        "warmup": False,
        "trend": "UP",
        "volatility": "NORMAL",
        "gap": "GAP_UP",
        "opening": "OPENING_DRIVE",
        "abnormal": False,
        "classifier_agreement": "0.67",
    }
    r = tony.market_read(base(regime=label, ret_30m_pct=D("0.31")))
    assert r["text"] == "Trending up; volatility normal; gap up; the open was a drive (+0.31% over 30 min)"
    assert "RC-2026-10-02.1 (UNVALIDATED; thresholds ASSUMED; SIMULATED inputs)" in r["basis"]
    assert "not a confidence" in r["basis"]
    warm = tony.market_read(base(regime={"classifier": "RC-2026-10-02.1", "status": "UNVALIDATED", "warmup": True}))
    assert "NO_EDGE" in warm["text"]
    odd = tony.market_read(base(regime={**label, "abnormal": True, "abnormal_reason": "India VIX 31 >= 30"}))
    assert "ABNORMAL (India VIX 31 >= 30)" in odd["text"]


def test_normal_volatility_has_a_phrase() -> None:
    assert tony.regime_phrase(base(regime_tags=("VOLATILITY_NORMAL",))) == " · normal volatility"

"""Regime-classifier validation maths (docs/research/validation.md §13.5) on hand-made label streams."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from project100c.regime.validation import (
    LabelRow,
    ValidationCriteria,
    boot_test,
    by_day,
    confusion,
    evaluate,
    load_validation_criteria,
    realised_trend,
    samples,
    stability,
)
from project100c.sessions.model import IST
from tests.data.dhan_fakes import CONFIGS

D = date(2026, 8, 3)


def _row(day: date, i: int, close: float, trend: str = "RANGE", vol: str = "NORMAL", warm: bool = False) -> LabelRow:
    ts = datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=9, minutes=16 + i)
    return LabelRow(ts, day, close, warm, trend, vol, "FLAT", "UNDETERMINED", False, False, False)


def test_shipped_criteria_load_and_periods_are_ordered() -> None:
    c = load_validation_criteria(CONFIGS / "regime" / "validation.toml")
    assert c.version == "RV-2026-10-03.1" and c.in_sample[1] < c.out_of_sample[0]
    with pytest.raises(ValidationError):
        ValidationCriteria.model_validate({**c.model_dump(), "out_of_sample": c.in_sample})


def test_csv_round_trip() -> None:
    r = _row(D, 3, 24000.05, "UP", "EXPANSION")
    assert LabelRow.from_csv(r.as_csv()) == r


def test_stability_counts_runs_and_flips_after_warmup() -> None:
    labels = ["UP"] * 10 + ["RANGE"] * 5 + ["UP"] * 5
    rows = [_row(D, i, 100.0, warm=True) for i in range(3)] + [_row(D, 3 + i, 100.0, t) for i, t in enumerate(labels)]
    st = stability(by_day(rows))
    assert st["sessions"] == 1 and st["trend_flips_median"] == 2 and st["trend_run_median"] == 5
    assert st["vol_flips_median"] == 0 and st["vol_run_median"] == 20


def test_samples_need_a_complete_forward_window() -> None:
    rows = [_row(D, i, 100.0 * math.exp(i / 1e4), "UP") for i in range(70)]
    ss = samples(by_day(rows), horizon=30, every=15)
    assert [s.ts for s in ss] == [rows[0].ts, rows[15].ts, rows[30].ts]  # 45 + 30 > 69
    assert all(abs(s.fwd_ret_bp - 30) < 1e-6 for s in ss)
    assert ss[0].trail_sd_bp is None and ss[2].trail_sd_bp is not None
    gap = rows[:20] + rows[21:]  # a missing minute inside the window drops the sample
    assert samples(by_day(gap), horizon=30, every=15)[0].ts == rows[30].ts


def _days(n: int, drift_up: float) -> list[LabelRow]:
    rows: list[LabelRow] = []
    for k in range(n):
        day = D + timedelta(days=k)
        px = 100.0
        for i in range(60):
            trend = "UP" if i < 30 else "DOWN"
            px *= math.exp((drift_up if trend == "UP" else -drift_up) / 1e4 + ((-1) ** (i + k)) * 2e-4)
            rows.append(_row(day, i, px, trend, "NORMAL"))
    return rows


def test_trend_direction_is_positive_when_labels_lead_the_move() -> None:
    ss = samples(by_day(_days(40, 3.0)), horizon=10, every=5)
    t = boot_test("TREND_DIRECTION", ss, level=0.9, iterations=300, seed=1)
    assert t["estimate"] > 0 and t["ci_lower"] > 0
    flat = samples(by_day(_days(40, 0.0)), horizon=10, every=5)
    assert abs(boot_test("TREND_DIRECTION", flat, level=0.9, iterations=300, seed=1)["estimate"]) < 1.0
    assert boot_test("VOL_HIGH", ss, level=0.9, iterations=100, seed=1)["estimate"] is None  # no EXPANSION samples


def test_confusion_and_realised_trend() -> None:
    perfect = confusion([("UP", "UP"), ("DOWN", "DOWN"), ("RANGE", "RANGE")] * 5, ("UP", "DOWN", "RANGE"))
    assert perfect["kappa"] == 1.0 and perfect["lift"]["UP"] == 3.0
    rows = [_row(D, i, 100.0 * math.exp(((-1) ** i) * 1e-4 + i * 5e-4), "UP") for i in range(80)]
    s = samples(by_day(rows), horizon=30, every=15)[-1]
    assert realised_trend(s, 1.0) == "UP"


def test_evaluate_reports_every_test_and_a_verdict() -> None:
    c = load_validation_criteria(CONFIGS / "regime" / "validation.toml")
    c = c.model_copy(update={"in_sample": (D, D + timedelta(days=19)), "bootstrap_iterations": 100,
                             "out_of_sample": (D + timedelta(days=20), D + timedelta(days=39))})  # fmt: skip
    rep = evaluate(_days(40, 3.0), c, classifier="TEST")
    assert set(rep["tests"]) == {"TREND_DIRECTION", "TREND_MAGNITUDE", "VOL_LOW", "VOL_HIGH"}
    assert rep["tests"]["TREND_DIRECTION"]["pass"] is False  # positive, but only one calendar year (< 3)
    assert rep["verdict"] == "FAIL" and rep["failures"]
    cut = evaluate(_days(40, 3.0), c, classifier="TEST", exclude=lambda d: d >= D + timedelta(days=20))
    assert cut["sessions"] == 20 and cut["sessions_excluded"] == 20 and cut["samples"]["OOS"] == 0

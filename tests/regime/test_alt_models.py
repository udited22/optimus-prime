from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from project100c.regime.alt_models import fit_hmm, hmm_walk_forward, tercile_labels, term_slope


def test_term_slope_sign() -> None:
    assert term_slope(0.12, 0.12, 3.0) == pytest.approx(0.0)
    assert (term_slope(0.11, 0.13, 3.0) or 0) > 0  # back richer: upward slope
    assert (term_slope(0.16, 0.13, 3.0) or 0) < 0  # inverted front
    assert term_slope(0.0, 0.1, 3.0) is None


def test_terciles_use_only_earlier_trainable_days() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(100)]
    vals = {d: float(i) for i, d in enumerate(days)}
    lab = tercile_labels(vals, set(days[:80]), lookback=60, min_history=30, labels=["L", "M", "H"])
    assert days[29] not in lab and lab[days[30]] == "H"  # rising series: every new value is above history
    # holdout days (not trainable) are labelled but never enter the history
    assert lab[days[99]] == "H"
    vals2 = {d: (0.0 if i < 80 else -1.0) for i, d in enumerate(days)}
    assert tercile_labels(vals2, set(days[:80]), lookback=60, min_history=30, labels=["L", "M", "H"])[days[90]] == "L"


def _regime_series(n: int) -> tuple[list[list[float]], list[int]]:
    rng = random.Random(11)
    s, xs, states = 0, [], []
    for _ in range(n):
        if rng.random() < 0.05:
            s = 1 - s
        states.append(s)
        xs.append([rng.gauss(3.0 if s else 0.0, 0.5), rng.gauss(0, 1)])
    return xs, states


def test_hmm_recovers_two_states_and_orders_them() -> None:
    xs, states = _regime_series(400)
    m = fit_hmm(xs, k=2)
    assert m.mu[0][0] < m.mu[1][0]
    probs = m.filter(xs)
    acc = sum(1 for p, s in zip(probs, states, strict=True) if (p[1] > 0.5) == bool(s)) / len(xs)
    assert acc > 0.9


def test_hmm_walk_forward_labels_are_causal() -> None:
    xs, _ = _regime_series(420)
    days = [date(2023, 1, 2) + timedelta(days=i) for i in range(420)]
    obs = dict(zip(days, xs, strict=True))
    lab, fits = hmm_walk_forward(obs, set(days), k=2, labels=["CALM", "TURB"], min_train=200)
    assert fits and all(int(str(f["n_train"])) >= 200 for f in fits)
    # changing the future must not change a past label
    obs2 = dict(obs)
    for d in days[-30:]:
        obs2[d] = [10.0, 0.0]
    lab2, _ = hmm_walk_forward(obs2, set(days), k=2, labels=["CALM", "TURB"], min_train=200)
    cut = days[-31]
    assert all(lab[d] == lab2[d] for d in lab if date(cut.year, cut.month, 1) > d >= date(2023, 9, 1))

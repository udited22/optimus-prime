from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from project100c.sessions.model import IST
from project100c.studies.metalabel import (
    Sample,
    auc,
    cv_diagnostic,
    fit_logistic,
    purged_kfold,
    walk_forward,
)


def test_logistic_recovers_a_signal_and_shrinks_with_l2() -> None:
    rng = random.Random(7)
    xs = [[rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(400)]
    ys = [1 if rng.random() < 1 / (1 + 2.718281828 ** -(2 * x[0])) else 0 for x in xs]
    m = fit_logistic(xs, ys, c=1.0)
    assert m.coef[0] > 1.2 and abs(m.coef[1]) < 0.3
    assert m.predict([2.0, 0.0]) > 0.9 > 0.1 > m.predict([-2.0, 0.0])
    strong = fit_logistic(xs, ys, c=0.001)
    assert abs(strong.coef[0]) < abs(m.coef[0])
    assert (auc([m.predict(x) for x in xs], ys) or 0) > 0.75


def test_purged_folds_respect_the_embargo() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(50)]
    folds = purged_kfold(days, 5, 1)
    assert len(folds) == 5
    for train, test in folds:
        lo, hi = days[test[0]], days[test[-1]]
        assert all(not (lo - timedelta(days=1) <= days[i] <= hi + timedelta(days=1)) for i in train)
        assert not set(train) & set(test)


def _samples(n: int, *, signal: bool) -> list[Sample]:
    rng = random.Random(3)
    out = []
    for i in range(n):
        d = date(2023, 1, 2) + timedelta(days=i * 3)
        t = datetime(d.year, d.month, d.day, 10, 0, tzinfo=IST)
        x = rng.gauss(0, 1)
        y = 1 if (x > 0.3 if signal else rng.random() < 0.5) else 0
        out.append(Sample(d, t, t + timedelta(minutes=60), (x, rng.gauss(0, 1)), y, trainable=i % 7 != 0))
    return out


def test_walk_forward_trains_only_on_known_trainable_labels() -> None:
    ss = _samples(300, signal=True)
    dec = walk_forward(ss, retrain_months=1, min_train=150, p_min=0.55, c=1.0)
    assert len(dec) == 300
    first = next(d for d in dec if d.reason != "NO_MODEL")
    assert first.model_trained_on >= 150
    trainable_before = sum(1 for s in ss if s.trainable and s.known_at < first.sample.signal_at)
    assert first.model_trained_on <= trainable_before
    took = [d for d in dec if d.take]
    assert took and sum(d.sample.label for d in took) / len(took) > 0.8
    cv = cv_diagnostic(ss, k=5, embargo_days=1, c=1.0)
    assert cv["auc"] is not None and cv["auc"] > 0.8


def test_missing_features_never_trade() -> None:
    ss = _samples(200, signal=False)
    ss[-1] = Sample(ss[-1].day, ss[-1].signal_at, ss[-1].known_at, None, 1)
    dec = walk_forward(ss, retrain_months=1, min_train=150, p_min=0.55, c=1.0)
    assert dec[-1].reason == "MISSING_FEATURE" and not dec[-1].take

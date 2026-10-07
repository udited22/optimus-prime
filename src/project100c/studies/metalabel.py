"""Meta-labelling for H23 S-ORBML-001: a walk-forward L2 logistic veto on a primary strategy's signals (research).

Pure Python (no numpy): the model has ten features and is trained on a few hundred labelled signals.

* ``fit_logistic``: L2-penalised logistic regression, sklearn's convention (minimise 0.5*|w|^2 + C * sum(logloss),
  intercept not penalised), fitted by Newton-Raphson on standardised features.
* ``purged_kfold``: contiguous time folds; the training side drops every sample within ``embargo_days`` calendar
  days of the test fold (the labels are intraday, so a 1-day embargo also purges overlapping labels).
* ``walk_forward``: refit at the start of every ``retrain_months`` block on the labelled samples whose outcome was
  known before the block (and that are allowed for training, i.e. not holdout); score the block's samples.

Nothing here is evidence of an edge; the decisions are tested downstream through the validation gates.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True, slots=True)
class Sample:
    day: date
    signal_at: datetime
    known_at: datetime  # when the label was known (the trade's exit)
    features: tuple[float, ...] | None  # None: a feature is missing at the signal minute (spec: no trade)
    label: int
    trainable: bool = True  # False for holdout days: scored, never trained on


@dataclass(frozen=True, slots=True)
class LogitModel:
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coef: tuple[float, ...]  # standardised-feature coefficients
    intercept: float
    n_train: int

    def predict(self, x: Sequence[float]) -> float:
        z = self.intercept + sum(
            c * (v - m) / s for c, v, m, s in zip(self.coef, x, self.mean, self.scale, strict=True)
        )
        return _sigmoid(z)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (a is small and positive definite here)."""
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        if abs(piv) < 1e-12:
            raise ArithmeticError("singular Hessian")
        for r in range(c + 1, n):
            f = m[r][c] / piv
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def fit_logistic(xs: Sequence[Sequence[float]], ys: Sequence[int], *, c: float = 1.0, iters: int = 50) -> LogitModel:
    if not xs or len(xs) != len(ys):
        raise ValueError("need matching, non-empty xs and ys")
    k = len(xs[0])
    n = len(xs)
    mean = [sum(x[j] for x in xs) / n for j in range(k)]
    scale = []
    for j in range(k):
        v = sum((x[j] - mean[j]) ** 2 for x in xs) / n
        scale.append(math.sqrt(v) if v > 1e-18 else 1.0)
    z = [[1.0] + [(x[j] - mean[j]) / scale[j] for j in range(k)] for x in xs]
    w = [0.0] * (k + 1)
    for _ in range(iters):
        p = [_sigmoid(sum(wi * zi for wi, zi in zip(w, row, strict=True))) for row in z]
        # gradient and Hessian of 0.5*|w[1:]|^2 + C*sum(logloss)
        g = [c * sum((p[i] - ys[i]) * z[i][j] for i in range(n)) + (w[j] if j else 0.0) for j in range(k + 1)]
        h = [[c * sum(p[i] * (1 - p[i]) * z[i][a] * z[i][b] for i in range(n)) + (1.0 if a == b and a else 0.0)
              for b in range(k + 1)] for a in range(k + 1)]  # fmt: skip
        h[0][0] += 1e-9
        step = _solve(h, g)
        w = [wi - si for wi, si in zip(w, step, strict=True)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return LogitModel(tuple(mean), tuple(scale), tuple(w[1:]), w[0], n)


def log_loss(model: LogitModel, xs: Sequence[Sequence[float]], ys: Sequence[int]) -> float:
    eps = 1e-12
    return -sum(y * math.log(max(model.predict(x), eps)) + (1 - y) * math.log(max(1 - model.predict(x), eps))
                for x, y in zip(xs, ys, strict=True)) / len(ys)  # fmt: skip


def auc(scores: Sequence[float], ys: Sequence[int]) -> float | None:
    pos = [s for s, y in zip(scores, ys, strict=True) if y == 1]
    neg = [s for s, y in zip(scores, ys, strict=True) if y == 0]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def purged_kfold(days: Sequence[date], k: int, embargo_days: int) -> list[tuple[list[int], list[int]]]:
    """Contiguous time folds over samples sorted by day; the training side drops samples within the embargo."""
    order = sorted(range(len(days)), key=lambda i: days[i])
    folds: list[tuple[list[int], list[int]]] = []
    n = len(order)
    for f in range(k):
        test = order[f * n // k : (f + 1) * n // k]
        if not test:
            continue
        lo, hi = days[test[0]] - timedelta(days=embargo_days), days[test[-1]] + timedelta(days=embargo_days)
        tset = set(test)
        train = [i for i in order if i not in tset and not lo <= days[i] <= hi]
        folds.append((train, test))
    return folds


def cv_diagnostic(samples: Sequence[Sample], *, k: int, embargo_days: int, c: float) -> dict[str, float | None]:
    """Purged k-fold out-of-fold AUC and log loss (diagnostic only; never used to tune anything)."""
    xs = [s.features for s in samples if s.features is not None]
    ys = [s.label for s in samples if s.features is not None]
    ds = [s.day for s in samples if s.features is not None]
    scores: list[float] = []
    labels: list[int] = []
    losses: list[float] = []
    for train, test in purged_kfold(ds, k, embargo_days):
        ytr = [ys[i] for i in train]
        if len(set(ytr)) < 2:
            continue
        m = fit_logistic([xs[i] for i in train], ytr, c=c)
        scores += [m.predict(xs[i]) for i in test]
        labels += [ys[i] for i in test]
        losses.append(log_loss(m, [xs[i] for i in test], [ys[i] for i in test]))
    return {"auc": auc(scores, labels), "log_loss": sum(losses) / len(losses) if losses else None,
            "base_rate": sum(ys) / len(ys) if ys else None}  # fmt: skip


def _month_start(d: date, months: int) -> date:
    m = (d.year * 12 + d.month - 1) // months * months
    return date(m // 12, m % 12 + 1, 1)


@dataclass(frozen=True, slots=True)
class Decision:
    sample: Sample
    p: float | None
    take: bool
    reason: str  # TAKE / VETO / NO_MODEL / MISSING_FEATURE
    model_trained_on: int


def walk_forward(
    samples: Sequence[Sample], *, retrain_months: int, min_train: int, p_min: float, c: float
) -> list[Decision]:
    out: list[Decision] = []
    blocks: dict[date, list[Sample]] = {}
    for s in sorted(samples, key=lambda s: s.signal_at):
        blocks.setdefault(_month_start(s.day, retrain_months), []).append(s)
    for start, block in sorted(blocks.items()):
        cut = datetime(start.year, start.month, start.day, tzinfo=block[0].signal_at.tzinfo)
        train = [s for s in samples if s.trainable and s.features is not None and s.known_at < cut]
        model = None
        if len(train) >= min_train and len({s.label for s in train}) == 2:
            model = fit_logistic([s.features for s in train if s.features is not None], [s.label for s in train], c=c)
        for s in block:
            if s.features is None:
                out.append(Decision(s, None, False, "MISSING_FEATURE", len(train)))
            elif model is None:
                out.append(Decision(s, None, False, "NO_MODEL", len(train)))
            else:
                p = model.predict(s.features)
                out.append(Decision(s, p, p >= p_min, "TAKE" if p >= p_min else "VETO", len(train)))
    return out

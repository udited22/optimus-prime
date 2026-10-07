"""Alternative regime models (configs/regime/alt_models.toml): H25 F-TERM-001 and H26 F-HMMREG-001 (research).

Pure Python and point in time:

* ``term_slope``: per-day forward variance between the code-1 and code-2 ATM expiries over the code-1 per-day
  variance, minus 1 (positive = upward-sloping, "calm"; <= 0 = flat or inverted front, "stress").
* ``tercile_labels``: each session's slope against the terciles of the previous trainable sessions only.
* ``GaussianHMM``: diagonal-covariance Gaussian HMM, Baum-Welch with scaling, forward FILTERED probabilities.
* ``hmm_walk_forward``: monthly refits on trainable (non-holdout) sessions before the month; the label for session
  t+1 is the argmax filtered state after session t; states ordered by the mean of the first feature.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


def term_slope(iv1: float, iv2: float, t1_days: float, gap_days: float = 7.0) -> float | None:
    if iv1 <= 0 or iv2 <= 0 or t1_days <= 0 or gap_days <= 0:
        return None
    t2 = t1_days + gap_days
    fwd = (iv2 * iv2 * t2 - iv1 * iv1 * t1_days) / gap_days
    return fwd / (iv1 * iv1) - 1.0


def _quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def tercile_labels(
    values: Mapping[date, float], trainable: set[date], *, lookback: int, min_history: int, labels: Sequence[str]
) -> dict[date, str]:
    """Label each day by the terciles of the previous ``lookback`` trainable days' values (never its own)."""
    out: dict[date, str] = {}
    hist: list[float] = []
    for d in sorted(values):
        v = values[d]
        if len(hist) >= min_history:
            window = hist[-lookback:]
            lo, hi = _quantile(window, 1 / 3), _quantile(window, 2 / 3)
            out[d] = labels[0] if v <= lo else labels[2] if v > hi else labels[1]
        if d in trainable:
            hist.append(v)
    return out


def _logpdf(x: Sequence[float], mu: Sequence[float], var: Sequence[float]) -> float:
    return -0.5 * sum(math.log(2 * math.pi * v) + (xi - m) ** 2 / v for xi, m, v in zip(x, mu, var, strict=True))


@dataclass(frozen=True, slots=True)
class GaussianHMM:
    start: tuple[float, ...]
    trans: tuple[tuple[float, ...], ...]
    mu: tuple[tuple[float, ...], ...]
    var: tuple[tuple[float, ...], ...]
    center: tuple[float, ...]
    scale: tuple[float, ...]
    loglik: float
    iterations: int

    @property
    def k(self) -> int:
        return len(self.start)

    def _z(self, x: Sequence[float]) -> list[float]:
        return [(v - c) / s for v, c, s in zip(x, self.center, self.scale, strict=True)]

    def emissions(self, x: Sequence[float]) -> list[float]:
        z = self._z(x)
        lp = [_logpdf(z, self.mu[i], self.var[i]) for i in range(self.k)]
        m = max(lp)
        return [math.exp(v - m) for v in lp]  # relative likelihoods (a common factor cancels in the filter)

    def filter(self, seq: Sequence[Sequence[float]]) -> list[list[float]]:
        """Forward (filtered) state probabilities P(state_t | x_1..x_t)."""
        out: list[list[float]] = []
        prev: list[float] | None = None
        for x in seq:
            e = self.emissions(x)
            if prev is None:
                a = [self.start[i] * e[i] for i in range(self.k)]
            else:
                a = [sum(prev[j] * self.trans[j][i] for j in range(self.k)) * e[i] for i in range(self.k)]
            s = sum(a) or 1.0
            prev = [v / s for v in a]
            out.append(prev)
        return out


def fit_hmm(
    seq: Sequence[Sequence[float]], *, k: int, max_iter: int = 200, tol: float = 1e-6, var_floor: float = 1e-3
) -> GaussianHMM:
    """Baum-Welch on standardised features from a deterministic start (states seeded by terciles of feature 0)."""
    n, f = len(seq), len(seq[0])
    if n < 3 * k:
        raise ValueError("sequence too short")
    center = [sum(x[j] for x in seq) / n for j in range(f)]
    scale = [math.sqrt(sum((x[j] - center[j]) ** 2 for x in seq) / n) or 1.0 for j in range(f)]
    z = [[(x[j] - center[j]) / scale[j] for j in range(f)] for x in seq]
    order = sorted(range(n), key=lambda i: z[i][0])
    mu, var = [], []
    for s in range(k):
        idx = order[s * n // k : (s + 1) * n // k]
        m = [sum(z[i][j] for i in idx) / len(idx) for j in range(f)]
        v = [max(sum((z[i][j] - m[j]) ** 2 for i in idx) / len(idx), var_floor) for j in range(f)]
        mu.append(m)
        var.append(v)
    start = [1.0 / k] * k
    trans = [[0.9 if i == j else 0.1 / (k - 1) for j in range(k)] for i in range(k)]
    prev_ll = -math.inf
    it = 0
    ll = -math.inf
    for _ in range(max_iter):
        it += 1
        # E step (scaled forward-backward on log-emission rows normalised per t)
        logb = [[_logpdf(z[t], mu[i], var[i]) for i in range(k)] for t in range(n)]
        bmax = [max(r) for r in logb]
        b = [[math.exp(v - bmax[t]) for v in logb[t]] for t in range(n)]
        alpha: list[list[float]] = []
        cs: list[float] = []
        for t in range(n):
            if t == 0:
                a = [start[i] * b[0][i] for i in range(k)]
            else:
                a = [sum(alpha[t - 1][j] * trans[j][i] for j in range(k)) * b[t][i] for i in range(k)]
            c = sum(a) or 1e-300
            cs.append(c)
            alpha.append([v / c for v in a])
        ll = sum(math.log(c) for c in cs) + sum(bmax)
        beta = [[1.0] * k for _ in range(n)]
        for t in range(n - 2, -1, -1):
            beta[t] = [sum(trans[i][j] * b[t + 1][j] * beta[t + 1][j] for j in range(k)) / cs[t + 1] for i in range(k)]
        gamma = []
        for t in range(n):
            g = [alpha[t][i] * beta[t][i] for i in range(k)]
            gs = sum(g) or 1e-300
            gamma.append([v / gs for v in g])
        xi_sum = [[0.0] * k for _ in range(k)]
        for t in range(n - 1):
            den = cs[t + 1]
            for i in range(k):
                for j in range(k):
                    xi_sum[i][j] += alpha[t][i] * trans[i][j] * b[t + 1][j] * beta[t + 1][j] / den
        # M step
        start = [max(g, 1e-6) for g in gamma[0]]
        ssum = sum(start)
        start = [v / ssum for v in start]
        for i in range(k):
            row = [max(v, 1e-6) for v in xi_sum[i]]
            rs = sum(row)
            trans[i] = [v / rs for v in row]
            w = sum(gamma[t][i] for t in range(n)) or 1e-300
            mu[i] = [sum(gamma[t][i] * z[t][j] for t in range(n)) / w for j in range(f)]
            var[i] = [
                max(sum(gamma[t][i] * (z[t][j] - mu[i][j]) ** 2 for t in range(n)) / w, var_floor) for j in range(f)
            ]
        if ll - prev_ll < tol * max(1.0, abs(ll)):
            break
        prev_ll = ll
    # order states by the mean of feature 0 (label switching)
    perm = sorted(range(k), key=lambda i: mu[i][0])
    return GaussianHMM(
        tuple(start[i] for i in perm), tuple(tuple(trans[i][j] for j in perm) for i in perm),
        tuple(tuple(mu[i]) for i in perm), tuple(tuple(var[i]) for i in perm), tuple(center), tuple(scale), ll, it,
    )  # fmt: skip


def hmm_walk_forward(
    obs: Mapping[date, Sequence[float]],
    trainable: set[date],
    *,
    k: int,
    labels: Sequence[str],
    min_train: int,
    max_iter: int = 200,
    tol: float = 1e-6,
    var_floor: float = 1e-3,
) -> tuple[dict[date, str], list[dict[str, object]]]:
    """Label for each session = argmax filtered state after the PREVIOUS session (a slow, morning label).

    Refit at each month start on the trainable sessions before it; sessions before the first fit are labelled with
    the first model (training-period labels). Returns (labels by session, one log row per fit)."""
    days = sorted(obs)
    months = sorted({date(d.year, d.month, 1) for d in days})
    fits: list[dict[str, object]] = []
    out: dict[date, str] = {}
    first: GaussianHMM | None = None
    model: GaussianHMM | None = None
    for m in months:
        train = [d for d in days if d < m and d in trainable]
        if len(train) >= min_train:
            model = fit_hmm([obs[d] for d in train], k=k, max_iter=max_iter, tol=tol, var_floor=var_floor)
            fits.append({"month": str(m), "n_train": len(train), "loglik": round(model.loglik, 3),
                         "iterations": model.iterations,
                         "state_mean_f0": [round(r[0], 3) for r in model.mu]})  # fmt: skip
            if first is None:
                first = model
        if model is None:
            continue
        # filter over every session up to the end of this month (filtering is causal), label the month's sessions
        upto = [d for d in days if d < (date(m.year + (m.month // 12), m.month % 12 + 1, 1))]
        probs = model.filter([obs[d] for d in upto])
        for i, d in enumerate(upto):
            if d >= m and i >= 1:
                p = probs[i - 1]
                out[d] = labels[max(range(k), key=lambda s: p[s])]
    if first is not None:  # training-period labels before the first fit
        pre = [d for d in days if d not in out]
        if pre:
            seq = [d for d in days if d <= pre[-1]]
            probs = first.filter([obs[d] for d in seq])
            for i, d in enumerate(seq):
                if d not in out and i >= 1:
                    p = probs[i - 1]
                    out[d] = labels[max(range(k), key=lambda s: p[s])]
    return out, fits

"""Regime-classifier validation on real bars (docs/research/validation.md §13.5): stability and out-of-sample predictive
validity.

The classifier describes the current intraday state. It is useful to strategies only if (a) its labels persist long
enough to act on, and (b) what it calls a trend, a range, a compression or an expansion is followed, out of sample,
by what those words promise. (b) is judged against EX-ANTE defined, future-realised outcomes (the forward return and
the forward realised volatility), which are used for evaluation only and never as features.

Pure functions over a label stream (``LabelRow``), so the criteria (``configs/regime/validation.toml``) and every
number in a report are reproducible: the only randomness is the seeded day-block bootstrap.
"""

from __future__ import annotations

import math
import random
import statistics
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from project100c.errors import ConfigError
from project100c.market_types import Bar
from project100c.regime.classifier import RegimeClassifier
from project100c.regime.features import MINUTES_PER_YEAR
from project100c.sessions.model import IST
from project100c.validation.stats import percentile

TRENDS = ("UP", "DOWN", "RANGE")
VOLS = ("COMPRESSION", "NORMAL", "EXPANSION")
TESTS = ("TREND_DIRECTION", "TREND_MAGNITUDE", "VOL_LOW", "VOL_HIGH")


class ValidationCriteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^RV-\d{4}-\d{2}-\d{2}\.\d+$")
    adopted_on: date
    in_sample: tuple[date, date]
    out_of_sample: tuple[date, date]
    horizon_minutes: int = Field(ge=5, le=120)
    sample_every_minutes: int = Field(ge=1, le=120)
    ci_level: Decimal = Field(gt=0, lt=1)
    bootstrap_iterations: int = Field(ge=100)
    seed: int
    min_median_trend_run_bars: int = Field(ge=1)
    max_median_trend_flips: int = Field(ge=0)
    min_median_vol_run_bars: int = Field(ge=1)
    max_median_vol_flips: int = Field(ge=0)
    min_years_same_sign: int = Field(ge=1)
    realised_trend_k: Decimal = Field(gt=0)
    holdout: str = Field(min_length=1)  # the holdout version whose days are dropped before scoring

    @model_validator(mode="after")
    def _periods(self) -> ValidationCriteria:
        (a, b), (c, d) = self.in_sample, self.out_of_sample
        if not (a <= b < c <= d):
            raise ValueError("need in_sample to end before out_of_sample starts")
        return self


def load_validation_criteria(path: Path, *, version: str | None = None) -> ValidationCriteria:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("validation")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: no [[validation]] blocks")
    try:
        all_ = [ValidationCriteria.model_validate(b) for b in raw]
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    if version is not None:
        for c in all_:
            if c.version == version:
                return c
        raise ConfigError(f"{path}: validation version {version} not found")
    return max(all_, key=lambda c: (c.adopted_on, c.version))


@dataclass(frozen=True, slots=True)
class LabelRow:
    """One classified 1-minute index bar: ``ts`` is the bar END (when the label becomes usable)."""

    ts: datetime
    day: date
    close: float
    warmup: bool
    trend: str
    vol: str
    gap: str
    opening: str
    expiry: bool
    event: bool
    abnormal: bool

    def as_csv(self) -> str:
        return ",".join((self.ts.isoformat(), self.day.isoformat(), repr(self.close), str(int(self.warmup)), self.trend,
                         self.vol, self.gap, self.opening, str(int(self.expiry)), str(int(self.event)),
                         str(int(self.abnormal))))  # fmt: skip

    @staticmethod
    def from_csv(line: str) -> LabelRow:
        f = line.rstrip("\n").split(",")
        return LabelRow(datetime.fromisoformat(f[0]), date.fromisoformat(f[1]), float(f[2]), f[3] == "1", f[4], f[5],
                        f[6], f[7], f[8] == "1", f[9] == "1", f[10] == "1")  # fmt: skip


CSV_HEADER = "ts,day,close,warmup,trend,vol,gap,opening,expiry,event,abnormal"


def run_stream(
    clf: RegimeClassifier,
    sessions: Iterable[tuple[date, Decimal | None, Sequence[Bar]]],
    vix: Mapping[datetime, Decimal],
) -> list[LabelRow]:
    """Classify whole sessions in order: (day, previous close or None, that day's 1-minute index bars)."""
    out: list[LabelRow] = []
    for day, prev_close, bars in sessions:
        clf.start_session(day, prev_close)
        for b in bars:
            lab = clf.on_bar(b, vix=vix.get(b.start))
            row = LabelRow(lab.ts, day, float(b.close), lab.warmup, lab.trend.value, lab.volatility.value,
                           lab.gap.value, lab.opening.value, lab.expiry_day, lab.event_day, lab.abnormal)  # fmt: skip
            out.append(row)
    return out


def by_day(rows: Iterable[LabelRow]) -> dict[date, list[LabelRow]]:
    d: dict[date, list[LabelRow]] = defaultdict(list)
    for r in rows:
        d[r.day].append(r)
    return dict(d)


def in_period(day: date, period: tuple[date, date]) -> bool:
    return period[0] <= day <= period[1]


# ---------------------------------------------------------------------------------------------------- stability
def _runs(labels: Sequence[str]) -> list[int]:
    out: list[int] = []
    n = 0
    for i, x in enumerate(labels):
        n = n + 1 if i and x == labels[i - 1] else 1
        if i == len(labels) - 1 or labels[i + 1] != x:
            out.append(n)
    return out


def stability(days: Mapping[date, Sequence[LabelRow]]) -> dict[str, Any]:
    """Run lengths (1-minute bars; a run cut by the session end still counts) and label changes per session, on the
    non-warm-up labels of every session."""
    t_runs: list[int] = []
    v_runs: list[int] = []
    t_flips: list[int] = []
    v_flips: list[int] = []
    for rows in days.values():
        live = [r for r in rows if not r.warmup]
        if len(live) < 2:
            continue
        tl, vl = [r.trend for r in live], [r.vol for r in live]
        t_runs += _runs(tl)
        v_runs += _runs(vl)
        t_flips.append(sum(1 for a, b in pairwise(tl) if a != b))
        v_flips.append(sum(1 for a, b in pairwise(vl) if a != b))
    if not t_flips:
        return {"sessions": 0}

    def q(xs: list[int], p: float) -> float:
        return round(percentile([float(x) for x in xs], p), 2)

    return {
        "sessions": len(t_flips),
        "trend_run_median": q(t_runs, 0.5), "trend_run_p25": q(t_runs, 0.25),
        "trend_flips_median": q(t_flips, 0.5), "trend_flips_p90": q(t_flips, 0.9),
        "vol_run_median": q(v_runs, 0.5), "vol_run_p25": q(v_runs, 0.25),
        "vol_flips_median": q(v_flips, 0.5), "vol_flips_p90": q(v_flips, 0.9),
        "sessions_trend_flips_ge_20": round(sum(1 for x in t_flips if x >= 20) / len(t_flips), 4),
    }  # fmt: skip


def label_shares(days: Mapping[date, Sequence[LabelRow]]) -> dict[str, dict[str, float]]:
    """Share of non-warm-up minutes per trend / vol label, by calendar year (stationarity check)."""
    out: dict[str, dict[str, float]] = {}
    years = sorted({d.year for d in days})
    for y in years:
        live = [r for d, rows in days.items() if d.year == y for r in rows if not r.warmup]
        if not live:
            continue
        tc, vc = Counter(r.trend for r in live), Counter(r.vol for r in live)
        out[str(y)] = {
            **{k: round(tc[k] / len(live), 4) for k in TRENDS},
            **{k: round(vc[k] / len(live), 4) for k in VOLS},
        }
    return out


def dominant(rows: Sequence[LabelRow], attr: str) -> str | None:
    live = [str(getattr(r, attr)) for r in rows if not r.warmup]
    if not live:
        return None
    c = Counter(live)
    return max(sorted(c), key=lambda k: c[k])


def session_transitions(days: Mapping[date, Sequence[LabelRow]], attr: str) -> dict[str, dict[str, int]]:
    """Day-to-day transitions of the session-dominant label (consecutive sessions in the data)."""
    keys = sorted(days)
    m: dict[str, dict[str, int]] = {}
    for a, b in pairwise(keys):
        x, y = dominant(days[a], attr), dominant(days[b], attr)
        if x is None or y is None:
            continue
        m.setdefault(x, {}).setdefault(y, 0)
        m[x][y] += 1
    return m


# ---------------------------------------------------------------------------------------------- forward samples
@dataclass(frozen=True, slots=True)
class Sample:
    day: date
    ts: datetime
    trend: str
    vol: str
    fwd_ret_bp: float  # log return over the horizon, basis points
    fwd_rv: float  # annualised realised vol (%) of the horizon's 1-minute log returns
    trail_sd_bp: float | None  # trailing 30-bar 1-minute return sd x sqrt(horizon), basis points (known at ts)


def _rv(closes: Sequence[float]) -> float:
    rets = [math.log(b / a) for a, b in pairwise(closes)]
    if len(rets) < 2:
        return 0.0
    return statistics.stdev(rets) * math.sqrt(MINUTES_PER_YEAR) * 100


def samples(days: Mapping[date, Sequence[LabelRow]], *, horizon: int, every: int) -> list[Sample]:
    """One sample per session every ``every`` minutes (from the session's first bar) with a complete forward window
    of ``horizon`` minutes inside the session (no missing minute) and a non-warm-up label."""
    out: list[Sample] = []
    h = timedelta(minutes=horizon)
    for day in sorted(days):
        rows = days[day]
        if not rows:
            continue
        t0 = rows[0].ts
        for i, r in enumerate(rows):
            if r.warmup or int((r.ts - t0).total_seconds() // 60) % every:
                continue
            j = i + horizon
            if j >= len(rows) or rows[j].ts - r.ts != h:
                continue
            closes = [x.close for x in rows[i : j + 1]]
            trail: float | None = None
            if i >= 30 and rows[i].ts - rows[i - 30].ts == timedelta(minutes=30):
                tr = [math.log(b.close / a.close) for a, b in pairwise(rows[i - 30 : i + 1])]
                trail = statistics.stdev(tr) * math.sqrt(horizon) * 1e4
            out.append(Sample(day, r.ts, r.trend, r.vol, math.log(closes[-1] / closes[0]) * 1e4, _rv(closes), trail))
    return out


# --------------------------------------------------------------------------------------------------- statistics
# Every test is a function of per-day sufficient statistics (sums and counts of two groups), so a day-block
# bootstrap resamples days and re-adds them.
_DayStats = tuple[float, int, float, int]


def _test_stats(name: str, s: Sample) -> tuple[float | None, float | None]:
    """(value for group A or None, value for group B or None). The test statistic is mean(A) - mean(B), or mean(A)
    when the test has no group B."""
    if name == "TREND_DIRECTION":
        if s.trend == "UP":
            return s.fwd_ret_bp, None
        if s.trend == "DOWN":
            return -s.fwd_ret_bp, None
        return None, None
    if name == "TREND_MAGNITUDE":
        return (abs(s.fwd_ret_bp), None) if s.trend in ("UP", "DOWN") else (None, abs(s.fwd_ret_bp))
    if name == "VOL_LOW":
        return (s.fwd_rv, None) if s.vol == "NORMAL" else (None, s.fwd_rv) if s.vol == "COMPRESSION" else (None, None)
    if name == "VOL_HIGH":
        return (s.fwd_rv, None) if s.vol == "EXPANSION" else (None, s.fwd_rv) if s.vol == "NORMAL" else (None, None)
    raise ValueError(name)


_TWO_GROUP = {"TREND_DIRECTION": False, "TREND_MAGNITUDE": True, "VOL_LOW": True, "VOL_HIGH": True}


def _per_day(name: str, ss: Sequence[Sample]) -> list[_DayStats]:
    acc: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for s in ss:
        a, b = _test_stats(name, s)
        v = acc[s.day]
        if a is not None:
            v[0] += a
            v[1] += 1
        if b is not None:
            v[2] += b
            v[3] += 1
    return [(v[0], int(v[1]), v[2], int(v[3])) for _, v in sorted(acc.items())]


def _stat(rows: Iterable[_DayStats], two: bool) -> float | None:
    sa = na = sb = nb = 0.0
    for a, n, b, m in rows:
        sa, na, sb, nb = sa + a, na + n, sb + b, nb + m
    if na == 0 or (two and nb == 0):
        return None
    return sa / na - (sb / nb if two else 0.0)


def boot_test(name: str, ss: Sequence[Sample], *, level: float, iterations: int, seed: int) -> dict[str, Any]:
    """Point estimate and day-block bootstrap CI of one test statistic, with the group sizes."""
    days = _per_day(name, ss)
    two = _TWO_GROUP[name]
    est = _stat(days, two)
    na, nb = sum(d[1] for d in days), sum(d[3] for d in days)
    if est is None:
        return {"estimate": None, "ci_lower": None, "ci_upper": None, "n_a": na, "n_b": nb, "days": len(days)}
    rng = random.Random(seed)
    n = len(days)
    vals: list[float] = []
    for _ in range(iterations):
        v = _stat((days[rng.randrange(n)] for _ in range(n)), two)
        if v is not None:
            vals.append(v)
    a = (1 - level) / 2
    return {"estimate": round(est, 4), "ci_lower": round(percentile(vals, a), 4),
            "ci_upper": round(percentile(vals, 1 - a), 4), "n_a": na, "n_b": nb, "days": n}  # fmt: skip


def realised_trend(s: Sample, k: float) -> str | None:
    if s.trail_sd_bp is None or s.trail_sd_bp <= 0:
        return None
    if s.fwd_ret_bp >= k * s.trail_sd_bp:
        return "UP"
    if s.fwd_ret_bp <= -k * s.trail_sd_bp:
        return "DOWN"
    return "RANGE"


def confusion(pairs: Iterable[tuple[str, str]], labels: Sequence[str]) -> dict[str, Any]:
    """Counts predicted x realised, per-class precision and lift over the realised base rate, and Cohen's kappa."""
    m = {p: dict.fromkeys(labels, 0) for p in labels}
    n = 0
    for p, r in pairs:
        m[p][r] += 1
        n += 1
    if n == 0:
        return {"n": 0}
    real = {r: sum(m[p][r] for p in labels) for r in labels}
    pred = {p: sum(m[p].values()) for p in labels}
    po = sum(m[x][x] for x in labels) / n
    pe = sum(real[x] * pred[x] for x in labels) / (n * n)
    prec = {p: round(m[p][p] / pred[p], 4) if pred[p] else None for p in labels}
    lift = {p: round((m[p][p] / pred[p]) / (real[p] / n), 3) if pred[p] and real[p] else None for p in labels}
    kappa = (po - pe) / (1 - pe) if pe < 1 else 0.0
    return {"n": n, "matrix": m, "precision": prec, "lift": lift, "kappa": round(kappa, 4)}


# ----------------------------------------------------------------------------------------------------- evaluate
def evaluate(
    rows: Sequence[LabelRow],
    crit: ValidationCriteria,
    *,
    classifier: str,
    exclude: Callable[[date], bool] | None = None,
) -> dict[str, Any]:
    """Score a label stream. ``exclude`` drops days before anything is computed (the holdout: research never sees
    it); the stream itself may run through them, since labels only use the past."""
    all_days = by_day(rows)
    days = {d: v for d, v in all_days.items() if exclude is None or not exclude(d)}
    periods = {"IS": crit.in_sample, "OOS": crit.out_of_sample}
    pdays = {k: {d: v for d, v in days.items() if in_period(d, p)} for k, p in periods.items()}
    ss_all = samples(days, horizon=crit.horizon_minutes, every=crit.sample_every_minutes)
    ss = {k: [s for s in ss_all if in_period(s.day, p)] for k, p in periods.items()}
    lvl, it, seed = float(crit.ci_level), crit.bootstrap_iterations, crit.seed
    report: dict[str, Any] = {"classifier": classifier, "criteria": crit.version, "sessions": len(days),
                              "sessions_excluded": len(all_days) - len(days), "holdout": crit.holdout,
                              "first_day": str(min(days)) if days else None,
                              "last_day": str(max(days)) if days else None,
                              "samples": {k: len(v) for k, v in ss.items()}}  # fmt: skip
    fails: list[str] = []
    # (a) stability
    stab = {k: stability(v) for k, v in pdays.items()}
    report["stability"] = stab
    for k, st in stab.items():
        if not st.get("sessions"):
            fails.append(f"stability {k}: no sessions")
            continue
        for key, lim, lo in (("trend_run_median", crit.min_median_trend_run_bars, True),
                             ("trend_flips_median", crit.max_median_trend_flips, False),
                             ("vol_run_median", crit.min_median_vol_run_bars, True),
                             ("vol_flips_median", crit.max_median_vol_flips, False)):  # fmt: skip
            v = st[key]
            if (lo and v < lim) or (not lo and v > lim):
                fails.append(f"stability {k}: {key} {v} {'<' if lo else '>'} {lim}")
    # (b) predictive validity
    tests: dict[str, Any] = {}
    years = sorted({s.day.year for s in ss_all})
    for name in TESTS:
        t = {k: boot_test(name, v, level=lvl, iterations=it, seed=seed) for k, v in ss.items()}
        per_year = {str(y): _stat(_per_day(name, [s for s in ss_all if s.day.year == y]), _TWO_GROUP[name])
                    for y in years}  # fmt: skip
        oos, ins = t["OOS"], t["IS"]
        same_year = sum(1 for v in per_year.values() if v is not None and v > 0)
        oos_ok = oos["estimate"] is not None and oos["estimate"] > 0 and oos["ci_lower"] > 0
        is_ok = ins["estimate"] is not None and ins["estimate"] > 0
        ok = oos_ok and is_ok and same_year >= crit.min_years_same_sign
        tests[name] = {**t, "per_year": {k: None if v is None else round(v, 4) for k, v in per_year.items()},
                       "years_positive": same_year, "pass": ok}  # fmt: skip
        if not ok:
            fails.append(f"{name}: OOS {oos['estimate']} (CI lower {oos['ci_lower']}), IS {ins['estimate']}, "
                         f"positive in {same_year}/{len(years)} years")  # fmt: skip
    report["tests"] = tests
    # confusion matrices (reported)
    kt = float(crit.realised_trend_k)
    is_rv = sorted(s.fwd_rv for s in ss["IS"])
    cuts = (percentile(is_rv, 1 / 3), percentile(is_rv, 2 / 3)) if is_rv else (0.0, 0.0)

    def rvol(s: Sample) -> str:
        return "COMPRESSION" if s.fwd_rv <= cuts[0] else "EXPANSION" if s.fwd_rv > cuts[1] else "NORMAL"

    conf: dict[str, Any] = {"realised_vol_cuts_is": [round(cuts[0], 3), round(cuts[1], 3)]}
    for per, v in ss.items():
        tp = [(s.trend, rt) for s in v if (rt := realised_trend(s, kt)) is not None]
        conf[f"trend_{per}"] = confusion(tp, TRENDS)
        conf[f"vol_{per}"] = confusion(((s.vol, rvol(s)) for s in v), VOLS)
    report["confusion"] = conf
    report["label_shares_by_year"] = label_shares(days)
    report["session_transitions"] = {
        "trend": session_transitions(days, "trend"),
        "vol": session_transitions(days, "vol"),
    }
    report["verdict"] = "PASS" if not fails else "FAIL"
    report["failures"] = fails
    return report


def label_at(index: Mapping[datetime, LabelRow], ts: datetime) -> LabelRow | None:
    """The label usable at ``ts`` (a decision at a bar end uses the label stamped with that bar end)."""
    return index.get(ts.astimezone(IST))

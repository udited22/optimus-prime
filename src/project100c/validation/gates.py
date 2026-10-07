"""The V1-V18 promotion gates (docs/research/validation.md §13.2) as one pure, seeded function: ``validate(inputs,
cfg)``.

Each gate returns PASS, FAIL or NOT_EVALUATED (the input it needs was not supplied, e.g. a point-in-time contract
universe that only real data can give). The verdict:

* REJECTED if any gate FAILs;
* INCOMPLETE if none fails but some are not evaluated;
* VALIDATED only if all 18 pass **on real data**. On SYNTHETIC data the best possible verdict is
  PASSES_ON_SYNTHETIC: a self-test of the toolkit, never a promotion.

Gates V5-V15 use the out-of-sample trades only.
"""

from __future__ import annotations

import random
import statistics
import tomllib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project100c.errors import ConfigError
from project100c.spec.models import StrategySpec
from project100c.validation.stats import day_block_bootstrap_ci, deflated_sharpe, mc_drawdown, moments
from project100c.validation.trades import RECENT_ERA, Trade

LOT = 65


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class Verdict(StrEnum):
    VALIDATED = "VALIDATED"
    PASSES_ON_SYNTHETIC = "PASSES_ON_SYNTHETIC"
    INCOMPLETE = "INCOMPLETE"
    REJECTED = "REJECTED"


class GateConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    adopted_on: date
    seed: int
    ci_level: Decimal
    bootstrap_iterations: int = Field(ge=100)
    min_oos_trades: int = Field(ge=1)
    min_recent_era_trades: int = Field(ge=1)
    top_trade_share: Decimal
    top_trade_max_pnl_share: Decimal
    grid_positive_share: Decimal
    grid_neighbour_median_share: Decimal
    dsr_min: Decimal
    slippage_multiple: Decimal
    charges_multiple: Decimal
    missed_entry_frac: Decimal
    worse_stop_frac: Decimal
    worse_stop_ticks: int = Field(ge=0)
    degraded_iterations: int = Field(ge=10)
    mc_iterations: int = Field(ge=100)
    mc_max_dd_frac: Decimal
    mc_max_dd_prob: Decimal
    mc_ruin_frac: Decimal
    mc_ruin_prob: Decimal
    regime_min_trades: int = Field(ge=1)
    tick: Decimal


def load_gate_config(path: Path, *, version: str | None = None) -> GateConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("gates")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: no [[gates]] blocks")
    try:
        all_ = [GateConfig.model_validate(b) for b in raw]
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    if version is not None:
        for c in all_:
            if c.version == version:
                return c
        raise ConfigError(f"{path}: gate version {version} not found")
    return max(all_, key=lambda c: (c.adopted_on, c.version))


@dataclass(frozen=True, slots=True)
class GateResult:
    code: str
    name: str
    status: GateStatus
    criterion: str
    metrics: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "status": self.status.value, "criterion": self.criterion,
                "metrics": dict(self.metrics), "note": self.note}  # fmt: skip


@dataclass(frozen=True, slots=True)
class ValidationInputs:
    spec: StrategySpec
    trades: Sequence[Trade]
    nav: Decimal
    data_label: str  # "SYNTHETIC" or "REAL"
    n_trials: int  # every run of this family in the experiment registry (V9)
    trial_sr_variance: float | None = None
    lookahead_stamps: Sequence[tuple[datetime, datetime]] | None = None  # (decided_at, end of the latest input)
    latency: timedelta = timedelta(0)
    canary_ci_lower: float | None = None  # V1: the shuffled-future canary feature's CI lower bound (must be <= 0)
    universe_point_in_time: bool | None = None  # V2
    fills_dated_costs: bool | None = None  # V3
    fill_model: Mapping[str, object] | None = None  # V4: the engine's fill_model description
    param_grid: Mapping[tuple[int, ...], float] | None = None  # V8: grid coordinates -> net expectancy per trade
    delayed_trades: Sequence[Trade] | None = None  # V11: the same run with a +1 bar entry delay
    holdout_trades: Sequence[Trade] | None = None  # V16
    holdout_prior_looks: int = 0  # V16: previous holdout evaluations of this spec version
    rerun_hashes: tuple[str, str] | None = None  # V17: ledger hashes of the original run and the re-run
    event_certified: bool = False  # V15
    lot_size: int = LOT


@dataclass(frozen=True, slots=True)
class ValidationReport:
    spec_id: str
    spec_version: str
    data_label: str
    config_version: str
    gates: tuple[GateResult, ...]
    verdict: Verdict
    labels: tuple[str, ...]

    def gate(self, code: str) -> GateResult:
        return next(g for g in self.gates if g.code == code)

    def to_dict(self) -> dict[str, Any]:
        return {"spec_id": self.spec_id, "spec_version": self.spec_version, "data_label": self.data_label,
                "config_version": self.config_version, "verdict": self.verdict.value, "labels": list(self.labels),
                "gates": [g.to_dict() for g in self.gates]}  # fmt: skip


def _f(x: float | Decimal) -> str:
    return f"{float(x):.4f}"


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _ne(code: str, name: str, crit: str, why: str) -> GateResult:
    return GateResult(code, name, GateStatus.NOT_EVALUATED, crit, note=why)


def _ok(b: bool) -> GateStatus:
    return GateStatus.PASS if b else GateStatus.FAIL


def validate(inp: ValidationInputs, cfg: GateConfig) -> ValidationReport:
    oos = [t for t in inp.trades if t.oos]
    pnl = [float(t.net_pnl) for t in oos]
    g: list[GateResult] = []
    synthetic = inp.data_label.upper() != "REAL"

    # V1 look-ahead
    c = "every input ends at or before decision - latency; shuffled-future canary shows no edge"
    if inp.lookahead_stamps is None:
        g.append(_ne("V1", "Look-ahead / leakage", c, "no decision/input stamps supplied"))
    else:
        bad = [(d, i) for d, i in inp.lookahead_stamps if i > d - inp.latency]
        canary_ok = inp.canary_ci_lower is None or inp.canary_ci_lower <= 0
        note = "" if inp.canary_ci_lower is not None else "canary feature not run"
        g.append(GateResult("V1", "Look-ahead / leakage", _ok(not bad and canary_ok), c,
                            {"stamps": str(len(inp.lookahead_stamps)), "violations": str(len(bad))}, note))  # fmt: skip
    # V2 survivorship
    c = "contract universe from the point-in-time master, expired contracts included"
    if inp.universe_point_in_time is None or synthetic:
        g.append(_ne("V2", "Survivorship / universe", c, "needs real point-in-time instrument data"))
    else:
        g.append(GateResult("V2", "Survivorship / universe", _ok(inp.universe_point_in_time), c))
    # V3 costs
    c = "dated cost model applied per order (a charge breakdown on every fill)"
    if inp.fills_dated_costs is None:
        g.append(_ne("V3", "Realistic costs", c, "fills not supplied"))
    else:
        g.append(GateResult("V3", "Realistic costs", _ok(inp.fills_dated_costs), c))
    # V4 fills
    c = "pessimistic fills: limit fills need trade-through, stops can gap, a spread is applied"
    if inp.fill_model is None:
        g.append(_ne("V4", "Realistic fills", c, "fill model not supplied"))
    else:
        fm = inp.fill_model
        strict = fm.get("model") in ("bar", "quote")
        spread = fm.get("model") == "quote" or fm.get("spread_model") is not None
        g.append(GateResult("V4", "Realistic fills", _ok(strict and spread), c,
                            {"model": str(fm.get("model")), "spread_model": str(fm.get("spread_model"))},
                            "" if spread else "no half-spread model: fills are optimistic"))  # fmt: skip
    # V5 OOS expectancy
    c = f"OOS net expectancy > 0 with the {cfg.ci_level} day-block bootstrap CI lower bound > 0"
    ci: tuple[float, float, float] | None = None
    if not oos:
        g.append(GateResult("V5", "OOS expectancy", GateStatus.FAIL, c, note="no out-of-sample trades"))
    else:
        ci = day_block_bootstrap_ci([t.day for t in oos], pnl, level=float(cfg.ci_level),
                                    iterations=cfg.bootstrap_iterations, seed=cfg.seed)  # fmt: skip
        g.append(GateResult("V5", "OOS expectancy", _ok(ci[0] > 0 and ci[1] > 0), c,
                            {"mean": _f(ci[0]), "ci_lower": _f(ci[1]), "ci_upper": _f(ci[2])}))  # fmt: skip
    # V6 sample size
    recent = sum(1 for t in oos if t.era is RECENT_ERA)
    c = f">= {cfg.min_oos_trades} OOS trades and >= {cfg.min_recent_era_trades} in the most recent era"
    ok6 = len(oos) >= cfg.min_oos_trades and recent >= cfg.min_recent_era_trades
    g.append(
        GateResult("V6", "Sample size", _ok(ok6), c, {"oos_trades": str(len(oos)), "recent_era_trades": str(recent)})
    )
    # V7 concentration
    c = f"top {cfg.top_trade_share} of trades < {cfg.top_trade_max_pnl_share} of net P&L; "
    c += "positive without the best month"
    tot = sum(pnl)
    if not oos or tot <= 0:
        g.append(GateResult("V7", "Concentration", GateStatus.FAIL, c, {"net": _f(tot)}, "net P&L is not positive"))
    else:
        k = max(1, int(len(pnl) * float(cfg.top_trade_share)))
        top = sum(sorted(pnl, reverse=True)[:k])
        months: dict[tuple[int, int], float] = defaultdict(float)
        for t in oos:
            months[(t.day.year, t.day.month)] += float(t.net_pnl)
        ex_best = tot - max(months.values())
        share = top / tot
        g.append(GateResult("V7", "Concentration", _ok(share < float(cfg.top_trade_max_pnl_share) and ex_best > 0), c,
                            {"top_share": _f(share), "net_without_best_month": _f(ex_best)}))  # fmt: skip
    # V8 parameter robustness
    c = f">= {cfg.grid_positive_share} of the grid positive; best point's neighbourhood median >= "
    c += f"{cfg.grid_neighbour_median_share} x best"
    if not inp.param_grid:
        g.append(_ne("V8", "Parameter robustness", c, "no pre-registered grid results supplied"))
    else:
        grid = inp.param_grid
        pos = sum(1 for v in grid.values() if v > 0) / len(grid)
        best_k = max(grid, key=lambda k: grid[k])
        nb = [v for k, v in grid.items() if sum(abs(a - b) for a, b in zip(k, best_k, strict=True)) <= 1]
        med = statistics.median(nb)
        best = grid[best_k]
        ok = pos >= float(cfg.grid_positive_share) and best > 0 and med >= float(cfg.grid_neighbour_median_share) * best
        g.append(GateResult("V8", "Parameter robustness", _ok(ok), c,
                            {"positive_share": _f(pos), "best": _f(best), "neighbour_median": _f(med)}))  # fmt: skip
    # V9 DSR
    c = f"Deflated Sharpe probability > {cfg.dsr_min} with the registry's total trial count"
    if len(pnl) < 2:
        g.append(GateResult("V9", "Multiple testing (DSR)", GateStatus.FAIL, c, note="fewer than 2 OOS trades"))
    else:
        m = moments(pnl)
        dsr, sr0 = deflated_sharpe(m, n_trials=inp.n_trials, sr_variance=inp.trial_sr_variance)
        note = "" if inp.trial_sr_variance is not None else "trial SR variance unknown: estimator variance used"
        g.append(GateResult("V9", "Multiple testing (DSR)", _ok(dsr > float(cfg.dsr_min)), c,
                            {"sharpe_per_trade": _f(m.sharpe), "sr0": _f(sr0), "dsr": _f(dsr),
                             "trials": str(inp.n_trials)}, note))  # fmt: skip
    # V10 cost / slippage stress
    c = f"expectancy > 0 at {cfg.slippage_multiple}x slippage and {cfg.charges_multiple}x charges"
    stressed = [float(t.net_pnl - t.slippage * (cfg.slippage_multiple - 1) - t.charges * (cfg.charges_multiple - 1))
                for t in oos]  # fmt: skip
    g.append(GateResult("V10", "Cost / slippage stress", _ok(bool(oos) and _mean(stressed) > 0), c,
                        {"stressed_mean": _f(_mean(stressed))}))  # fmt: skip
    # V11 delayed entry
    c = "expectancy > 0 with a +1 bar entry delay"
    if inp.delayed_trades is None:
        g.append(_ne("V11", "Delayed entry", c, "no delayed re-run supplied"))
    else:
        dm = _mean([float(t.net_pnl) for t in inp.delayed_trades if t.oos])
        g.append(GateResult("V11", "Delayed entry", _ok(dm > 0), c, {"delayed_mean": _f(dm)}))
    # V12 degraded fills
    c = f"{cfg.missed_entry_frac} missed entries and {cfg.worse_stop_frac} of stops {cfg.worse_stop_ticks} ticks worse"
    c += ": expectancy > 0"
    if not oos:
        g.append(GateResult("V12", "Degraded fills", GateStatus.FAIL, c, note="no OOS trades"))
    else:
        rng = random.Random(cfg.seed + 12)
        means = []
        for _ in range(cfg.degraded_iterations):
            kept = []
            for t in oos:
                if rng.random() < float(cfg.missed_entry_frac):
                    continue
                p = float(t.net_pnl)
                if t.exit_reason == "STOP" and rng.random() < float(cfg.worse_stop_frac):
                    p -= float(cfg.tick) * cfg.worse_stop_ticks * t.qty
                kept.append(p)
            means.append(_mean(kept))
        g.append(GateResult("V12", "Degraded fills", _ok(_mean(means) > 0), c, {"degraded_mean": _f(_mean(means))}))
    # V13 Monte Carlo
    c = f"P(max DD > {cfg.mc_max_dd_frac}) < {cfg.mc_max_dd_prob} and "
    c += f"P(ruin to {cfg.mc_ruin_frac}) < {cfg.mc_ruin_prob}"
    if not oos:
        g.append(GateResult("V13", "Monte Carlo sequence", GateStatus.FAIL, c, note="no OOS trades"))
    else:
        p_dd, p_ruin = mc_drawdown(pnl, nav=float(inp.nav), iterations=cfg.mc_iterations, seed=cfg.seed + 13,
                                   dd_frac=float(cfg.mc_max_dd_frac), ruin_frac=float(cfg.mc_ruin_frac))  # fmt: skip
        ok = p_dd < float(cfg.mc_max_dd_prob) and p_ruin < float(cfg.mc_ruin_prob)
        g.append(GateResult("V13", "Monte Carlo sequence", _ok(ok), c, {"p_dd": _f(p_dd), "p_ruin": _f(p_ruin)}))
    # V14 regime dependence
    c = "flat-or-positive in each eligible regime; prohibited regimes not profitable in the data"
    eligible = {r.value for r in inp.spec.eligible_regimes}
    prohibited = {r.value for r in inp.spec.prohibited_regimes}
    slices: dict[str, list[float]] = defaultdict(list)
    for t in oos:
        for tag in t.regimes:
            slices[tag].append(float(t.net_pnl))
    judged = {k: v for k, v in slices.items() if len(v) >= cfg.regime_min_trades}
    bad_el = sorted(k for k, v in judged.items() if k in eligible and _mean(v) < 0)
    bad_pr = sorted(k for k, v in judged.items() if k in prohibited and _mean(v) > 0)
    metrics = {f"{k} (n={len(v)})": _f(_mean(v)) for k, v in sorted(slices.items())}
    if not judged:
        g.append(
            GateResult("V14", "Regime dependence", GateStatus.FAIL, c, metrics, "no regime slice has enough trades")
        )
    else:
        note = "; ".join(x for x in (f"negative in eligible {bad_el}" if bad_el else "",
                                     f"profitable in prohibited {bad_pr}" if bad_pr else "") if x)  # fmt: skip
        g.append(GateResult("V14", "Regime dependence", _ok(not bad_el and not bad_pr), c, metrics, note))
    # V15 event contamination
    c = "reported with and without event days; a non-certified strategy must be positive without them"
    ex = [float(t.net_pnl) for t in oos if not t.event_day]
    ok15 = inp.event_certified or (bool(ex) and _mean(ex) > 0)
    g.append(GateResult("V15", "Event contamination", _ok(ok15), c,
                        {"with_events_mean": _f(_mean(pnl)), "without_events_mean": _f(_mean(ex)),
                         "event_trades": str(len(pnl) - len(ex))}))  # fmt: skip
    # V16 holdout
    c = "a single evaluation: holdout expectancy > 0 and not below the walk-forward CI (below = overfit)"
    if synthetic:
        g.append(_ne("V16", "Holdout", c, "no holdout on SYNTHETIC data"))
    elif inp.holdout_prior_looks > 0:
        g.append(GateResult("V16", "Holdout", GateStatus.FAIL, c, {"prior_looks": str(inp.holdout_prior_looks)},
                            "a second look needs a new version (logged as a new trial)"))  # fmt: skip
    elif inp.holdout_trades is None or ci is None:
        g.append(_ne("V16", "Holdout", c, "no holdout trades or no OOS CI"))
    else:
        hm = _mean([float(t.net_pnl) for t in inp.holdout_trades])
        ok = hm > 0 and hm >= ci[1]  # above the CI is luck in our favour, not overfitting
        g.append(GateResult("V16", "Holdout", _ok(ok), c, {"holdout_mean": _f(hm)},
                            "" if ok or hm <= 0 else "below the walk-forward CI: overfit"))  # fmt: skip
    # V17 reproducibility
    c = "a re-run from the registry record reproduces the ledger hash"
    if inp.rerun_hashes is None:
        g.append(_ne("V17", "Reproducibility", c, "no re-run supplied"))
    else:
        a, b = inp.rerun_hashes
        g.append(GateResult("V17", "Reproducibility", _ok(a == b), c, {"original": a[:16], "rerun": b[:16]}))
    # V18 capital eligibility
    c = "min_capital_inr computed (1-lot worst case / 2% of NAV) and reported against the NAV"
    if not inp.trades:
        g.append(GateResult("V18", "Capital eligibility", GateStatus.FAIL, c, note="no trades to size from"))
    else:
        per_lot = max(t.risk_at_stop * inp.lot_size / t.qty for t in inp.trades if t.qty > 0)
        min_cap = (per_lot / Decimal("0.02")).quantize(Decimal(1))
        g.append(GateResult("V18", "Capital eligibility", GateStatus.PASS, c,
                            {"min_capital_inr": str(min_cap), "nav": str(inp.nav),
                             "eligible_at_nav": str(min_cap <= inp.nav)}))  # fmt: skip
    if any(x.status is GateStatus.FAIL for x in g):
        verdict = Verdict.REJECTED
    elif any(x.status is GateStatus.NOT_EVALUATED for x in g):
        verdict = Verdict.INCOMPLETE
    else:
        verdict = Verdict.PASSES_ON_SYNTHETIC if synthetic else Verdict.VALIDATED
    if (
        synthetic
        and verdict is Verdict.INCOMPLETE
        and all(x.status is GateStatus.PASS for x in g if x.code not in ("V2", "V16"))
    ):
        verdict = Verdict.PASSES_ON_SYNTHETIC  # every gate synthetic data can exercise passed
    labels = (f"data {inp.data_label}", f"gates {cfg.version}", "no edge is claimed by a toolkit self-test")
    return ValidationReport(inp.spec.id, inp.spec.version, inp.data_label, cfg.version, tuple(g), verdict, labels)

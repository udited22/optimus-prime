"""Regime -> strategy map (walk-forward) and the portfolio book it trades (scripts/regime_portfolio.py).

Pure functions over closed-trade records, so the selection and the book are reproducible and testable:

* ``walk_forward_select``: at the start of each test window, choose the (strategy, regime cell) pairs whose
  training trades (everything before the window) clear the pre-registered rule.
* ``simulate_book``: trade the chosen pairs' trades through the system-wide limits: entries a day (OD-014), one
  position at a time, 2% of CURRENT NAV at the stop, daily stop, weekly freeze, the latched drawdown suspension,
  cooldown after a stop-out and exclusive strategy groups.
* ``book_metrics``: net CAGR, Sharpe, Sortino, max drawdown, Calmar, hit rate and expectancy from the daily P&L.

Config: ``configs/portfolio/regime_map.toml``. Every number there is pre-registered before results were read.
"""

from __future__ import annotations

import math
import tomllib
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project100c.errors import ConfigError
from project100c.validation.stats import day_block_bootstrap_ci


class RegimeMapConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^RM-\d{4}-\d{2}-\d{2}\.\d+$")
    adopted_on: date
    classifier: str
    classifier_criteria: str
    cell_dimensions: tuple[Literal["trend", "vol"], ...] = Field(min_length=1)
    holdout: str
    research_from: date
    research_to: date
    first_test_from: date
    test_window_months: int = Field(ge=1, le=12)
    min_train_trades: int = Field(ge=1)
    train_ci_level: Decimal = Field(gt=0, lt=1)
    bootstrap_iterations: int = Field(ge=100)
    seed: int
    navs: tuple[str, ...] = Field(min_length=1)
    max_trades_per_day: int = Field(ge=1)
    per_trade_max_loss_frac: Decimal = Field(gt=0, le=Decimal("0.02"))
    daily_stop_frac: Decimal = Field(gt=0)
    weekly_freeze_frac: Decimal = Field(gt=0)
    dd_suspend_frac: Decimal = Field(gt=0, le=Decimal("0.15"))
    cooldown_after_stop_min: int = Field(ge=0)
    exclusive_groups: tuple[tuple[str, ...], ...] = ()


def load_regime_map_config(path: Path, *, version: str | None = None) -> RegimeMapConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8")).get("regime_map")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: no [[regime_map]] blocks")
    try:
        all_ = [RegimeMapConfig.model_validate(b) for b in raw]
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e
    if version is not None:
        for c in all_:
            if c.version == version:
                return c
        raise ConfigError(f"{path}: regime map version {version} not found")
    return max(all_, key=lambda c: (c.adopted_on, c.version))


@dataclass(frozen=True, slots=True)
class BookTrade:
    """One closed trade of one strategy run, with the regime cell at its signal (money in INR)."""

    strategy: str
    cell: str
    day: date
    signal_at: datetime
    entry_ts: datetime
    exit_ts: datetime
    net_pnl: float
    charges: float
    slippage: float
    risk_at_stop: float
    exit_reason: str
    tags: frozenset[str] = frozenset()

    @property
    def r(self) -> float:
        return self.net_pnl / self.risk_at_stop if self.risk_at_stop > 0 else 0.0


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def walk_forward_windows(cfg: RegimeMapConfig) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    s = cfg.first_test_from
    while s <= cfg.research_to:
        e = min(add_months(s, cfg.test_window_months) - timedelta(days=1), cfg.research_to)
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


@dataclass(frozen=True, slots=True)
class PairStats:
    strategy: str
    cell: str
    n: int
    mean: float
    ci_lower: float
    mean_r: float
    selected: bool


def pair_stats(trades: Sequence[BookTrade], cfg: RegimeMapConfig) -> dict[tuple[str, str], PairStats]:
    groups: dict[tuple[str, str], list[BookTrade]] = defaultdict(list)
    for t in trades:
        groups[(t.strategy, t.cell)].append(t)
    out: dict[tuple[str, str], PairStats] = {}
    for (s, c), ts in sorted(groups.items()):
        pnl = [t.net_pnl for t in ts]
        mean, lo, _ = day_block_bootstrap_ci([t.day for t in ts], pnl, level=float(cfg.train_ci_level),
                                             iterations=cfg.bootstrap_iterations, seed=cfg.seed)  # fmt: skip
        sel = len(ts) >= cfg.min_train_trades and mean > 0 and lo > 0
        out[(s, c)] = PairStats(s, c, len(ts), mean, lo, sum(t.r for t in ts) / len(ts), sel)
    return out


@dataclass(frozen=True, slots=True)
class WindowSelection:
    start: date
    end: date
    train_end: date
    stats: dict[tuple[str, str], PairStats]

    @property
    def selected(self) -> dict[tuple[str, str], PairStats]:
        return {k: v for k, v in self.stats.items() if v.selected}


def walk_forward_select(trades: Sequence[BookTrade], cfg: RegimeMapConfig) -> list[WindowSelection]:
    """Expanding-window refits: each test window is chosen from trades strictly before it (research days only)."""
    out: list[WindowSelection] = []
    for s, e in walk_forward_windows(cfg):
        train = [t for t in trades if cfg.research_from <= t.day < s]
        out.append(WindowSelection(s, e, s - timedelta(days=1), pair_stats(train, cfg)))
    return out


def final_selection(trades: Sequence[BookTrade], cfg: RegimeMapConfig, until: date) -> dict[tuple[str, str], PairStats]:
    """The map refitted on every research trade up to ``until``: what would trade next (e.g. into the holdout)."""
    return pair_stats([t for t in trades if cfg.research_from <= t.day <= until], cfg)


@dataclass(slots=True)
class BookResult:
    nav0: float
    taken: list[BookTrade] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    daily: dict[date, float] = field(default_factory=dict)
    suspended_on: date | None = None

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def simulate_book(
    candidates: Iterable[tuple[BookTrade, float]],
    *,
    nav0: float,
    days: Sequence[date],
    caps: Mapping[str, int],
    cfg: RegimeMapConfig,
) -> BookResult:
    """Trade ``(trade, priority)`` candidates in entry order through the book's limits. ``days`` are the trading days
    of the period (a day without a trade is a zero-P&L day in the metrics); ``caps`` = each spec's entries a day."""
    res = BookResult(nav0)
    for d in days:
        res.daily[d] = 0.0
    group_of = {s: i for i, g in enumerate(cfg.exclusive_groups) for s in g}
    nav = hwm = nav0
    open_until: datetime | None = None
    cool_until: datetime | None = None
    day_entries: dict[date, int] = defaultdict(int)
    strat_entries: dict[tuple[date, str], int] = defaultdict(int)
    group_owner: dict[tuple[date, int], str] = {}
    day_start: dict[date, float] = {}
    week_start: dict[tuple[int, int], float] = {}
    day_real: dict[date, float] = defaultdict(float)
    week_real: dict[tuple[int, int], float] = defaultdict(float)
    pending: list[BookTrade] = []  # entered, not yet exited (P&L lands at exit)
    order = sorted(candidates, key=lambda tp: (tp[0].entry_ts, -tp[1], tp[0].strategy))

    def settle(upto: datetime) -> None:
        nonlocal nav, hwm
        for t in sorted([p for p in pending if p.exit_ts <= upto], key=lambda p: p.exit_ts):
            pending.remove(t)
            nav += t.net_pnl
            hwm = max(hwm, nav)
            res.daily[t.day] = res.daily.get(t.day, 0.0) + t.net_pnl
            day_real[t.day] += t.net_pnl
            wk = t.day.isocalendar()[:2]
            week_real[(wk[0], wk[1])] += t.net_pnl
            if res.suspended_on is None and hwm > 0 and (hwm - nav) / hwm >= float(cfg.dd_suspend_frac):
                res.suspended_on = t.day

    for t, _prio in order:
        settle(t.entry_ts)
        d = t.day
        wk = d.isocalendar()[:2]
        wkey = (wk[0], wk[1])
        day_start.setdefault(d, nav)
        week_start.setdefault(wkey, nav)
        if res.suspended_on is not None:
            res.skip("DD_SUSPENDED")
            continue
        if open_until is not None and t.entry_ts < open_until:
            res.skip("ONE_POSITION")
            continue
        if cool_until is not None and t.entry_ts < cool_until:
            res.skip("COOLDOWN_AFTER_STOP")
            continue
        if -day_real[d] >= float(cfg.daily_stop_frac) * day_start[d]:
            res.skip("DAILY_STOP")
            continue
        if -week_real[wkey] >= float(cfg.weekly_freeze_frac) * week_start[wkey]:
            res.skip("WEEKLY_FREEZE")
            continue
        if day_entries[d] >= cfg.max_trades_per_day:
            res.skip("MAX_TRADES_PER_DAY")
            continue
        if strat_entries[(d, t.strategy)] >= caps.get(t.strategy, 1):
            res.skip("STRATEGY_MAX_ENTRIES")
            continue
        g = group_of.get(t.strategy)
        if g is not None and group_owner.get((d, g), t.strategy) != t.strategy:
            res.skip("EXCLUSIVE_GROUP")
            continue
        if t.risk_at_stop > float(cfg.per_trade_max_loss_frac) * nav:
            res.skip("RISK_OVER_2PCT_OF_NAV")
            continue
        day_entries[d] += 1
        strat_entries[(d, t.strategy)] += 1
        if g is not None:
            group_owner[(d, g)] = t.strategy
        open_until = t.exit_ts
        if t.exit_reason == "STOP":
            cool_until = t.exit_ts + timedelta(minutes=cfg.cooldown_after_stop_min)
        pending.append(t)
        res.taken.append(t)
    settle(datetime.max.replace(tzinfo=order[-1][0].exit_ts.tzinfo) if order else datetime.max)
    return res


def book_metrics(res: BookResult, *, periods_per_year: int = 252) -> dict[str, Any]:
    days = sorted(res.daily)
    nav = res.nav0
    rets: list[float] = []
    path = [nav]
    for d in days:
        r = res.daily[d] / nav if nav > 0 else 0.0
        rets.append(r)
        nav += res.daily[d]
        path.append(nav)
    peak, mdd = path[0], 0.0
    for v in path:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / n) if n else 0.0
    years = n / periods_per_year if n else 0.0
    growth = path[-1] / res.nav0 if res.nav0 > 0 else 0.0
    cagr = growth ** (1 / years) - 1 if years > 0 and growth > 0 else (-1.0 if growth <= 0 and n else 0.0)
    pnl = [t.net_pnl for t in res.taken]
    wins = [p for p in pnl if p > 0]
    return {
        "trading_days": n, "trades": len(pnl), "net_pnl": round(sum(pnl), 2), "nav_end": round(path[-1], 2),
        "net_cagr": round(cagr, 4), "sharpe": round(mean / sd * math.sqrt(periods_per_year), 3) if sd > 0 else None,
        "sortino": round(mean / dd * math.sqrt(periods_per_year), 3) if dd > 0 else None,
        "max_drawdown": round(mdd, 4), "calmar": round(cagr / mdd, 3) if mdd > 0 else None,
        "hit_rate": round(len(wins) / len(pnl), 4) if pnl else None,
        "expectancy_inr": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "expectancy_r": round(sum(t.r for t in res.taken) / len(pnl), 4) if pnl else None,
        "charges": round(sum(t.charges for t in res.taken), 2), "skipped": dict(sorted(res.skipped.items())),
        "suspended_on": None if res.suspended_on is None else str(res.suspended_on),
    }  # fmt: skip


# ---------------------------------------------------------------------------------------------- run records -> trades
EXCLUDING_TAGS = {"EVENT_REGIME": "EVENT_DAY", "ABNORMAL_MARKET": "ABNORMAL", "NO_EDGE": "NO_EDGE_OR_WARMUP"}
_TREND = {"TRENDING_UP": "TREND_UP", "TRENDING_DOWN": "TREND_DOWN", "MEAN_REVERTING": "RANGE"}


def regime_cell(tags: Iterable[str], dims: Sequence[str]) -> str | None:
    """The map's cell for a trade from its signal-time regime tags: the vol state (VOLATILITY_*) and/or the trend
    state, joined with '+'. None when a needed dimension has no tag."""
    ts = set(tags)
    parts: list[str] = []
    for d in dims:
        if d == "vol":
            v = sorted(t for t in ts if t.startswith("VOLATILITY_"))
            if not v:
                return None
            parts.append(v[0])
        else:
            tr = sorted(_TREND[t] for t in ts if t in _TREND)
            if not tr:
                return None
            parts.append(tr[0])
    return "+".join(parts)


def exclusion(tags: Iterable[str], prohibited: Iterable[str]) -> str | None:
    """Why a trade may not be traded by the map (event day, abnormal, warm-up/NO_EDGE, a regime its spec prohibits)."""
    ts = set(tags)
    for tag, why in EXCLUDING_TAGS.items():
        if tag in ts:
            return why
    return "SPEC_PROHIBITED" if ts & set(prohibited) else None


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


_LABEL_TREND = {"UP": "TRENDING_UP", "DOWN": "TRENDING_DOWN", "RANGE": "MEAN_REVERTING"}
_LABEL_OPEN = {"OPENING_DRIVE": "OPENING_DRIVE", "MEAN_REVERSION": "OPENING_REVERSION"}


def tags_from_label_row(row: Mapping[str, str]) -> frozenset[str]:
    """The spec-level tags of one classifier-stream row (scripts/regime_research.py labels CSV), as
    RegimeState.tags(): warm-up -> NO_EDGE only (plus conditions)."""
    out: set[str] = set()
    if row["warmup"] == "1":
        out.add("NO_EDGE")
    else:
        out.add(_LABEL_TREND[row["trend"]])
        out.add(f"VOLATILITY_{row['vol']}")
        if row["opening"] in _LABEL_OPEN:
            out.add(_LABEL_OPEN[row["opening"]])
    if row["gap"] not in ("FLAT", "UNKNOWN"):
        out.add("GAP_REGIME")
    for col, tag in (("expiry", "EXPIRY_REGIME"), ("event", "EVENT_REGIME"), ("abnormal", "ABNORMAL_MARKET")):
        if row[col] == "1":
            out.add(tag)
    return frozenset(out)


class LabelIndex:
    """Classifier-stream tags by bar end; ``at(ts)`` = the latest row ending at or before ``ts`` on the same day."""

    def __init__(self, rows: Iterable[Mapping[str, str]]) -> None:
        intern: dict[frozenset[str], frozenset[str]] = {}
        self._by_day: dict[date, tuple[list[datetime], list[frozenset[str]]]] = {}
        for r in rows:
            ts = datetime.fromisoformat(r["ts"])
            tg = tags_from_label_row(r)
            tg = intern.setdefault(tg, tg)
            ts_l, tg_l = self._by_day.setdefault(ts.date(), ([], []))
            ts_l.append(ts)
            tg_l.append(tg)

    @property
    def days(self) -> list[date]:
        return sorted(self._by_day)

    def at(self, ts: datetime) -> frozenset[str] | None:
        got = self._by_day.get(ts.date())
        if got is None:
            return None
        i = bisect_right(got[0], ts) - 1
        return got[1][i] if i >= 0 else None


def book_trades_from_run(
    doc: Mapping[str, Any],
    *,
    dims: Sequence[str],
    prohibited: Iterable[str],
    labels: LabelIndex | None = None,
) -> tuple[list[BookTrade], dict[str, int]]:
    """One strategy run's closed trades (scripts/regime_strategy_runs.py JSON) as BookTrades, minus the excluded.

    Tags at the signal: the classifier stream's (``labels``) when given and it has the bar, else the plug-in's own
    ``lib_regime_tags`` (H01/H01b record none)."""
    out: list[BookTrade] = []
    skipped: dict[str, int] = defaultdict(int)
    pro = set(prohibited)
    for t in doc["trades"]:
        lt = labels.at(_dt(t["signal_at"])) if labels is not None else None
        tags = sorted(lt) if lt is not None else list(t.get("lib_regime_tags", []))
        why = exclusion(tags, pro)
        cell = regime_cell(tags, dims)
        if why is None and cell is None:
            why = "NO_CELL"
        if why is not None:
            skipped[why] += 1
            continue
        assert cell is not None
        out.append(
            BookTrade(
                strategy=str(doc["strategy"]), cell=cell, day=date.fromisoformat(t["day"]),
                signal_at=_dt(t["signal_at"]),
                entry_ts=_dt(t["entry_ts"]), exit_ts=_dt(t["exit_ts"]), net_pnl=float(t["net_pnl"]),
                charges=float(t["charges"]), slippage=float(t["slippage"]), risk_at_stop=float(t["risk_at_stop"]),
                exit_reason=str(t["exit_reason"]), tags=frozenset(tags),
            )
        )  # fmt: skip
    return out, dict(skipped)


def cell_table(trades: Sequence[BookTrade]) -> list[dict[str, Any]]:
    """Per (strategy, cell): trades, net, hit rate, expectancy in INR and R (plus an ALL row per strategy)."""
    groups: dict[tuple[str, str], list[BookTrade]] = defaultdict(list)
    for t in trades:
        groups[(t.strategy, t.cell)].append(t)
        groups[(t.strategy, "ALL")].append(t)
    rows = []
    for (s, c), ts in sorted(groups.items()):
        pnl = [t.net_pnl for t in ts]
        rows.append({"strategy": s, "cell": c, "trades": len(ts), "net": round(sum(pnl), 2),
                     "hit_rate": round(sum(1 for p in pnl if p > 0) / len(pnl), 4),
                     "expectancy_inr": round(sum(pnl) / len(pnl), 2),
                     "expectancy_r": round(sum(t.r for t in ts) / len(ts), 4),
                     "charges": round(sum(t.charges for t in ts), 2)})  # fmt: skip
    return rows


def mean_diff_ci(
    a: Sequence[tuple[date, float]], b: Sequence[tuple[date, float]], *, level: float, iterations: int, seed: int
) -> tuple[float, float, float] | None:
    """Difference of means (a - b) with an unpaired day-block bootstrap CI: each side resamples its own trading days
    with replacement (a day's trades stay together). None when either side is empty."""
    if not a or not b:
        return None
    import random

    def blocks(xs: Sequence[tuple[date, float]]) -> list[list[float]]:
        by: dict[date, list[float]] = defaultdict(list)
        for d, v in xs:
            by[d].append(v)
        return list(by.values())

    ba, bb = blocks(a), blocks(b)
    rng = random.Random(seed)
    diffs = []
    for _ in range(iterations):
        sa = [v for _ in ba for v in rng.choice(ba)]
        sb = [v for _ in bb for v in rng.choice(bb)]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    lo = diffs[int((1 - level) / 2 * iterations)]
    hi = diffs[min(int((1 + level) / 2 * iterations), iterations - 1)]
    return sum(v for _, v in a) / len(a) - sum(v for _, v in b) / len(b), lo, hi

"""Cost-drag study: what brokerage, statutory charges and fixed running costs take out of a month, by NAV and by the
number of entries a day, with no edge at all (docs/risk/system-economics.md, OD-012, OD-014).

Method (every input is SYNTHETIC or ASSUMED; the output is a zero-edge baseline, not a forecast):

* a month of seeded SYNTHETIC sessions (zero drift, volatility drawn per day) and the SYNTHETIC Black-Scholes chain;
  common random numbers: every (NAV, entries a day) cell sees the same market for a given seed;
* a zero-edge policy: ``k`` entries a day at evenly spaced times, CE or PE by a seeded coin, 1 lot, a 30% premium
  stop, a 20-minute time exit. The direction carries no information, so before costs the expected P&L is about zero
  (theta and the spread make it slightly negative);
* every order goes through the real stack: allocator v1, the Risk Governor (paper venue, regime gate on), the kernel
  runtime, the fake broker and the versioned cost model. Strikes start ATM and walk OTM until the risk at the stop
  fits the allocation;
* sizing: to take ``k`` entries inside the 4% daily risk budget, the study sets the allocator's per-trade fraction to
  min(2%, 4% / k) of NAV (the allocator may only lower risk);
* each day is an independent run at the cell's NAV (no compounding), so a strategy kill latched on one day does not
  carry into the next.

The study measures costs (round trips, brokerage, statutory charges) and the size of the loss at the stop (R). From
those it derives what a strategy would need (gross per trade, win rate at a given payoff) to break even or to make
a target after fixed costs and tax. It never treats the synthetic P&L as an expected return.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from project100c.core_types import OptionRight
from project100c.economics.journal import trading_days_from_journal
from project100c.economics.model import ChargeComponents
from project100c.paper.loop import PaperKernel, PaperLoop
from project100c.sessions import IST
from project100c.spec.io import load_spec_file
from project100c.spec.models import StrategySpec
from project100c.strategies.library import EntrySignal, Session
from project100c.strategies.library.base import BasePlugin
from project100c.synthetic import DayPlan, Segment, SyntheticChain, SyntheticDay, generate_chain

STUDY_ID = "S-ZEROEDGE-001"
LABELS = (
    "SIMULATED: seeded SYNTHETIC sessions and a SYNTHETIC Black-Scholes option chain; not market data",
    "ZERO-EDGE BASELINE, NOT A FORECAST: entry direction is a coin flip; the P&L shows cost drag, not expected returns",
    "ASSUMED: quotes are the synthetic close +/- 1 tick; fills, slippage, volatility and costs are model assumptions",
)
FIRST_SLOT = time(9, 25)
LAST_SLOT = time(13, 55)
DAILY_RISK_FRAC = Decimal("0.04")
PER_TRADE_CAP = Decimal("0.02")
MONTH_SESSIONS = 22  # a month of NSE sessions: shorter runs are scaled to it


def per_trade_frac(k: int) -> Decimal:
    """The per-trade budget that lets ``k`` stops fit the daily risk budget, capped at OD-005's 2%."""
    return min(PER_TRADE_CAP, DAILY_RISK_FRAC / k)


def slot_times(k: int) -> tuple[time, ...]:
    """``k`` entry minutes evenly spaced (mid-points) between 09:25 and 13:55 IST."""
    if k < 1:
        raise ValueError("k must be >= 1")
    a = FIRST_SLOT.hour * 60 + FIRST_SLOT.minute
    span = LAST_SLOT.hour * 60 + LAST_SLOT.minute - a
    mins = [a + int((i + 0.5) * span / k) for i in range(k)]
    return tuple(time(m // 60, m % 60) for m in mins)


def _coin(day: date, seed: int, slot: int) -> bool:
    h = hashlib.sha256(f"zero-edge|{day.isoformat()}|{seed}|{slot}".encode()).digest()
    return h[0] % 2 == 0


class ZeroEdgePlugin(BasePlugin):
    """Enter at the slot minutes; CE or PE by a seeded coin. No information, so no edge by construction."""

    code: str = "ZEROEDGE"
    deviations: tuple[str, ...] = ("study plug-in: direction is a seeded coin flip (zero edge by construction)",)

    def on_bar(self, s: Session, p: Mapping[str, Decimal]) -> EntrySignal | None:
        k, seed = int(p["entries"]), int(p["seed"])
        now = s.end.astimezone(IST).time()
        slots = slot_times(k)
        if now not in slots:
            return None
        i = slots.index(now)
        right = OptionRight.CE if _coin(s.day, seed, i) else OptionRight.PE
        return EntrySignal((right,), f"ZERO_EDGE_SLOT_{i + 1}_OF_{k}", {"slot": i})


@cache
def _base_spec(specs_dir: Path) -> StrategySpec:
    return load_spec_file(specs_dir / "S-VWAPMR-001.yaml")


def zero_edge_spec(specs_dir: Path, k: int, seed: int) -> StrategySpec:
    """A study-only spec (never written to specs/): k entries a day, 30% premium stop, 20-minute time exit."""
    d: dict[str, Any] = _base_spec(specs_dir).model_dump(mode="json")
    d.update(
        id=STUDY_ID,
        hypothesis="Study control: a coin-flip long option has no edge; it measures cost drag only.",
        economic_rationale="None by design. This spec exists only for the cost-drag study.",
        eligible_regimes=["TRENDING_UP", "TRENDING_DOWN", "MEAN_REVERTING"],
        prohibited_regimes=["EVENT_REGIME", "ABNORMAL_MARKET", "NO_EDGE"],
        invalidation_rules=[r for r in d["invalidation_rules"] if r["kind"] in ("TIME", "DATA")],
        expected_frequency=f"{k} entries a day (study)",
        cost_sensitivity="Study control: measures cost sensitivity directly.",
    )
    d["signal"] = {
        "formula": "seeded coin flip at k evenly spaced minutes (zero edge)",
        "params": {
            "entries": {"value": k, "min": 1, "max": 100, "step": 1},
            "seed": {"value": seed, "min": 0, "max": 1_000_000, "step": 1},
        },
    }
    d["entry"] = {**d["entry"], "window_start": "09:25:00", "window_end": "14:00:00", "max_entries_per_day": k}
    d["exit"] = {
        **d["exit"],
        "stop": {**d["exit"]["stop"], "value": 30},
        "profit_taking": "none (time exit only)",
        "time_exit": "14:30:00",
        "max_holding_minutes": 20,
    }
    return StrategySpec.model_validate(d)


# ---------------------------------------------------------------- market
def day_plan(day: date, seed: int) -> DayPlan:
    """Zero-drift SYNTHETIC session; annualised vol 9-18% (VIX alike), opening gap ~ N(0, 0.35%)."""
    rng = random.Random(f"cost-drag|{day.isoformat()}|{seed}")
    vol = rng.uniform(9.0, 18.0)
    return DayPlan(day, seed=rng.randrange(1 << 30), gap_pct=rng.gauss(0.0, 0.35),
                   segments=(Segment(375, 0.0, vol),), vix_open=vol)  # fmt: skip


class NearestChain:
    """Chain source for the paper loop: the two nearest live expiries, ATM +/- ``width`` strikes (wide enough for
    the strike walk), cached per synthetic day."""

    def __init__(self, width: int = 40) -> None:
        self.width = width
        self._c: dict[tuple[DayPlan, tuple[date, ...]], SyntheticChain] = {}

    def __call__(self, sd: SyntheticDay, expiries: Sequence[date]) -> SyntheticChain:
        live = tuple(sorted(e for e in expiries if e >= sd.plan.day)[:2])
        key = (sd.plan, live)
        if key not in self._c:
            self._c[key] = generate_chain(sd, live, strikes_each_side=self.width)
        return self._c[key]


# ---------------------------------------------------------------- one cell-day
@dataclass(frozen=True, slots=True)
class CellDay:
    nav: Decimal
    k: int
    seed: int
    day: date
    round_trips: int
    orders: int
    gross: Decimal
    charges: ChargeComponents
    risks: tuple[Decimal, ...]  # risk at the stop of each approved entry (incl. round-trip charges)
    premiums: tuple[Decimal, ...]  # entry premium of each filled leg
    refusals: Mapping[str, int]
    kills: int


@dataclass(frozen=True, slots=True)
class StudyConfig:
    configs: Path
    specs: Path
    navs: tuple[Decimal, ...] = tuple(Decimal(x) for x in ("10000", "50000", "100000", "200000", "500000"))
    ks: tuple[int, ...] = (1, 3, 5, 10)
    seeds: tuple[int, ...] = (1, 2, 3, 4, 5)
    start: date = date(2026, 11, 2)
    sessions: int = 22
    workers: int = 1


@cache
def _kernel(configs: Path) -> PaperKernel:
    return PaperKernel.load(configs)


def _sized_kernel(configs: Path, k: int) -> PaperKernel:
    k0 = _kernel(configs)
    return dataclasses.replace(k0, allocator=k0.allocator.model_copy(update={"per_trade_frac": per_trade_frac(k)}))


def run_day(cfg: StudyConfig, seed: int, day: date, chain: NearestChain | None = None) -> list[CellDay]:
    """Every (NAV, k) cell for one seeded day; the same market for all of them."""
    chain = chain or NearestChain()
    plan = day_plan(day, seed)
    out: list[CellDay] = []
    for k in cfg.ks:
        kern = _sized_kernel(cfg.configs, k)
        spec = zero_edge_spec(cfg.specs, k, seed)
        for nav in cfg.navs:
            with tempfile.TemporaryDirectory(prefix="cost-drag-") as tmp:
                lp = PaperLoop(kern, [spec], nav=nav, workdir=Path(tmp), plugins={STUDY_ID: ZeroEdgePlugin},
                               strike_fit_steps=40, chain_source=chain)  # fmt: skip
                run = lp.run([plan])
                (td,) = trading_days_from_journal(lp.journal, kern.costs, kern.plan_id, simulated=True)
                lp.journal.close()
            (pd,) = run.days
            refusals: dict[str, int] = {}
            for x in pd.decisions:
                if x.outcome != "APPROVED":
                    why = x.reasons[0].split(":")[0] if x.reasons else x.outcome
                    refusals[why] = refusals.get(why, 0) + x.repeats
            risks = tuple(x.risk_at_stop for x in pd.decisions if x.outcome == "APPROVED" and x.risk_at_stop)
            prem = tuple(lg.entry for t in pd.trades for lg in t.legs if lg.entry is not None)
            out.append(CellDay(nav, k, seed, day, td.round_trips, td.executed_orders, td.gross_pnl, td.charges,
                               risks, prem, refusals, len(pd.kills)))  # fmt: skip
    return out


def _job(args: tuple[StudyConfig, int, date]) -> list[CellDay]:
    return run_day(*args, chain=None)


# ---------------------------------------------------------------- aggregation
@dataclass(frozen=True, slots=True)
class CellResult:
    """One (NAV, k) cell over all seeds: per-month figures (scaled to 22 sessions) are means over the seeds."""

    nav: Decimal
    k: int
    sessions: int
    seeds: int
    round_trips: Decimal  # per month
    brokerage: Decimal
    statutory: Decimal
    gross_per_month: tuple[Decimal, ...]  # one per seed (zero-edge: not an expected return)
    mean_risk: Decimal | None  # mean risk at the stop of an approved entry, incl. round-trip charges
    mean_premium: Decimal | None = None  # mean entry premium of a filled leg
    refusals: Mapping[str, int] = field(default_factory=dict)
    kills: int = 0

    @property
    def charges(self) -> Decimal:
        return self.brokerage + self.statutory

    @property
    def per_trip_charges(self) -> Decimal | None:
        return self.charges / self.round_trips if self.round_trips else None

    @property
    def gross_loss_at_stop(self) -> Decimal | None:
        """R: the gross premium lost at the stop (the risk at the stop less the round-trip charges)."""
        if self.mean_risk is None or self.per_trip_charges is None:
            return None
        return self.mean_risk - self.per_trip_charges


def aggregate(rows: Sequence[CellDay], cfg: StudyConfig) -> list[CellResult]:
    out: list[CellResult] = []
    n_seeds = Decimal(len(cfg.seeds))
    scale = Decimal(MONTH_SESSIONS) / cfg.sessions  # per month
    for k in cfg.ks:
        for nav in cfg.navs:
            cell = [r for r in rows if r.k == k and r.nav == nav]
            ch = sum((r.charges for r in cell), ChargeComponents())
            risks = [x for r in cell for x in r.risks]
            prem = [x for r in cell for x in r.premiums]
            gross = tuple(sum((r.gross for r in cell if r.seed == s), Decimal(0)) * scale for s in cfg.seeds)
            ref: dict[str, int] = {}
            for r in cell:
                for why, n in r.refusals.items():
                    ref[why] = ref.get(why, 0) + n
            out.append(CellResult(nav, k, cfg.sessions, len(cfg.seeds),
                                  Decimal(sum(r.round_trips for r in cell)) * scale / n_seeds,
                                  ch.brokerage * scale / n_seeds, ch.statutory * scale / n_seeds, gross,
                                  sum(risks, Decimal(0)) / len(risks) if risks else None,
                                  sum(prem, Decimal(0)) / len(prem) if prem else None, ref,
                                  sum(r.kills for r in cell)))  # fmt: skip
    return out


def study_days(cfg: StudyConfig) -> list[date]:
    cal = _kernel(cfg.configs).market_clock.calendar
    d = cfg.start if cal.is_trading_day(cfg.start) else cal.next_trading_day(cfg.start)
    days = [d]
    while len(days) < cfg.sessions:
        days.append(cal.next_trading_day(days[-1]))
    return days


def run_study(cfg: StudyConfig) -> list[CellResult]:
    jobs = [(cfg, s, d) for s in cfg.seeds for d in study_days(cfg)]
    rows: list[CellDay] = []
    if cfg.workers > 1:
        with ProcessPoolExecutor(cfg.workers) as ex:
            for part in ex.map(_job, jobs):
                rows.extend(part)
    else:
        chain = NearestChain()
        for c, s, d in jobs:
            rows.extend(run_day(c, s, d, chain))
    return aggregate(rows, cfg)


# ---------------------------------------------------------------- break-even arithmetic
def required_win_rate(gross_per_trade: Decimal, r: Decimal, payoff: Decimal) -> Decimal:
    """Win rate p with p * b * R - (1 - p) * R = E (gross per trade E, loss R at the stop, payoff b)."""
    if r <= 0 or payoff <= 0:
        raise ValueError("need R > 0 and payoff > 0")
    return (gross_per_trade / r + 1) / (payoff + 1)


def needed_gross_per_trade(
    nav: Decimal, trips: Decimal, charges: Decimal, fixed: Decimal, target_net_frac: Decimal, tax: Decimal
) -> Decimal | None:
    """Gross P&L each round trip must average for the month to end at ``target_net_frac`` of NAV after the
    trading charges, the fixed costs and tax on a positive result."""
    if trips <= 0:
        return None
    target = target_net_frac * nav
    pre_tax = target / (1 - tax) if target > 0 else target
    return (pre_tax + fixed + charges) / trips


# ---------------------------------------------------------------- report
PAYOFFS: tuple[Decimal, ...] = (Decimal(1), Decimal("1.5"), Decimal(2), Decimal(3))
TARGETS: tuple[Decimal, ...] = (Decimal(0), Decimal("0.05"))  # net a month after tax (0% = break even)
COST_SHARES: tuple[Decimal, ...] = (Decimal("0.02"), Decimal("0.05"))  # ASSUMED bars: costs as a share of NAV
BE_OVER_R_OK = Decimal("0.2")  # break-even gross per trade at or under 0.2 R
LOT = 65
TICK_INR = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class Economics:
    fixed_monthly: Decimal
    tax_rate: Decimal
    version: str


@dataclass(frozen=True, slots=True)
class Mix:
    name: str
    parts: tuple[tuple[str, int], ...]  # (strategy family, entries a day)

    @property
    def per_day(self) -> int:
        return sum(n for _, n in self.parts)


MIXES: tuple[Mix, ...] = (
    Mix("Canary style: 3 strategies x 1 a day", (("strategy A", 1), ("strategy B", 1), ("strategy C", 1))),
    Mix(
        "RSI/MACD reversal 2 + 3 others x 1",
        (("RSI/MACD reversal (H18)", 2), ("strategy A", 1), ("strategy B", 1), ("strategy C", 1)),
    ),
    Mix(
        "Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap)",
        (("scalper (H17)", 6), ("RSI/MACD reversal (H18)", 2), ("strategy A", 1), ("strategy B", 1)),
    ),
)


def _pct(x: Decimal | None, dp: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{dp}f}%"


def _r(x: Decimal | None, dp: int = 0) -> str:
    from project100c.economics.fmt import inr

    return "n/a" if x is None else inr(x, dp)


def _q(xs: Sequence[Decimal], q: float) -> Decimal:
    v = sorted(xs)
    if len(v) == 1:
        return v[0]
    i = q * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * Decimal(repr(i - lo))


def _win_rate(c: CellResult, econ: Economics, target: Decimal, b: Decimal) -> str:
    be = needed_gross_per_trade(c.nav, c.round_trips, c.charges, econ.fixed_monthly, target, econ.tax_rate)
    r = c.gross_loss_at_stop
    if be is None or r is None or r <= 0:
        return "n/a"
    p = required_win_rate(be, r, b)
    return f"{p * 100:.0f}%" + (" (infeasible)" if p > 1 else "")


def recommend(cells: Sequence[CellResult], econ: Economics, nav: Decimal, cost_share: Decimal) -> int | None:
    """The most entries a day at this NAV for which costs (charges + fixed) stay within ``cost_share`` of NAV a month
    AND the break-even gross per trade stays within 0.2 R. None when even 1 a day misses (or nothing fits)."""
    best: int | None = None
    for c in sorted((x for x in cells if x.nav == nav), key=lambda x: x.k):
        be = needed_gross_per_trade(c.nav, c.round_trips, c.charges, econ.fixed_monthly, Decimal(0), econ.tax_rate)
        r = c.gross_loss_at_stop
        if be is None or r is None or r <= 0:
            continue
        if (c.charges + econ.fixed_monthly) / c.nav <= cost_share and be / r <= BE_OVER_R_OK:
            best = c.k
    return best


def render_markdown(cells: Sequence[CellResult], cfg: StudyConfig, econ: Economics) -> str:
    L: list[str] = []
    add = L.append
    add("> " + "\n> ".join(LABELS))
    add("")
    add(
        f"Grid: NAV {', '.join(_r(n) for n in cfg.navs)} x entries a day {', '.join(map(str, cfg.ks))}; "
        f"{cfg.sessions} sessions from {cfg.start.isoformat()}; seeds {', '.join(map(str, cfg.seeds))}; "
        f"economics {econ.version}: fixed {_r(econ.fixed_monthly, 2)}/month, tax {_pct(econ.tax_rate)} (ASSUMED)."
    )
    add("")
    add("## Headline: cost drag per month (means over seeds)")
    add("")
    add(
        "| NAV | Entries/day | Per-trade budget | Round trips/month | Brokerage | Statutory | Fixed | "
        "Total cost | Cost % NAV | Mean premium | R (loss at stop) | Break-even gross/trade | Break-even / R |"
    )
    add("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in cells:
        tot = c.charges + econ.fixed_monthly
        be = needed_gross_per_trade(c.nav, c.round_trips, c.charges, econ.fixed_monthly, Decimal(0), econ.tax_rate)
        r = c.gross_loss_at_stop
        ratio = None if be is None or r is None or r <= 0 else be / r
        add(
            f"| {_r(c.nav)} | {c.k} | {_r(per_trade_frac(c.k) * c.nav)} ({_pct(per_trade_frac(c.k))}) | "
            f"{c.round_trips:.1f} | {_r(c.brokerage)} | {_r(c.statutory)} | {_r(econ.fixed_monthly)} | {_r(tot)} | "
            f"{_pct(tot / c.nav)} | {_r(c.mean_premium, 2)} | {_r(r)} | {_r(be)} | "
            f"{'n/a' if ratio is None else f'{ratio:.2f} R'} |"
        )
    add("")
    add("A cell with 0 round trips means no strike fitted the per-trade budget, or the allocator refused the entry.")
    add("")
    add("## Win rate a strategy would need (from the measured costs and R)")
    add("")
    add(
        "p = (E / R + 1) / (b + 1), where E is the gross each trade must average, R the gross loss at the stop and "
        "b the average win as a multiple of R. These are requirements, not estimates."
    )
    add("")
    heads = " | ".join(f"net {_pct(t, 0)} @ b={b}" for t in TARGETS for b in PAYOFFS)
    add(f"| NAV | Entries/day | {heads} |")
    add("|---:|---:|" + "---:|" * (len(TARGETS) * len(PAYOFFS)))
    for c in cells:
        add(f"| {_r(c.nav)} | {c.k} | " + " | ".join(_win_rate(c, econ, t, b) for t in TARGETS for b in PAYOFFS) + " |")
    add("")
    add("## The synthetic month (ZERO-EDGE: this is cost drag plus noise, never an expected return)")
    add("")
    add("| NAV | Entries/day | Net % NAV p5 | median | p95 | Refusals (count over all seeds) | Kills |")
    add("|---:|---:|---:|---:|---:|:---|---:|")
    for c in cells:
        nets = [(g - c.charges - econ.fixed_monthly) / c.nav for g in c.gross_per_month]
        ref = ", ".join(f"{k} {v}" for k, v in sorted(c.refusals.items())) or "none"
        add(
            f"| {_r(c.nav)} | {c.k} | {_pct(_q(nets, 0.05))} | {_pct(_q(nets, 0.5))} | {_pct(_q(nets, 0.95))} | "
            f"{ref} | {c.kills} |"
        )
    add("")
    add("## Strategy mixes at the planned scale")
    add("")
    add(
        "Per-trip charges are the measured ones of the cell with the mix's total entries a day (the per-trade budget "
        "is min(2%, 4% / entries) of NAV, so more entries mean cheaper, further-OTM strikes)."
    )
    for nav in (n for n in cfg.navs if n in (Decimal(100_000), Decimal(200_000))):
        add("")
        add(f"### NAV {_r(nav)}")
        add("")
        add("| Mix | Strategy | Entries/day | Round trips/month | Charges/month | Charges per trip in ticks (65 qty) |")
        add("|:---|:---|---:|---:|---:|---:|")
        notes: list[str] = []
        for mx in MIXES:
            cell = next((c for c in cells if c.nav == nav and c.k == mx.per_day), None)
            if cell is None or cell.per_trip_charges is None:
                add(f"| {mx.name} | (no measured cell for {mx.per_day} a day) | {mx.per_day} | | | |")
                continue
            pt = cell.per_trip_charges
            ticks = pt / (LOT * TICK_INR)
            for name, n in mx.parts:
                trips = Decimal(n * MONTH_SESSIONS)
                add(f"| {mx.name} | {name} | {n} | {trips:.0f} | {_r(trips * pt)} | {ticks:.1f} |")
            trips = Decimal(mx.per_day * MONTH_SESSIONS)
            tot = trips * pt + econ.fixed_monthly
            add(f"| {mx.name} | **total** | **{mx.per_day}** | **{trips:.0f}** | **{_r(trips * pt)}** | |")
            notes.append(
                f"- {mx.name}: charges {_r(trips * pt)} + fixed {_r(econ.fixed_monthly)} = {_r(tot)} a month "
                f"({_pct(tot / nav)} of NAV); each round trip must average {_r(tot / trips)} gross to break "
                f"even; per-trade budget {_r(per_trade_frac(mx.per_day) * nav)}, mean premium "
                f"{_r(cell.mean_premium, 2)}."
            )
        add("")
        L.extend(notes)
    add("")
    add("## Recommendation (plain)")
    add("")
    add(
        "The most entries a day (of those tested) at which charges plus fixed costs stay within a share of NAV a "
        f"month AND the break-even gross per trade stays within {BE_OVER_R_OK} R (both bars ASSUMED):"
    )
    add("")
    add("| NAV | " + " | ".join(f"costs <= {_pct(x, 0)} of NAV" for x in COST_SHARES) + " |")
    add("|---:|" + ":---:|" * len(COST_SHARES))
    for nav in cfg.navs:
        ks = [recommend(cells, econ, nav, x) for x in COST_SHARES]
        add(f"| {_r(nav)} | " + " | ".join("none" if k is None else f"up to {k} a day" for k in ks) + " |")
    return "\n".join(L) + "\n"


def load_economics(configs: Path) -> Economics:
    from project100c.economics import load_economics_config
    from project100c.economics.model import fixed_total, monthly_fixed_costs

    cfg = load_economics_config(configs / "economics" / "economics.toml")
    return Economics(fixed_total(monthly_fixed_costs(cfg)), cfg.tax.effective_rate, cfg.config_version)

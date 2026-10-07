"""K-14 daily owner report (docs/architecture/observability.md §17.4) rendered from a paper run, with the
docs/risk/system-economics.md economics section.

Every field of §17.4 is present. A field the system cannot fill yet says so in plain words rather than being
left out: "not built" for a missing component, and "n/a" for a field that does not apply to a SIMULATED run.
The economics section is ``economics.render_markdown`` on the run's own journal (net of everything, advisory only).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from project100c.economics import EconomicsConfig
from project100c.economics.advisory import cost_justification
from project100c.economics.fmt import inr
from project100c.economics.journal import trading_days_from_journal
from project100c.economics.model import build_statement
from project100c.economics.report import render_markdown
from project100c.paper.loop import PaperDay, PaperLoop, PaperRun, PaperTrade

NOT_BUILT = "not built yet"


def _pct(x: Decimal) -> str:
    return f"{x * 100:.2f}%"


def _dd(navs: Sequence[Decimal], start: Decimal) -> Decimal:
    """Drawdown of the last NAV from the high-water mark of ``start`` and ``navs`` (a fraction, >= 0)."""
    hwm = max([start, *navs])
    return (hwm - navs[-1]) / hwm if navs and hwm > 0 else Decimal(0)


def _tomorrow(pd: PaperDay) -> str:
    if pd.kills or pd.halts:
        return "HALTED (" + "; ".join([*pd.kills, *pd.halts]) + ")"
    return "NORMAL"


def _risk_of(t: PaperTrade, pd: PaperDay) -> Decimal | None:
    ids = {lg.intent_id for lg in t.legs}
    dec = next((d for d in pd.decisions if d.outcome == "APPROVED" and ids & set(d.intents)), None)
    return None if dec is None else dec.risk_at_stop


def owner_report(run: PaperRun, loop: PaperLoop, econ: EconomicsConfig, *, day: date | None = None) -> str:
    """Markdown owner report for ``day`` (default: the run's last day)."""
    if not run.days:
        raise ValueError("the run has no days")
    days = run.days
    pd = next((d for d in days if d.day == day), None) if day is not None else days[-1]
    if pd is None:
        raise ValueError(f"{day} is not in the run")
    upto = [d for d in days if d.day <= pd.day]
    start_nav = days[0].sod_nav
    navs = [d.eod_nav for d in upto]
    week0 = pd.day - timedelta(days=pd.day.weekday())
    wk = [d for d in upto if d.day >= week0]
    mo = [d for d in upto if (d.day.year, d.day.month) == (pd.day.year, pd.day.month)]
    gross = pd.realised + pd.charges
    L: list[str] = []
    add = L.append
    add(f"# Daily owner report: {pd.day:%a %d-%b-%Y} (SIMULATED)")
    add("")
    add("Labels: " + "; ".join(run.labels) + ".")
    add("")
    add("## Summary")
    add("")
    add("| Field | Value |")
    add("|:---|:---|")
    add(f"| Date | {pd.day.isoformat()} |")
    add(f"| Mode | {run.stage.value} (paper loop on a SIMULATED feed) |")
    add(f"| Tomorrow | {_tomorrow(pd)} |")
    add(f"| NAV | {inr(pd.eod_nav)} (start of day {inr(pd.sod_nav)}) |")
    add(f"| Day P&L | net {inr(pd.realised)}; gross {inr(gross)}; charges {inr(pd.charges)}; slippage n/a "
        "(fake-broker fills on ASSUMED quotes; no slippage is measured) |")  # fmt: skip
    add(f"| Cumulative P&L | {inr(pd.eod_nav - start_nav)} since {days[0].day.isoformat()} |")
    add(f"| DD from HWM | {_pct(_dd(navs, start_nav))} |")
    add(f"| Week / Month DD | {_pct(_dd([d.eod_nav for d in wk], wk[0].sod_nav))} / "
        f"{_pct(_dd([d.eod_nav for d in mo], mo[0].sod_nav))} |")  # fmt: skip
    add(f"| Config versions | {', '.join(f'{k} {v}' for k, v in sorted(run.config_versions.items()))} |")
    add(f"| Journal | {'hash chain verified' if run.journal_verified else 'VERIFY FAILED'} |")
    add("")
    filled = [t for t in pd.trades if t.filled]
    add(f"## Trades: {len(filled)}")
    add("")
    if filled:
        add("| Strategy | Contract | Entry | Exit reason | Net ₹ | R | Slippage ticks | Attribution |")
        add("|:---|:---|---:|:---|---:|---:|---:|:---|")
        for t in filled:
            risk = _risk_of(t, pd)
            r = f"{t.net_pnl / risk:.2f}" if risk else "n/a"
            legs = [lg for lg in t.legs if lg.entry is not None]
            add(f"| {t.strategy_id} | {' + '.join(lg.contract.trading_symbol for lg in legs)} | "
                f"{' + '.join(str(lg.entry) for lg in legs)} | {t.exit_reason} | {inr(t.net_pnl)} | {r} | n/a | "
                f"UNATTRIBUTED ({NOT_BUILT}) |")  # fmt: skip
    else:
        add("No filled trades.")
    add("")
    add("## Active strategies and lifecycle changes")
    add("")
    add(f"- Rehearsed at {run.stage.value}: {', '.join(run.strategies)}. Lifecycle changes: none (the paper loop "
        "never promotes or demotes).")  # fmt: skip
    add("")
    add("## Best / worst attribution")
    add("")
    add(f"- {NOT_BUILT}: the attribution engine (STRATEGY / EXECUTION / RISK / DATA / REGIME / RANDOM_VARIANCE).")
    if filled:
        best = max(filled, key=lambda t: t.net_pnl)
        worst = min(filled, key=lambda t: t.net_pnl)
        add(f"- Best trade {best.strategy_id} {inr(best.net_pnl)}; worst {worst.strategy_id} {inr(worst.net_pnl)}.")
    add("")
    add("## Confidence per live strategy")
    add("")
    add(
        "- n/a: no strategy is live. "
        "SYNTHETIC data can never reach VALIDATED (validation toolkit, docs/research/validation.md)."
    )
    add("")
    add("## Execution quality")
    add("")
    approved = [d for d in pd.decisions if d.outcome == "APPROVED"]
    rate = f"{len(filled)}/{len(approved)} approved entries filled" if approved else "no approved entries"
    add(f"- Fill rate: {rate}; orders sent {pd.orders_sent}; fills {pd.fills}.")
    add("- Slippage vs assumption: n/a (SIMULATED fills). Latency p50/p99: n/a (in-process fake broker).")
    add("")
    add("## Risk incidents and kill-switch events")
    add("")
    refused = Counter(r.split(":")[0] for d in pd.decisions if d.outcome == "GOVERNOR_REJECTED" for r in d.reasons[:1])
    alloc = Counter(r.split(":")[0] for d in pd.decisions if d.outcome == "ALLOCATOR_REFUSED" for r in d.reasons[:1])
    add(f"- Kills: {'; '.join(pd.kills) or 'none'}. Halts: {'; '.join(pd.halts) or 'none'}.")
    add(f"- Urgent alerts: {'; '.join(pd.urgent_alerts) or 'none'}.")
    add(f"- Governor refusals: {', '.join(f'{k} {v}' for k, v in sorted(refused.items())) or 'none'}. "
        f"Allocator refusals: {', '.join(f'{k} {v}' for k, v in sorted(alloc.items())) or 'none'}.")  # fmt: skip
    add(f"- Flat at the end of the day: {'yes' if pd.flat_at_end else 'NO'}. Event day: "
        f"{'yes' if pd.event_day else 'no'}.")  # fmt: skip
    add("")
    add("## Data quality summary")
    add("")
    add("- SYNTHETIC feed: the DQ monitor does not run on the paper loop's synthetic bars.")
    add("")
    add("## Research findings")
    add("")
    add("- None reported. Anything a research agent finds is listed here as NOT YET VALIDATED.")
    add("")
    add("## Infrastructure costs and economics (docs/risk/system-economics.md)")
    add("")
    tdays = [x for x in trading_days_from_journal(loop.journal, loop.k.costs, loop.k.plan_id, simulated=True)
             if x.day <= pd.day]  # fmt: skip
    st = build_statement(tdays, econ, through=pd.day)
    add(render_markdown(st, cost_justification(st, econ)).rstrip("\n"))
    add("")
    add("## Regulatory watch")
    add("")
    add(f"- Changes detected: n/a ({NOT_BUILT}: no regulatory feed is monitored).")
    return "\n".join(L) + "\n"

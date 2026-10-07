"""K-14 owner report (docs/architecture/observability.md §17.4) from a SIMULATED paper run, with the
docs/risk/system-economics.md economics section."""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pytest

from project100c.economics import load_economics_config
from project100c.paper import PaperLoop, PaperRun, owner_report
from tests.data.dhan_fakes import CONFIGS
from tests.paper.test_loop import LIBRARY, NAV, kernel
from tests.strategies.library_rig import spec
from tests.strategies.test_library import SCENARIOS

ECON = load_economics_config(CONFIGS / "economics" / "economics.toml")
SECTIONS = (
    "## Summary", "| Date |", "| Mode | PAPER", "| Tomorrow |", "| NAV |", "| Day P&L | net ", "gross ", "charges ",
    "slippage", "| Cumulative P&L |", "| DD from HWM |", "| Week / Month DD |", "## Trades:",
    "## Active strategies and lifecycle changes", "## Best / worst attribution", "## Confidence per live strategy",
    "## Execution quality", "Latency p50/p99", "## Risk incidents and kill-switch events", "## Data quality summary",
    "## Research findings", "## Infrastructure costs and economics (docs/risk/system-economics.md)",
    "## Regulatory watch",
)  # fmt: skip


def two_days(tmp: Path) -> tuple[PaperLoop, PaperRun]:
    plans = [dataclasses.replace(SCENARIOS["gap-go"][1][0], day=date(2026, 10, 8)),
             dataclasses.replace(SCENARIOS["vix-straddle"][1][0], day=date(2026, 10, 9))]  # fmt: skip
    lp = PaperLoop(kernel(), [spec(i) for i in LIBRARY], nav=NAV, workdir=tmp)
    return lp, lp.run(plans)


def test_every_section_17_4_field_is_present_with_the_labels(tmp_path: Path) -> None:
    lp, run = two_days(tmp_path)
    md = owner_report(run, lp, ECON)
    for s in SECTIONS:
        assert s in md, s
    assert md.startswith("# Daily owner report: Fri 09-Oct-2026 (SIMULATED)")
    for lab in run.labels:
        assert lab in md
    # docs/risk/system-economics.md, via economics.render_markdown
    assert "### Economics: net of everything (SIMULATED)" in md
    assert "hash chain verified" in md and "never promotes" in md and "NOT YET VALIDATED" in md


def test_the_numbers_tie_to_the_run(tmp_path: Path) -> None:
    lp, run = two_days(tmp_path)
    d0, d1 = run.days
    md = owner_report(run, lp, ECON, day=d0.day)
    assert md.startswith("# Daily owner report: Thu 08-Oct-2026")
    filled = [t for t in d0.trades if t.filled]
    assert f"## Trades: {len(filled)}" in md
    for t in filled:
        assert t.strategy_id in md
    from project100c.economics.fmt import inr

    assert f"| NAV | {inr(d0.eod_nav)}" in md and f"net {inr(d0.realised)}" in md
    md1 = owner_report(run, lp, ECON)
    assert f"| Cumulative P&L | {inr(d1.eod_nav - d0.sod_nav)}" in md1
    hwm = max(NAV, d0.eod_nav, d1.eod_nav)
    assert f"| DD from HWM | {(hwm - d1.eod_nav) / hwm * 100:.2f}%" in md1
    assert ("Tomorrow | NORMAL" in md1) == (not d1.kills and not d1.halts)


def test_unknown_day_is_refused(tmp_path: Path) -> None:
    lp, run = two_days(tmp_path)
    with pytest.raises(ValueError):
        owner_report(run, lp, ECON, day=date(2026, 10, 12))

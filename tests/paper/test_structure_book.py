"""The multi-leg PAPER structure book (OD-019): research/paper only, hard-locked against live, OD-002 gated."""

from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path

import pytest

from project100c.errors import ConfigError
from project100c.paper import structures as ps_mod
from project100c.paper.structures import (
    PaperStructureConfig,
    StructurePaperBook,
    StructurePaperRefused,
    load_paper_structure_config,
)
from project100c.sessions import IST
from project100c.spec.structure import load_structure_file

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "project100c"
SPECS = REPO / "examples" / "structures"  # SYNTHETIC examples
EXP = load_structure_file(SPECS / "X-DEMO-SPREAD-001.yaml")
HSS = load_structure_file(SPECS / "X-DEMO-STRANGLE-001.yaml")
THU = date(2026, 10, 6)  # an expiry Tuesday
CFG = PaperStructureConfig("PS-TEST", False, "", ("X-DEMO-SPREAD-001-E", "X-DEMO-STRANGLE-001-O"))


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def prices(ce0: float, ce2: float) -> dict[tuple[str, float], float]:
    return {("CE", 25050.0): ce0, ("CE", 25150.0): ce2}  # the demo spread: sell +1 strike, buy +3


def book(cfg: PaperStructureConfig = CFG) -> StructurePaperBook:
    return StructurePaperBook(cfg, tick=0.05, slippage_ticks=2)


def open_exp(b: StructurePaperBook, when: datetime | None = None):  # type: ignore[no-untyped-def]
    return b.open(
        EXP, "E", when or at(THU, 10, 0), atm=25000.0, step=50.0, expiry=THU, lot=75, prices=prices(100.0, 40.0)
    )


def test_the_example_config_is_paper_only_and_refuses_overnight() -> None:
    cfg = load_paper_structure_config(REPO / "examples" / "configs" / "paper_structures.example.toml")
    # the example refuses overnight PAPER holds (no exception recorded)
    assert cfg.od002_paper_exception is False and cfg.od002_decision_ref == ""
    assert cfg.enabled == ("X-DEMO-SPREAD-001-E", "X-DEMO-STRANGLE-001-O")
    assert ps_mod.LIVE_ALLOWED is False


def test_config_rejects_live_and_an_exception_without_a_decision(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('version = "x"\nvenue = "LIVE"\n')
    with pytest.raises(ConfigError, match="PAPER"):
        load_paper_structure_config(p)
    p.write_text('version = "x"\nvenue = "PAPER"\nod002_paper_exception = true\nod002_decision_ref = ""\n')
    with pytest.raises(ConfigError, match="decision"):
        load_paper_structure_config(p)


def test_the_book_refuses_any_non_paper_venue() -> None:
    with pytest.raises(StructurePaperRefused, match="PAPER only"):
        StructurePaperBook(CFG, venue="LIVE")


def test_an_overnight_structure_needs_the_od002_paper_exception() -> None:
    b = book()
    kw = dict(atm=25000.0, step=50.0, expiry=THU, lot=75)
    px = {(r, 25000.0 + o * 50): 50.0 for r in ("CE", "PE") for o in range(-10, 11)}
    with pytest.raises(StructurePaperRefused, match="OD-002"):
        b.open(HSS, "O", at(date(2026, 10, 1), 13, 30), prices=px, **kw)  # type: ignore[arg-type]
    ok = book(PaperStructureConfig("PS-TEST", True, "OD-002-PAPER-TEST", CFG.enabled))
    got = ok.open(HSS, "O", at(date(2026, 10, 1), 13, 30), prices=px, **kw)  # type: ignore[arg-type]
    assert got.variant.holds_overnight and got.max_loss_inr > 0


def test_scope_window_enablement_and_one_per_variant() -> None:
    b = book(PaperStructureConfig("PS-TEST", False, "", ()))
    with pytest.raises(StructurePaperRefused, match="not enabled"):
        open_exp(b)
    b = book()
    with pytest.raises(StructurePaperRefused, match="09:20-14:00"):
        open_exp(b, at(THU, 9, 16))
    with pytest.raises(StructurePaperRefused, match="non-expiry"):
        open_exp(b, at(date(2026, 10, 5), 10, 0))
    open_exp(b)
    with pytest.raises(StructurePaperRefused, match="already open"):
        open_exp(b)


def test_fills_credit_and_time_exit_pnl() -> None:
    b = book()
    s = open_exp(b)
    # sold at 100 - 2 ticks, bought at 40 + 2 ticks: credit = 99.9 - 40.1 = 59.8 points
    assert [lg.entry for lg in s.legs] == [pytest.approx(99.9), pytest.approx(40.1)]
    assert s.credit == pytest.approx(59.8)
    assert s.max_loss_inr == pytest.approx((100 - 59.8) * 75)  # width minus credit, x lot (no charges here)
    assert b.due_exits(at(THU, 12, 0), prices(90.0, 35.0)) == {}
    assert b.due_exits(at(THU, 14, 30), prices(60.0, 20.0)) == {s.name: "TIME_EXIT"}
    done = b.close(s.name, at(THU, 14, 30), prices(60.0, 20.0), "TIME_EXIT")
    # buy back at 60.1, sell at 19.9: gross = (99.9 - 60.1 + 19.9 - 40.1) * 75
    assert done.net_inr == pytest.approx((99.9 - 60.1 + 19.9 - 40.1) * 75)
    assert done.status == "CLOSED" and not b.open_positions and all(e["sim"] for e in done.events)


def test_a_spread_widens_both_fills() -> None:
    b = book()
    s = b.open(
        EXP,
        "E",
        at(THU, 10, 0),
        atm=25000.0,
        step=50.0,
        expiry=THU,
        lot=75,
        prices=prices(100.0, 40.0),
        half_spread=lambda r, k, p: 0.5,
    )
    assert s.credit == pytest.approx((100 - 0.5 - 0.1) - (40 + 0.5 + 0.1))


def test_an_intraday_structure_is_never_carried_overnight() -> None:
    b = book()
    s = open_exp(b)
    with pytest.raises(StructurePaperRefused, match="OD-002"):
        b.due_exits(at(date(2026, 10, 7), 9, 15), prices(1.0, 0.5))
    assert s.name in b.open_positions


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_architecture_no_live_path_reaches_the_structure_book() -> None:
    # ops holds the host (paper and live composition roots)
    own = _imports(SRC / "paper" / "structures.py")
    for banned in ("project100c.kernel", "project100c.broker", "project100c.execution", "project100c.ops"):
        assert not any(m == banned or m.startswith(banned + ".") for m in own), banned
    live = [
        SRC / p for p in ("kernel", "broker", "execution", "ops", "portfolio", "strategies", "recorder")
    ]  # ops = host
    files = [f for d in live if d.exists() for f in d.rglob("*.py")]
    files += [SRC / "paper" / "loop.py", SRC / "paper" / "live.py", SRC / "paper" / "__init__.py"]
    assert files
    for f in files:
        assert "project100c.paper.structures" not in _imports(f), f
        assert "StructurePaperBook" not in f.read_text(), f

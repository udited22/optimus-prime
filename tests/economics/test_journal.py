"""Trading days from the K-01 journal: repriced once per executed order, flat days only, no silent fixes."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from project100c.costs import CostModel, Side
from project100c.economics import EconomicsConfig, build_statement, trading_days_from_journal
from project100c.errors import EconomicsError
from project100c.journal import Journal
from project100c.observability.dashboard.simulator import REPLAY_SCENARIOS, Kernel, SimulatedSession

PLAN = "upstox-options"  # OD-004 live broker, Rs 20 per executed order
KEY = "NSE_FO|NIFTY-2026-10-06-25000-CE"


@dataclass
class Clock:
    t: datetime

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def jr(tmp_path: Path) -> Iterator[tuple[Journal, Clock]]:
    clock = Clock(datetime(2026, 10, 5, 4, 0, tzinfo=UTC))  # 09:30 IST
    j = Journal(tmp_path / "j.sqlite", clock=clock)
    yield j, clock
    j.close()


def start(j: Journal, d: date, nav: str = "10000") -> None:
    j.append("DAY_START", {"trading_date": d, "sod_nav": D(nav), "week_start": d})


def order(j: Journal, cid: str, side: str, qty: int, key: str = KEY) -> None:
    j.append(
        "ORDER_SUBMITTED",
        {
            "client_order_id": cid,
            "strategy_id": "S-ORB-001",
            "instrument_key": key,
            "side": side,
            "qty": qty,
            "price": D("100"),
            "kind": "ENTRY",
            "lot_size": 65,
        },
    )


def fill(j: Journal, cid: str, n: int, qty: int, px: str, charges: str = "25") -> None:
    j.append(
        "FILL", {"client_order_id": cid, "trade_id": f"{cid}-{n}", "qty": qty, "price": D(px), "charges": D(charges)}
    )


def test_round_trip_with_partial_fills_charges_brokerage_once_per_order(
    jr: tuple[Journal, Clock], costs: CostModel
) -> None:
    j, _ = jr
    start(j, date(2026, 10, 5))
    order(j, "B1", "BUY", 130)
    fill(j, "B1", 1, 65, "100")
    fill(j, "B1", 2, 65, "102")  # VWAP 101
    order(j, "S1", "SELL", 130)
    fill(j, "S1", 1, 130, "110")
    order(j, "B2", "BUY", 65)  # never filled: no charge, not counted
    (d,) = trading_days_from_journal(j, costs, PLAN, simulated=True)
    assert d.gross_pnl == D(130) * D(110) - (D(65) * 100 + D(65) * 102) == D(1170)
    buy = costs.order_charges(Side.BUY, D(101), 130, date(2026, 10, 5), PLAN)
    sell = costs.order_charges(Side.SELL, D(110), 130, date(2026, 10, 5), PLAN)
    assert d.charges.total == buy.total + sell.total
    assert d.charges.brokerage == D(40)  # Rs 20 x 2 executed orders, not x 3 fills
    assert d.executed_orders == 2 and d.round_trips == 1 and d.simulated
    assert d.recorded_charges == D(75)  # the kernel's per-fill figure is kept for comparison
    assert d.abs_trade_pnl == D(1170) and d.opening_nav == D(10000)


def test_one_trading_day_per_day_start_including_idle_days(jr: tuple[Journal, Clock], costs: CostModel) -> None:
    j, clock = jr
    start(j, date(2026, 10, 5))
    clock.t += timedelta(days=1)
    start(j, date(2026, 10, 6), "10050")
    order(j, "B1", "BUY", 65)
    fill(j, "B1", 1, 65, "100")
    order(j, "S1", "SELL", 65)
    fill(j, "S1", 1, 65, "90")
    days = trading_days_from_journal(j, costs, PLAN, simulated=False)
    assert [x.day for x in days] == [date(2026, 10, 5), date(2026, 10, 6)]
    assert days[0].gross_pnl == 0 and days[0].executed_orders == 0
    assert days[1].gross_pnl == D(-650) and days[1].opening_nav == D(10050)


def test_open_position_at_day_end_raises(jr: tuple[Journal, Clock], costs: CostModel) -> None:
    j, _ = jr
    start(j, date(2026, 10, 5))
    order(j, "B1", "BUY", 65)
    fill(j, "B1", 1, 65, "100")
    with pytest.raises(EconomicsError, match="not flat"):
        trading_days_from_journal(j, costs, PLAN, simulated=True)


def test_order_filling_across_days_raises(jr: tuple[Journal, Clock], costs: CostModel) -> None:
    j, clock = jr
    start(j, date(2026, 10, 5))
    order(j, "B1", "BUY", 130)
    fill(j, "B1", 1, 65, "100")
    clock.t += timedelta(days=1)
    fill(j, "B1", 2, 65, "100")
    with pytest.raises(EconomicsError, match="fills on"):
        trading_days_from_journal(j, costs, PLAN, simulated=True)


Step = tuple[str, tuple[object, ...]]


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        ([("fill", ("X", 1, 65, "100"))], "not submitted"),
        ([("order", ("S1", "SELL", 65)), ("fill", ("S1", 1, 65, "100"))], "net short"),
        ([("order", ("B1", "BUY", 65)), ("order", ("B1", "BUY", 65))], "duplicate"),
        ([("order", ("B1", "BUY", 65)), ("fill", ("B1", 1, 0, "100"))], "> 0"),
        ([("adopt", ())], "adopted"),
    ],
)
def test_inconsistent_journals_raise(
    jr: tuple[Journal, Clock], costs: CostModel, steps: list[Step], match: str
) -> None:
    j, _ = jr
    start(j, date(2026, 10, 5))
    for kind, args in steps:
        if kind == "order":
            order(j, *args)  # type: ignore[arg-type]
        elif kind == "fill":
            fill(j, *args)  # type: ignore[arg-type]
        else:
            j.append("POSITION_ADOPTED", {"instrument_key": KEY, "qty": 65})
    with pytest.raises(EconomicsError, match=match):
        trading_days_from_journal(j, costs, PLAN, simulated=True)


def test_order_before_day_start_raises(jr: tuple[Journal, Clock], costs: CostModel) -> None:
    j, _ = jr
    order(j, "B1", "BUY", 65)
    with pytest.raises(EconomicsError, match="before any DAY_START"):
        trading_days_from_journal(j, costs, PLAN, simulated=True)


def test_wrong_payload_types_raise(jr: tuple[Journal, Clock], costs: CostModel) -> None:
    j, _ = jr
    j.append("DAY_START", {"trading_date": "2026-10-05", "sod_nav": D(1), "week_start": date(2026, 10, 5)})
    with pytest.raises(EconomicsError, match="date"):
        trading_days_from_journal(j, costs, PLAN, simulated=True)


def test_the_real_kernel_journal_reconciles(cfg: EconomicsConfig) -> None:
    """End to end: a SIMULATED day through the real Risk Governor + kernel runtime + fake broker. The adapter's
    gross P&L less the kernel's own recorded charges equals the kernel's realised P&L, and repricing per order is
    never dearer than the kernel's per-fill charging."""
    kernel = Kernel.load()
    with tempfile.TemporaryDirectory() as d:
        sess = SimulatedSession(REPLAY_SCENARIOS[0], kernel, Path(d), step_s=15)
        for _ in sess.run():
            pass
        days = trading_days_from_journal(sess.journal, kernel.costs, kernel.plan_id, simulated=True)
        realised = sess.rt.state.realised_today
        sess.journal.close()
    (td,) = days
    assert td.recorded_charges is not None
    assert td.day == REPLAY_SCENARIOS[0].day and td.executed_orders >= 2 and td.round_trips >= 1
    assert abs(td.gross_pnl - td.recorded_charges - realised) < D("1e-9")
    assert td.charges.total <= td.recorded_charges
    st = build_statement(days, cfg)
    assert st.simulated and any("SIMULATED" in x for x in st.labels)

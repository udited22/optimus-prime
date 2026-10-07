"""B-01 acceptance: deterministic ledger hash; look-ahead canary fails as expected; latency delays visibility;
kernel rules (long-only, trading window, 1 lot) enforced; engine-owned 14:50 flatten; costs on every fill."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from project100c.backtest import (
    BacktestEngine,
    BarEvent,
    CancelOrder,
    LatencyModel,
    MarketView,
    PlaceOrder,
    ReplayFeed,
    SimOrderType,
    StrategyContext,
)
from project100c.backtest.engine import FLATTEN_TAG
from project100c.backtest.types import Intent
from project100c.core_types import OptionRight, OrderSide
from project100c.costs import CostModel, Side
from project100c.errors import BacktestError, LookAheadError
from tests.backtest.conftest import DAY, KEY, ONE_MIN, Scripted, at, day_bars

D = Decimal
Factory = Callable[..., BacktestEngine]


def buy(lim: str = "100", qty: int = 65) -> PlaceOrder:
    return PlaceOrder(KEY, "NIFTY", OptionRight.CE, OrderSide.BUY, qty, 65, SimOrderType.LIMIT, D(lim))


def sell(
    lim: str = "100", qty: int = 65, typ: SimOrderType = SimOrderType.LIMIT, trig: str | None = None
) -> PlaceOrder:
    return PlaceOrder(
        KEY, "NIFTY", OptionRight.CE, OrderSide.SELL, qty, 65, typ, D(lim), None if trig is None else D(trig)
    )


def feed(**kw: object) -> ReplayFeed:
    return ReplayFeed.from_bars(day_bars(**kw), interval=ONE_MIN)  # type: ignore[arg-type]


def test_round_trip_costs_and_determinism(engine_factory: Factory, costs: CostModel) -> None:
    script = {at(10, 1): [buy("100")], at(10, 30): [sell("100")]}
    r1 = engine_factory().run(feed(), Scripted(dict(script)))
    r2 = engine_factory().run(feed(), Scripted(dict(script)))
    assert r1.ledger_hash == r2.ledger_hash  # B-01 AT: identical ledger hash on re-run
    assert [f.side for f in r1.fills] == [OrderSide.BUY, OrderSide.SELL] and r1.flat_at_end
    assert r1.fills[0].ts == at(10, 3)  # order live 10:01:00.3 -> first eligible bar 10:02, filled at its end
    rt = (
        costs.order_charges(Side.BUY, D("100"), 65, DAY, "upstox-options").total
        + costs.order_charges(Side.SELL, D("100"), 65, DAY, "upstox-options").total
    )
    assert r1.total_charges == rt and r1.net_pnl == -rt
    r3 = engine_factory(LatencyModel(order_to_exchange=timedelta(seconds=2))).run(feed(), Scripted(dict(script)))
    assert r3.ledger_hash != r1.ledger_hash


def test_latency_delays_visibility(engine_factory: Factory) -> None:
    fast, slow = Scripted({at(10, 1): [buy()]}), Scripted({at(10, 1): [buy()]})
    engine_factory(LatencyModel(report_to_strategy=timedelta(0))).run(feed(), fast)
    engine_factory(LatencyModel(report_to_strategy=timedelta(seconds=90))).run(feed(), slow)

    def first_seen(s: Scripted) -> object:
        return next(t for t, n in s.seen_fills_at if n > 0)

    assert first_seen(fast) == at(10, 3)
    assert first_seen(slow) == at(10, 5)  # fill at 10:03 + 90 s -> visible from the 10:05 event


class Peeker:
    name = "lookahead-canary"

    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
        view.bars_between(KEY, view.now - timedelta(minutes=5), view.now + ONE_MIN)  # peeks one bar ahead
        return ()


class Honest:
    name = "honest"

    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
        bars = view.bars_between(KEY, view.now - timedelta(minutes=5), view.now)
        assert all(b.start + ONE_MIN <= view.now for b in bars)
        return ()


def test_lookahead_canary_fails_as_expected(engine_factory: Factory) -> None:
    with pytest.raises(LookAheadError):
        engine_factory().run(feed(), Peeker())
    engine_factory().run(feed(), Honest())  # the same access pattern without peeking is fine


def test_feed_cannot_publish_a_bar_before_it_closes() -> None:
    b = day_bars()[0]
    with pytest.raises(LookAheadError):
        BarEvent(b, ONE_MIN, b.start)  # available at its START = look-ahead
    with pytest.raises(BacktestError):
        ReplayFeed.from_bars([b], interval=ONE_MIN, feed_latency=-ONE_MIN)


@pytest.mark.parametrize(
    ("when", "intent", "reason"),
    [
        ((9, 17), buy(), "ENTRY_WINDOW_CLOSED"),  # before 09:20 (OD-009)
        ((14, 0), buy(), "ENTRY_WINDOW_CLOSED"),  # at the 14:00 cutoff (OD-008)
        ((15, 5), buy(), "OUTSIDE_ORDER_WINDOW"),  # after 15:00 (OD-002)
        ((10, 1), sell(), "MANDATE_LONG_ONLY"),  # sell-to-open (OD-006)
        ((10, 1), buy(qty=130), "MAX_LOTS"),  # 1 lot
        ((10, 1), buy("100.03"), "OFF_TICK"),
    ],
)
def test_kernel_rules_reject(engine_factory: Factory, when: tuple[int, int], intent: PlaceOrder, reason: str) -> None:
    r = engine_factory().run(feed(), Scripted({at(*when): [intent]}))
    rej = [e for e in r.ledger.entries if e["kind"] == "ORDER_REJECTED"]
    assert r.rejects == 1 and reason in rej[0]["reasons"] and r.fills == ()


def test_second_lot_rejected_while_first_pending(engine_factory: Factory) -> None:
    r = engine_factory().run(feed(), Scripted({at(10, 1): [buy(), buy()]}))
    assert r.rejects == 1 and [f.side for f in r.fills] == [OrderSide.BUY, OrderSide.SELL]


def test_engine_flattens_at_1450(engine_factory: Factory) -> None:
    r = engine_factory().run(feed(), Scripted({at(10, 1): [buy()]}))
    assert r.flat_at_end
    last = r.fills[-1]
    assert last.tag == FLATTEN_TAG and last.side is OrderSide.SELL and last.price == D("90")  # 100 x 0.9
    assert at(14, 50) <= last.ts < at(15, 0)


def test_residual_recorded_when_flatten_cannot_fill(engine_factory: Factory) -> None:
    f = feed(volume=lambda i: 0 if i >= 5 * 60 else 6500)  # no volume from 14:15: nothing can fill
    r = engine_factory().run(f, Scripted({at(10, 1): [buy()]}))
    assert not r.flat_at_end and "RESIDUAL_AT_CLOSE" in r.ledger.kinds()


def test_protective_stop_gap_then_flatten(engine_factory: Factory) -> None:
    def px(i: int) -> Decimal:
        return D("100") if i < 60 else D("85")  # gap down at 10:15 through the stop band

    script = {at(10, 1): [buy()], at(10, 5): [sell("94", typ=SimOrderType.SL_LIMIT, trig="95")]}
    r = engine_factory().run(feed(price=px), Scripted(script))
    kinds = r.ledger.kinds()
    assert "TRIGGERED" in kinds  # the stop triggered on the gap ...
    assert [f.tag for f in r.fills] == ["", FLATTEN_TAG]  # ... did not fill below its limit; flatten exited
    assert r.flat_at_end and r.fills[-1].price == D("76.50")  # floor_tick(85 x 0.9)


def test_cancel_before_fill(engine_factory: Factory) -> None:
    r = engine_factory().run(
        feed(),
        Scripted({at(10, 1): [buy("50")], at(10, 5): [CancelOrder("O000000")], at(10, 6): [CancelOrder("nope")]}),
    )
    kinds = r.ledger.kinds()
    assert "CANCELLED" in kinds and "CANCEL_REJECTED" in kinds and r.fills == ()


class _BuyThenCancel:
    name = "buy-then-cancel"

    def __init__(self) -> None:
        self.sent = False
        self.cancelled = False

    def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
        if ctx.now == at(10, 1) and not self.sent:
            self.sent = True
            spec = PlaceOrder(
                KEY, "NIFTY", OptionRight.CE, OrderSide.BUY, 65, 65, SimOrderType.LIMIT, D("100"), None, "E1"
            )
            return [spec]
        if ctx.now == at(10, 2) and not self.cancelled:
            self.cancelled = True
            return [CancelOrder(ctx.accepted["E1"])]  # placement ack gives the id synchronously
        return ()


def test_cancel_effective_inside_a_bar_blocks_that_bar(engine_factory: Factory) -> None:
    """Order live from 10:01:00.3; cancel sent 10:02:00 takes effect 10:02:00.3, inside the 10:02 bar, which
    trades through the limit. Intra-bar timing is unknown, so the conservative engine does not fill it."""
    r = engine_factory().run(feed(price=lambda i: D("100")), _BuyThenCancel())
    assert not r.fills
    assert [e["kind"] for e in r.ledger.entries if e["kind"] in ("CANCEL_SENT", "CANCELLED")] == [
        "CANCEL_SENT",
        "CANCELLED",
    ]


def test_rejected_tags_are_reported_to_the_strategy(engine_factory: Factory) -> None:
    class Late:
        name = "late"
        ctx: StrategyContext | None = None

        def on_event(self, view: MarketView, ctx: StrategyContext) -> Sequence[Intent]:
            self.ctx = ctx
            if ctx.now == at(14, 1):
                return [
                    PlaceOrder(
                        KEY, "NIFTY", OptionRight.CE, OrderSide.BUY, 65, 65, SimOrderType.LIMIT, D("100"), None, "L1"
                    )
                ]
            return ()

    s = Late()
    engine_factory().run(feed(), s)
    assert s.ctx is not None and s.ctx.rejected["L1"] == ["ENTRY_WINDOW_CLOSED"] and "L1" not in s.ctx.accepted


def test_decision_keys_come_last_within_an_instant() -> None:
    from datetime import datetime

    from project100c.market_types import Bar
    from project100c.sessions.model import IST

    t0 = datetime(2026, 8, 3, 9, 15, tzinfo=IST)

    def mk(k: str, m: int) -> Bar:
        return Bar(k, t0 + timedelta(minutes=m), Decimal(1), Decimal(1), Decimal(1), Decimal(1), 0)

    bars = [mk("NIFTY-INDEX", 0), mk("NIFTY|2026-08-04|24000|CE", 0), mk("INDIA-VIX", 0), mk("NIFTY-INDEX", 1)]
    plain = [e.key for e in ReplayFeed.from_bars(bars, interval=ONE_MIN)]
    assert plain == ["INDIA-VIX", "NIFTY-INDEX", "NIFTY|2026-08-04|24000|CE", "NIFTY-INDEX"]  # the old order: by key
    fixed = [e.key for e in ReplayFeed.from_bars(bars, interval=ONE_MIN, decision_keys=("NIFTY-INDEX",))]
    assert fixed == ["INDIA-VIX", "NIFTY|2026-08-04|24000|CE", "NIFTY-INDEX", "NIFTY-INDEX"]


def test_flatten_never_oversells_a_working_exit(engine_factory: Factory) -> None:
    """Regression (3-Oct-2026, S-NOISE-001 on 10-Jan-2023): another instrument's 14:50 bar arrived first, the forced
    flatten sent a full-size sell while the strategy's exit was still live (its cancel not yet effective), the exit
    then filled on its own 14:49 bar and the flatten sold again: net short. The flatten now waits for the cancels."""
    other = "NSE_FO|00001"  # sorts before KEY, so its bar is processed first at each minute
    bars = [*day_bars(), *day_bars(key=other)]
    script = {at(10, 1): [buy()], at(14, 48): [sell("100")]}
    r = engine_factory().run(ReplayFeed.from_bars(bars, interval=ONE_MIN), Scripted(script))
    sells = [f for f in r.fills if f.side is OrderSide.SELL]
    assert sum(f.qty for f in sells) == 65 and r.flat_at_end

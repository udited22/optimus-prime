"""Portfolio allocator v1 (S-05): eligibility, the regime refusal, equal risk, auto-decrease and exclusive groups."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from project100c.errors import ConfigError
from project100c.kernel.regime_gate import RegimeReading
from project100c.portfolio import (
    AllocationMode,
    AllocatorConfig,
    Refusal,
    StrategyRecord,
    allocate,
    load_allocator_config,
)
from project100c.sessions.model import IST
from project100c.spec.io import load_spec_file
from project100c.spec.models import Lifecycle, Regime, StrategySpec
from tests.data.dhan_fakes import CONFIGS, REPO

NOW = datetime(2026, 10, 7, 10, 30, tzinfo=IST)
NAV = Decimal(1_000_000)
R = Regime


@pytest.fixture(scope="module")
def cfg() -> AllocatorConfig:
    return load_allocator_config(CONFIGS / "portfolio" / "allocator.toml")


def spec(sid: str, **upd: object) -> StrategySpec:
    s = load_spec_file(REPO / "specs" / f"{sid}.yaml")
    return s.model_copy(update=upd) if upd else s


def reading(*tags: Regime, validated: bool = False, age_s: int = 30) -> RegimeReading:
    return RegimeReading(frozenset(tags), NOW - timedelta(seconds=age_s), "RC-2026-10-02.1", validated)


TREND_UP = (R.TRENDING_UP, R.VOLATILITY_NORMAL)
RANGE = (R.MEAN_REVERTING, R.VOLATILITY_NORMAL)


def codes(plan_alloc: object) -> set[Refusal]:
    return {c for c, _ in plan_alloc.refusals}  # type: ignore[attr-defined]


def test_the_shipped_config_is_versioned_assumed_and_within_od005(cfg: AllocatorConfig) -> None:
    assert cfg.version.startswith("AL-") and cfg.status == "ASSUMED"
    assert cfg.per_trade_frac == Decimal("0.02") and cfg.daily_risk_frac == Decimal("0.04")
    assert ("S-ORB-001", "S-FBO-001") in cfg.exclusive_groups


def test_the_config_cannot_raise_the_per_trade_risk_above_two_percent(cfg: AllocatorConfig, tmp_path: Path) -> None:
    bad = cfg.model_dump() | {"per_trade_frac": "0.03", "adopted_on": cfg.adopted_on}
    with pytest.raises(ValueError, match="OD-005"):
        AllocatorConfig.model_validate(bad)
    p = tmp_path / "a.toml"
    p.write_text('[[allocator]]\nversion = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_allocator_config(p)
    with pytest.raises(ConfigError, match="not found"):
        load_allocator_config(CONFIGS / "portfolio" / "allocator.toml", version="AL-nope")


# ---------------------------------------------------------------------------------------------- regime
def test_a_disallowed_regime_is_refused(cfg: AllocatorConfig) -> None:
    recs = [StrategyRecord(spec("S-FBO-001")), StrategyRecord(spec("S-VWAPC-001"))]
    plan = allocate(recs, nav=NAV, regime=reading(*TREND_UP), now=NOW, cfg=cfg)
    fbo, vwapc = plan.get("S-FBO-001"), plan.get("S-VWAPC-001")
    assert not fbo.eligible and Refusal.REGIME_NOT_ALLOWED in codes(fbo)  # range strategy on a trend day
    assert fbo.risk_budget_inr == 0
    assert vwapc.eligible and vwapc.risk_budget_inr > 0
    plan2 = allocate(recs, nav=NAV, regime=reading(*RANGE), now=NOW, cfg=cfg)
    assert plan2.eligible == ("S-FBO-001",)  # the same two strategies, the other way round


def test_a_missing_required_condition_is_refused(cfg: AllocatorConfig) -> None:
    recs = [StrategyRecord(spec("S-GAPGO-001")), StrategyRecord(spec("S-EXP0-001"))]
    plan = allocate(recs, nav=NAV, regime=reading(R.OPENING_DRIVE, *TREND_UP), now=NOW, cfg=cfg)
    assert plan.eligible == ()  # no gap, not expiry day
    plan = allocate(recs, nav=NAV, regime=reading(R.OPENING_DRIVE, R.GAP_REGIME, *TREND_UP), now=NOW, cfg=cfg)
    assert plan.eligible == ("S-GAPGO-001",)


def test_a_prohibited_regime_blocks_everyone(cfg: AllocatorConfig) -> None:
    recs = [StrategyRecord(spec(s)) for s in ("S-VWAPC-001", "S-IVRV-001", "S-VOLX-001")]
    plan = allocate(recs, nav=NAV, regime=reading(R.ABNORMAL_MARKET, *TREND_UP), now=NOW, cfg=cfg)
    assert plan.eligible == ()
    assert all(Refusal.REGIME_BLOCKED in codes(a) for a in plan.allocations)
    assert plan.total_risk_inr == 0


def test_no_reading_or_a_stale_one_is_refused(cfg: AllocatorConfig) -> None:
    recs = [StrategyRecord(spec("S-VWAPC-001"))]
    assert Refusal.REGIME_UNKNOWN in codes(allocate(recs, nav=NAV, regime=None, now=NOW, cfg=cfg).get("S-VWAPC-001"))
    stale = allocate(recs, nav=NAV, regime=reading(*TREND_UP, age_s=600), now=NOW, cfg=cfg)
    assert Refusal.REGIME_STALE in codes(stale.get("S-VWAPC-001"))


def test_an_unvalidated_classifier_gives_no_live_money(cfg: AllocatorConfig) -> None:
    canary = StrategyRecord(spec("S-VWAPC-001"), status=Lifecycle.CANARY)
    live = allocate([canary], nav=NAV, regime=reading(*TREND_UP), now=NOW, cfg=cfg, mode=AllocationMode.LIVE)
    a = live.get("S-VWAPC-001")
    assert not a.eligible and Refusal.REGIME_BLOCKED in codes(a)
    assert "UNVALIDATED" in a.refusals[-1][1]
    sim = allocate([canary], nav=NAV, regime=reading(*TREND_UP), now=NOW, cfg=cfg)  # simulate: no money at risk
    assert sim.get("S-VWAPC-001").eligible


# ---------------------------------------------------------------------------------------------- eligibility
def test_live_mode_needs_a_live_status_and_known_capital(cfg: AllocatorConfig) -> None:
    rv = reading(*TREND_UP, validated=True)
    research = StrategyRecord(spec("S-VWAPC-001"))
    plan = allocate([research], nav=NAV, regime=rv, now=NOW, cfg=cfg, mode=AllocationMode.LIVE)
    assert codes(plan.get("S-VWAPC-001")) == {Refusal.STATUS_NOT_ELIGIBLE, Refusal.CAPITAL_UNKNOWN}
    sp = spec("S-VWAPC-001")
    rich = sp.model_copy(
        update={"dependencies": sp.dependencies.model_copy(update={"min_capital_inr": Decimal(80_000)})}
    )
    ok = allocate([StrategyRecord(rich, status=Lifecycle.CANARY)], nav=NAV, regime=rv, now=NOW, cfg=cfg,
                  mode=AllocationMode.LIVE)  # fmt: skip
    assert ok.eligible == ("S-VWAPC-001",)
    poor = allocate([StrategyRecord(rich, status=Lifecycle.CANARY)], nav=Decimal(10_000), regime=rv, now=NOW,
                    cfg=cfg, mode=AllocationMode.LIVE)  # fmt: skip
    assert codes(poor.get("S-VWAPC-001")) == {Refusal.CAPITAL_INELIGIBLE}


@pytest.mark.parametrize("status", [Lifecycle.QUARANTINED, Lifecycle.RETIRED])
def test_quarantined_and_retired_never_trade(cfg: AllocatorConfig, status: Lifecycle) -> None:
    plan = allocate([StrategyRecord(spec("S-VWAPC-001"), status=status)], nav=NAV, regime=reading(*TREND_UP),
                    now=NOW, cfg=cfg)  # fmt: skip
    assert codes(plan.get("S-VWAPC-001")) == {Refusal.STATUS_NOT_ELIGIBLE}


def test_a_killed_strategy_gets_nothing(cfg: AllocatorConfig) -> None:
    plan = allocate([StrategyRecord(spec("S-VWAPC-001"), killed=True)], nav=NAV, regime=reading(*TREND_UP), now=NOW,
                    cfg=cfg)  # fmt: skip
    assert codes(plan.get("S-VWAPC-001")) == {Refusal.STRATEGY_KILLED}


# ---------------------------------------------------------------------------------------------- sizing
def test_equal_risk_among_eligible_capped_per_trade(cfg: AllocatorConfig) -> None:
    rd = reading(R.VOLATILITY_NORMAL, R.TRENDING_UP)
    one = allocate([StrategyRecord(spec("S-VWAPC-001"))], nav=NAV, regime=rd, now=NOW, cfg=cfg)
    assert one.get("S-VWAPC-001").risk_budget_inr == Decimal(20_000)  # min(2%, 4% / 1)
    ids = ("S-VWAPC-001", "S-IVRV-001", "S-VOLX-001")
    three = allocate([StrategyRecord(spec(s)) for s in ids], nav=NAV, regime=rd, now=NOW, cfg=cfg)
    assert three.eligible == ids
    budgets = {a.risk_budget_inr for a in three.allocations}
    assert budgets == {Decimal("13333.33")}  # 4% / 3, floored to the paisa
    assert three.total_risk_inr <= cfg.daily_risk_frac * NAV


def test_risk_already_used_today_shrinks_the_share(cfg: AllocatorConfig) -> None:
    rd = reading(*TREND_UP)
    recs = [StrategyRecord(spec("S-VWAPC-001"))]
    part = allocate(recs, nav=NAV, regime=rd, now=NOW, cfg=cfg, daily_risk_used=Decimal(30_000))
    assert part.get("S-VWAPC-001").risk_budget_inr == Decimal(10_000)
    spent = allocate(recs, nav=NAV, regime=rd, now=NOW, cfg=cfg, daily_risk_used=Decimal(40_000))
    assert codes(spent.get("S-VWAPC-001")) == {Refusal.NO_DAILY_RISK_LEFT}


def test_auto_decrease_rules_compound_and_slippage_at_the_kill_multiple_zeroes(cfg: AllocatorConfig) -> None:
    rd = reading(*TREND_UP)
    sp = spec("S-VWAPC-001")
    worn = StrategyRecord(sp, consecutive_losses=3, drawdown_frac=Decimal("0.06"), slippage_multiple=Decimal("1.6"))
    a = allocate([worn], nav=NAV, regime=rd, now=NOW, cfg=cfg).get("S-VWAPC-001")
    assert a.multiplier == Decimal("0.125") and len(a.decreases) == 3
    assert a.risk_budget_inr == Decimal(2_500)
    deg = allocate([StrategyRecord(sp, status=Lifecycle.DEGRADED)], nav=NAV, regime=rd, now=NOW, cfg=cfg)
    assert deg.get("S-VWAPC-001").risk_budget_inr == Decimal(10_000)
    slip = allocate([StrategyRecord(sp, slippage_multiple=Decimal(2))], nav=NAV, regime=rd, now=NOW, cfg=cfg)
    assert codes(slip.get("S-VWAPC-001")) == {Refusal.SLIPPAGE_BREACH}


def test_mirror_image_strategies_are_exclusive(cfg: AllocatorConfig) -> None:
    # on a gap day with an opening drive the gap-and-go is eligible; its mirror would be too if regimes allowed
    rd = reading(R.GAP_REGIME, R.OPENING_DRIVE, R.OPENING_REVERSION, *TREND_UP)
    recs = [StrategyRecord(spec("S-GAPGO-001")), StrategyRecord(spec("S-GAPFADE-001"))]
    plan = allocate(recs, nav=NAV, regime=rd, now=NOW, cfg=cfg)
    assert plan.eligible == ("S-GAPGO-001",)
    assert codes(plan.get("S-GAPFADE-001")) == {Refusal.EXCLUSIVE_GROUP}
    flipped = allocate(list(reversed(recs)), nav=NAV, regime=rd, now=NOW, cfg=cfg)  # priority decides
    assert flipped.eligible == ("S-GAPFADE-001",)


def test_a_refused_member_does_not_take_the_group_slot(cfg: AllocatorConfig) -> None:
    recs = [StrategyRecord(spec("S-ORB-001")), StrategyRecord(spec("S-FBO-001"))]
    plan = allocate(recs, nav=NAV, regime=reading(*RANGE), now=NOW, cfg=cfg)  # ORB refused on a range day
    assert plan.eligible == ("S-FBO-001",)


def test_the_plan_is_serialisable_and_labelled(cfg: AllocatorConfig) -> None:
    plan = allocate([StrategyRecord(spec("S-VWAPC-001"))], nav=NAV, regime=reading(*TREND_UP), now=NOW, cfg=cfg)
    d = json.loads(json.dumps(plan.to_dict()))
    assert d["config_version"] == cfg.version and d["classifier_validated"] is False
    assert any("ASSUMED" in x for x in d["labels"])
    with pytest.raises(ValueError):
        allocate([StrategyRecord(spec("S-VWAPC-001"))] * 2, nav=NAV, regime=None, now=NOW, cfg=cfg)
    with pytest.raises(ValueError):
        allocate([], nav=Decimal(0), regime=None, now=NOW, cfg=cfg)


ALL = ("S-ORB-001", "S-FBO-001", "S-VWAPC-001", "S-VWAPMR-001", "S-VOLX-001", "S-IVRV-001", "S-EXP0-001",
       "S-GAPGO-001", "S-GAPFADE-001", "S-VIXSTR-001", "S-EVTBO-001", "S-LUNCH-001")  # fmt: skip
SPECS = {s: spec(s) for s in ALL}


@settings(max_examples=150, deadline=None)
@given(
    tags=st.sets(st.sampled_from(list(Regime)), max_size=6),
    nav=st.integers(min_value=1_000, max_value=50_000_000),
    used=st.integers(min_value=0, max_value=3_000_000),
    order=st.permutations(ALL),
)
def test_budgets_never_exceed_the_limits_and_refused_means_zero(
    tags: set[Regime], nav: int, used: int, order: list[str]
) -> None:
    cfg = load_allocator_config(CONFIGS / "portfolio" / "allocator.toml")
    n = Decimal(nav)
    plan = allocate([StrategyRecord(SPECS[s]) for s in order], nav=n, regime=reading(*tags), now=NOW, cfg=cfg,
                    daily_risk_used=Decimal(used))  # fmt: skip
    assert plan.total_risk_inr <= max(Decimal(0), cfg.daily_risk_frac * n - used) + Decimal("0.01")
    for a in plan.allocations:
        assert a.risk_budget_inr <= cfg.per_trade_frac * n
        assert a.eligible == (not a.refusals)
        assert a.eligible or a.risk_budget_inr == 0
        if a.eligible:  # an eligible strategy's regime policy really permits these tags
            assert Regime.NO_EDGE not in tags and Regime.ABNORMAL_MARKET not in tags
    for g in cfg.exclusive_groups:
        assert sum(1 for s in g if s in plan.eligible) <= 1


# ---------------------------------------------------------------------------------------------- OD-014 entry caps
def test_the_spec_entry_cap_and_the_system_cap_are_both_enforced(cfg: AllocatorConfig) -> None:
    rd = reading(*TREND_UP)
    sp = spec("S-VWAPC-001")
    three = sp.model_copy(update={"entry": sp.entry.model_copy(update={"max_entries_per_day": 3})})
    ok = allocate([StrategyRecord(three, entries_today=2)], nav=NAV, regime=rd, now=NOW, cfg=cfg,
                  book_entries_today=5, max_entries_per_day=10)  # fmt: skip
    assert ok.get("S-VWAPC-001").eligible
    own = allocate([StrategyRecord(three, entries_today=3)], nav=NAV, regime=rd, now=NOW, cfg=cfg)
    assert codes(own.get("S-VWAPC-001")) == {Refusal.MAX_ENTRIES_STRATEGY}
    book = allocate([StrategyRecord(three, entries_today=0)], nav=NAV, regime=rd, now=NOW, cfg=cfg,
                    book_entries_today=10, max_entries_per_day=10)  # fmt: skip
    assert codes(book.get("S-VWAPC-001")) == {Refusal.MAX_ENTRIES_BOOK}
    # the shipped style default (docs/research/strategy-hypotheses.md entry caps)
    assert sp.entry.max_entries_per_day == 2

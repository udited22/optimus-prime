"""Net-of-everything maths: fixed costs, monthly statement, tax accrual, planning metrics (hand-checked)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from project100c.economics import (
    ChargeComponents,
    EconomicsConfig,
    TradingDay,
    build_statement,
    fixed_total,
    monthly_fixed_costs,
    nav_for_fixed_cost_threshold,
    required_gross_monthly_return,
)
from project100c.economics.model import fiscal_year_label
from project100c.errors import EconomicsError

DHAN = D(499) * D("30.4375") / D(30) * D("1.18")  # per 30 days -> calendar month, + GST
SERVER = D(5) * D(96) * D("1.18")  # USD 5 at the ASSUMED 96 INR/USD, + GST
FIXED = DHAN + SERVER


def day(d: date, gross: str, brokerage: str = "0", stt: str = "0", nav: str = "10000", **kw: object) -> TradingDay:
    return TradingDay(
        d,
        D(nav),
        D(gross),
        ChargeComponents(brokerage=D(brokerage), stt=D(stt)),
        int(kw.get("orders", 2)),  # type: ignore[call-overload]
        int(kw.get("trips", 1)),  # type: ignore[call-overload]
        abs(D(gross)),
        simulated=bool(kw.get("sim", False)),
    )


def test_fixed_costs_match_the_owner_inputs(cfg: EconomicsConfig) -> None:
    lines = {x.line_id: x for x in monthly_fixed_costs(cfg)}
    assert lines["dhan-data-api"].total_inr == DHAN
    assert lines["dhan-data-api"].total_inr.quantize(D("0.01")) == D("597.41")
    assert lines["cloud-server-mumbai"].total_inr == SERVER == D("566.4000")
    assert lines["llm-api"].total_inr == 0 and lines["upstox-plus"].enabled is False
    assert fixed_total(tuple(lines.values())) == FIXED
    assert FIXED.quantize(D("0.01")) == D("1163.81")


def test_overrides_and_toggles(cfg: EconomicsConfig) -> None:
    lines = monthly_fixed_costs(cfg, amounts={"cloud-server-mumbai": D(20), "llm-api": D(10)}, enable=["upstox-plus"])
    by = {x.line_id: x for x in lines}
    assert by["cloud-server-mumbai"].total_inr == D(20) * 96 * D("1.18")
    assert by["llm-api"].total_inr == D(10) * 96 * D("1.18")
    assert by["upstox-plus"].enabled and by["upstox-plus"].brokerage_plan_if_enabled == "upstox-plus-options"
    assert not {x.line_id: x for x in monthly_fixed_costs(cfg, disable=["dhan-data-api"])}["dhan-data-api"].enabled
    with pytest.raises(EconomicsError, match="unknown"):
        monthly_fixed_costs(cfg, amounts={"yacht": D(1)})
    with pytest.raises(EconomicsError, match="both"):
        monthly_fixed_costs(cfg, enable=["llm-api"], disable=["llm-api"])
    with pytest.raises(EconomicsError):
        monthly_fixed_costs(cfg, amounts={"llm-api": D(-1)})


def test_worked_example_at_10k_and_the_navs_where_it_makes_sense(cfg: EconomicsConfig) -> None:
    """docs/risk/system-economics.md §19.4: ~Rs 1,164/month is ~11.6% of a Rs 10k NAV; < 1%/month only from ~Rs 1.16
    lakh."""
    assert (FIXED / 10000).quantize(D("0.0001")) == D("0.1164")
    assert nav_for_fixed_cost_threshold(FIXED, D("0.01")).quantize(D(1)) == D(116381)
    hi = D(20) * 96 * D("1.18") + DHAN
    assert nav_for_fixed_cost_threshold(hi, D("0.01")).quantize(D(1)) == D(286301)
    # gross needed for +0.5%/month NET after fixed costs and 31.2% tax at Rs 10k
    r = required_gross_monthly_return(D(10000), FIXED, D(0), D("0.005"), cfg.tax.effective_rate)
    assert r == (D(50) / (1 - D("0.312")) + FIXED) / 10000
    assert r.quantize(D("0.0001")) == D("0.1236")
    with pytest.raises(EconomicsError):
        nav_for_fixed_cost_threshold(FIXED, D(0))
    with pytest.raises(EconomicsError):
        required_gross_monthly_return(D(0), FIXED, D(0), D("0.005"), D("0.3"))


def test_monthly_statement_hand_computed(cfg: EconomicsConfig) -> None:
    days = [day(date(2026, 10, 5), "3000", brokerage="80", stt="10"), day(date(2026, 10, 6), "-500", "40", "5")]
    st = build_statement(days, cfg)
    (m,) = st.months
    assert m.month == "2026-10" and m.trading_days == 2 and m.executed_orders == 4 and m.round_trips == 2
    assert m.gross_pnl == D(2500) and m.trading_charges == D(135) and m.charges.brokerage == D(120)
    assert m.fixed_total == FIXED
    taxable = D(2500) - D(135) - FIXED
    assert m.taxable_ytd == taxable and m.tax == taxable * D("0.312")
    assert m.net_after_all == taxable - taxable * D("0.312")
    assert m.net_return_on_nav == m.net_after_all / D(10000)
    assert m.cost_drag == (D(135) + FIXED + m.tax) / D(2500)
    assert m.break_even_gross_return == FIXED / D(10000)
    assert m.break_even_gross_return_incl_trading == (FIXED + D(135)) / D(10000)
    assert m.closing_nav == D(10000) + D(2500) - D(135)  # infra and tax are paid outside trading NAV (OD-011)
    assert not st.simulated and any("ASSUMED" in x for x in st.labels)


def test_tax_accrual_reverses_after_a_later_loss_and_resets_each_fiscal_year(cfg: EconomicsConfig) -> None:
    days = [
        day(date(2026, 10, 5), "4000"),
        day(date(2026, 11, 2), "-3000", nav="12000"),
        day(date(2027, 3, 1), "0", nav="9000"),
        day(date(2027, 4, 1), "2000", nav="9000"),
    ]
    st = build_statement(days, cfg)
    by = {m.month: m for m in st.months}
    assert list(by) == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04"]
    oct_tax = (D(4000) - FIXED) * D("0.312")
    assert by["2026-10"].tax == oct_tax
    assert by["2026-11"].tax == -oct_tax  # FY-to-date result is now a loss: the accrual is reversed
    assert all(by[k].tax == 0 for k in ("2026-12", "2027-01", "2027-02", "2027-03"))
    # months without trades still pay fixed costs and carry NAV forward
    assert by["2026-12"].trading_days == 0 and by["2026-12"].fixed_total == FIXED
    assert by["2026-12"].opening_nav == by["2026-11"].closing_nav == D(9000)
    assert by["2027-04"].taxable_ytd == D(2000) - FIXED  # new fiscal year starts from zero
    assert by["2027-04"].tax == (D(2000) - FIXED) * D("0.312")
    assert any("FY2026-27" in n and "loss" in n and "UNVERIFIED" in n for n in st.notes)
    assert fiscal_year_label(2027, 3, 4) == "FY2026-27" and fiscal_year_label(2027, 4, 4) == "FY2027-28"


def test_brought_forward_loss_is_explicit_and_first_year_only(cfg: EconomicsConfig) -> None:
    days = [day(date(2026, 10, 5), "5000")]
    plain = build_statement(days, cfg).latest
    with_bf = build_statement(days, cfg, brought_forward_loss=D(1000)).latest
    assert with_bf.tax == (D(5000) - FIXED - D(1000)) * D("0.312") < plain.tax
    with pytest.raises(EconomicsError):
        build_statement(days, cfg, brought_forward_loss=D(-1))


def test_cost_drag_is_undefined_without_gross_profit(cfg: EconomicsConfig) -> None:
    m = build_statement([day(date(2026, 10, 5), "-100", brokerage="40")], cfg).latest
    assert m.cost_drag is None and m.tax == 0 and m.net_after_all == D(-140) - FIXED


def test_turnover_note_near_the_audit_limit(cfg: EconomicsConfig) -> None:
    big = replace(day(date(2026, 10, 5), "100", nav="1000000"), abs_trade_pnl=D(6_000_000))
    st = build_statement([big], cfg)
    assert st.fy_turnover["FY2026-27"] == D(6_000_000)
    assert any("audit" in n.lower() and "UNVERIFIED" in n for n in st.notes)
    assert not any("audit limit" in n for n in build_statement([day(date(2026, 10, 5), "100")], cfg).notes)


def test_through_extends_the_statement(cfg: EconomicsConfig) -> None:
    st = build_statement([day(date(2026, 10, 5), "100")], cfg, through=date(2026, 12, 31))
    assert [m.month for m in st.months] == ["2026-10", "2026-11", "2026-12"]
    with pytest.raises(EconomicsError, match="before"):
        build_statement([day(date(2026, 10, 5), "100")], cfg, through=date(2026, 9, 1))


@pytest.mark.parametrize(
    "bad",
    [
        lambda: TradingDay(date(2026, 10, 5), D(0), D(1), ChargeComponents(), 0, 0, D(0)),
        lambda: TradingDay(date(2026, 10, 5), D(1), 1.5, ChargeComponents(), 0, 0, D(0)),  # type: ignore[arg-type]
        lambda: TradingDay(date(2026, 10, 5), D(1), D(1), ChargeComponents(), -1, 0, D(0)),
        lambda: TradingDay(date(2026, 10, 5), D(1), D(1), ChargeComponents(), True, 0, D(0)),
        lambda: TradingDay(date(2026, 10, 5), D(1), D(1), ChargeComponents(), 0, 0, D(-1)),
        lambda: ChargeComponents(brokerage=D(-1)),
        lambda: ChargeComponents(stt=D("NaN")),
    ],
)
def test_bad_inputs_raise(bad: object) -> None:
    with pytest.raises(EconomicsError):
        bad()  # type: ignore[operator]


def test_statement_input_errors(cfg: EconomicsConfig) -> None:
    d = day(date(2026, 10, 5), "100")
    with pytest.raises(EconomicsError, match="no trading days"):
        build_statement([], cfg)
    with pytest.raises(EconomicsError, match="duplicate"):
        build_statement([d, d], cfg)
    with pytest.raises(EconomicsError, match="SIMULATED"):
        build_statement([d, day(date(2026, 10, 6), "1", sim=True)], cfg)
    with pytest.raises(EconomicsError, match="NAV"):
        build_statement([day(date(2026, 10, 5), "-20000")], cfg)

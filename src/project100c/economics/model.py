"""Whole-system economics: gross P&L -> net of trading charges, fixed infrastructure and income tax.

Pure functions over Decimals; nothing is rounded until a report renders it. Inputs are validated and bad inputs
raise EconomicsError (never a silent default). Conventions:

* NAV is trading capital. Fixed infrastructure costs are paid from the separate infrastructure budget (OD-011) and
  tax is paid outside the trading account, so a month's closing NAV = opening NAV + gross P&L - trading charges.
  The *economic* return still charges everything: net_after_all = gross - trading charges - fixed costs - tax.
* Fixed costs are charged in full for every calendar month in a statement (they are paid whether or not we trade).
* Tax (ASSUMED; OD-017: no CA review, the owner handles tax at filing) accrues on fiscal-year-to-date taxable
  business income; a month's accrual is the change in year-to-date tax, so a later loss in the same year reverses
  earlier accruals.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from project100c.costs import ChargeBreakdown
from project100c.economics.config import Billing, Currency, EconomicsConfig, Status
from project100c.errors import EconomicsError

ZERO = Decimal(0)


def _req_dec(v: object, name: str) -> Decimal:
    if not isinstance(v, Decimal) or not v.is_finite():
        raise EconomicsError(f"{name} must be a finite Decimal, got {type(v).__name__}")
    return v


# ---------------------------------------------------------------- charges
@dataclass(frozen=True, slots=True)
class ChargeComponents:
    """Trading charges by component (brokerage + statutory/exchange + GST), in INR."""

    brokerage: Decimal = ZERO
    stt: Decimal = ZERO
    exchange_txn: Decimal = ZERO
    sebi_fee: Decimal = ZERO
    stamp_duty: Decimal = ZERO
    gst: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in ("brokerage", "stt", "exchange_txn", "sebi_fee", "stamp_duty", "gst"):
            if _req_dec(getattr(self, name), name) < 0:
                raise EconomicsError(f"charge component {name} must be >= 0")

    @classmethod
    def from_breakdown(cls, b: ChargeBreakdown) -> ChargeComponents:
        return cls(b.brokerage, b.stt, b.exchange_txn, b.sebi_fee, b.stamp_duty, b.gst)

    def __add__(self, o: ChargeComponents) -> ChargeComponents:
        return ChargeComponents(
            self.brokerage + o.brokerage,
            self.stt + o.stt,
            self.exchange_txn + o.exchange_txn,
            self.sebi_fee + o.sebi_fee,
            self.stamp_duty + o.stamp_duty,
            self.gst + o.gst,
        )

    @property
    def statutory(self) -> Decimal:
        """Everything except brokerage: STT, exchange transaction charges, SEBI fee, stamp duty and GST."""
        return self.stt + self.exchange_txn + self.sebi_fee + self.stamp_duty + self.gst

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.statutory


# ---------------------------------------------------------------- fixed costs
@dataclass(frozen=True, slots=True)
class FixedCostMonthly:
    line_id: str
    label: str
    base_inr: Decimal  # per calendar month, before GST
    gst_inr: Decimal
    enabled: bool
    status: Status
    note: str
    optimisation: str
    brokerage_plan_if_enabled: str | None = None

    @property
    def total_inr(self) -> Decimal:
        return self.base_inr + self.gst_inr


def monthly_fixed_costs(
    cfg: EconomicsConfig,
    *,
    amounts: Mapping[str, Decimal] | None = None,
    enable: Iterable[str] = (),
    disable: Iterable[str] = (),
) -> tuple[FixedCostMonthly, ...]:
    """Every configured line converted to INR per calendar month (GST on top). ``amounts`` overrides a line's
    configured amount (in that line's own currency); ``enable``/``disable`` override the enabled flag."""
    amounts = dict(amounts or {})
    on, off = set(enable), set(disable)
    known = {f.line_id for f in cfg.fixed_cost}
    for lid in set(amounts) | on | off:
        if lid not in known:
            raise EconomicsError(f"unknown fixed cost line {lid!r}")
    if on & off:
        raise EconomicsError(f"lines both enabled and disabled: {sorted(on & off)}")
    out: list[FixedCostMonthly] = []
    for f in cfg.fixed_cost:
        amt = _req_dec(amounts[f.line_id], f"amount[{f.line_id}]") if f.line_id in amounts else f.amount
        if amt < 0:
            raise EconomicsError(f"{f.line_id}: amount must be >= 0")
        inr = amt * cfg.fx.usd_inr if f.currency is Currency.USD else amt
        if f.billing is Billing.PER_N_DAYS:
            assert f.period_days is not None  # validated by the config model
            inr = inr * cfg.days_per_month / f.period_days
        status = f.status
        if f.currency is Currency.USD and cfg.fx.status is not Status.VERIFIED and status is Status.VERIFIED:
            status = cfg.fx.status  # a verified USD price is only as good as the FX assumption
        enabled = (f.enabled or f.line_id in on) and f.line_id not in off
        out.append(
            FixedCostMonthly(
                f.line_id,
                f.label,
                inr,
                inr * f.gst_rate,
                enabled,
                status,
                f.note,
                f.optimisation,
                f.brokerage_plan_if_enabled,
            )
        )
    return tuple(out)


def fixed_total(lines: Sequence[FixedCostMonthly]) -> Decimal:
    return sum((x.total_inr for x in lines if x.enabled), ZERO)


# ---------------------------------------------------------------- inputs
@dataclass(frozen=True, slots=True)
class TradingDay:
    """One trading day's realised result (flat by the close, OD-002)."""

    day: date
    opening_nav: Decimal
    gross_pnl: Decimal  # sell value - buy value, before any charge
    charges: ChargeComponents
    executed_orders: int
    round_trips: int
    abs_trade_pnl: Decimal  # sum of |gross P&L| per instrument-day (tax-turnover estimate input)
    recorded_charges: Decimal | None = None  # what the kernel journalled (brokerage per fill: conservative)
    simulated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise EconomicsError("TradingDay.day must be a date")
        if _req_dec(self.opening_nav, "opening_nav") <= 0:
            raise EconomicsError(f"{self.day}: opening_nav must be > 0")
        _req_dec(self.gross_pnl, "gross_pnl")
        if _req_dec(self.abs_trade_pnl, "abs_trade_pnl") < 0:
            raise EconomicsError("abs_trade_pnl must be >= 0")
        for name in ("executed_orders", "round_trips"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise EconomicsError(f"{name} must be an int >= 0")
        if self.recorded_charges is not None:
            _req_dec(self.recorded_charges, "recorded_charges")


# ---------------------------------------------------------------- outputs
@dataclass(frozen=True, slots=True)
class MonthEconomics:
    month: str  # YYYY-MM
    opening_nav: Decimal
    trading_days: int
    executed_orders: int
    round_trips: int
    gross_pnl: Decimal
    charges: ChargeComponents
    fixed: tuple[FixedCostMonthly, ...]
    tax: Decimal  # accrual for this month (can be negative: reversal after a later loss in the fiscal year)
    taxable_ytd: Decimal  # fiscal-year-to-date taxable business income after this month
    simulated: bool

    @property
    def trading_charges(self) -> Decimal:
        return self.charges.total

    @property
    def fixed_total(self) -> Decimal:
        return fixed_total(self.fixed)

    @property
    def closing_nav(self) -> Decimal:
        return self.opening_nav + self.gross_pnl - self.trading_charges

    @property
    def pre_tax_net(self) -> Decimal:
        return self.gross_pnl - self.trading_charges - self.fixed_total

    @property
    def net_after_all(self) -> Decimal:
        return self.pre_tax_net - self.tax

    @property
    def total_costs(self) -> Decimal:
        return self.trading_charges + self.fixed_total + self.tax

    @property
    def net_return_on_nav(self) -> Decimal:
        """Monthly net return on opening NAV after ALL costs (trading charges, fixed infrastructure, tax)."""
        return self.net_after_all / self.opening_nav

    @property
    def cost_drag(self) -> Decimal | None:
        """Total costs / gross P&L. None when gross P&L <= 0 (costs then exceed 100% of nothing)."""
        return self.total_costs / self.gross_pnl if self.gross_pnl > 0 else None

    @property
    def fixed_cost_frac(self) -> Decimal:
        return self.fixed_total / self.opening_nav

    @property
    def break_even_gross_return(self) -> Decimal:
        """Gross monthly return on NAV needed just to cover the fixed costs at this NAV."""
        return self.fixed_total / self.opening_nav

    @property
    def break_even_gross_return_incl_trading(self) -> Decimal:
        """...and this month's trading charges too."""
        return (self.fixed_total + self.trading_charges) / self.opening_nav


@dataclass(frozen=True, slots=True)
class Statement:
    config_version: str
    months: tuple[MonthEconomics, ...]
    fy_turnover: Mapping[str, Decimal]  # fiscal year label (e.g. "FY2026-27") -> sum |trade P&L| (UNVERIFIED)
    notes: tuple[str, ...]
    simulated: bool
    labels: tuple[str, ...] = field(default=())

    @property
    def latest(self) -> MonthEconomics:
        return self.months[-1]


# ---------------------------------------------------------------- statement
def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def _months_between(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], a
    while (y, m) <= b:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fiscal_year_label(year: int, month: int, start_month: int) -> str:
    fy = year if month >= start_month else year - 1
    return f"FY{fy}-{(fy + 1) % 100:02d}"


def build_statement(
    days: Sequence[TradingDay],
    cfg: EconomicsConfig,
    *,
    fixed_lines: Sequence[FixedCostMonthly] | None = None,
    through: date | None = None,
    brought_forward_loss: Decimal = ZERO,
) -> Statement:
    """Monthly economics for every calendar month from the first trading day to max(last day, ``through``).

    ``brought_forward_loss`` (UNVERIFIED; owner/CA supplied, never inferred) is set off against the first fiscal
    year's taxable income only.
    """
    if not days:
        raise EconomicsError("no trading days")
    if _req_dec(brought_forward_loss, "brought_forward_loss") < 0:
        raise EconomicsError("brought_forward_loss must be >= 0")
    ds = sorted(days, key=lambda d: d.day)
    if len({d.day for d in ds}) != len(ds):
        raise EconomicsError("duplicate trading days")
    sims = {d.simulated for d in ds}
    if len(sims) != 1:
        raise EconomicsError("cannot mix SIMULATED and real trading days in one statement")
    simulated = sims.pop()
    lines = tuple(fixed_lines) if fixed_lines is not None else monthly_fixed_costs(cfg)
    last = _month_key(ds[-1].day)
    if through is not None:
        if through < ds[0].day:
            raise EconomicsError("through is before the first trading day")
        last = max(last, _month_key(through))
    by_month: dict[tuple[int, int], list[TradingDay]] = {}
    for d in ds:
        by_month.setdefault(_month_key(d.day), []).append(d)
    tax_cfg = cfg.tax
    months: list[MonthEconomics] = []
    nav: Decimal | None = None
    ytd: dict[str, Decimal] = {}
    tax_ytd: dict[str, Decimal] = {}
    turnover: dict[str, Decimal] = {}
    first_fy: str | None = None
    for y, m in _months_between(_month_key(ds[0].day), last):
        md = by_month.get((y, m), [])
        opening = md[0].opening_nav if md else nav
        if opening is None:  # pragma: no cover - the first month always has days
            raise EconomicsError("no opening NAV")
        gross = sum((d.gross_pnl for d in md), ZERO)
        ch = sum((d.charges for d in md), ChargeComponents())
        mfixed = fixed_total(lines)
        fy = fiscal_year_label(y, m, tax_cfg.fiscal_year_start_month)
        first_fy = first_fy or fy
        taxable_month = gross - ch.total - (mfixed if tax_cfg.expenses_deductible else ZERO)
        ytd[fy] = ytd.get(fy, ZERO) + taxable_month
        offset = brought_forward_loss if fy == first_fy else ZERO
        new_tax_ytd = max(ZERO, ytd[fy] - offset) * tax_cfg.effective_rate
        accrual = new_tax_ytd - tax_ytd.get(fy, ZERO)
        tax_ytd[fy] = new_tax_ytd
        turnover[fy] = turnover.get(fy, ZERO) + sum((d.abs_trade_pnl for d in md), ZERO)
        me = MonthEconomics(
            f"{y:04d}-{m:02d}",
            opening,
            len(md),
            sum(d.executed_orders for d in md),
            sum(d.round_trips for d in md),
            gross,
            ch,
            lines,
            accrual,
            ytd[fy],
            simulated,
        )
        months.append(me)
        nav = me.closing_nav
        if nav <= 0:
            raise EconomicsError(f"{me.month}: closing NAV {nav} is not positive")
    notes = [f"Tax ({tax_cfg.status}): {tax_cfg.note}", tax_cfg.basis]
    for fy, v in ytd.items():
        if v < 0:
            notes.append(
                f"{fy}: taxable business result so far is a loss of ₹{-v:,.2f}. "
                f"Carry-forward ({tax_cfg.loss_carry_forward.status}): {tax_cfg.loss_carry_forward.note}"
            )
    audit = tax_cfg.audit
    for fy, t in turnover.items():
        if t >= audit.turnover_limit_default / 2:
            notes.append(
                f"{fy}: estimated F&O turnover ₹{t:,.2f} (sum of |trade P&L|) is at or above half of the "
                f"₹{audit.turnover_limit_default:,.0f} audit limit. Tax audit ({audit.status}): {audit.note}"
            )
    labels = [f"{x.label}: {x.status}" for x in lines if x.enabled and x.status is not Status.VERIFIED]
    labels.append(f"Income tax at {tax_cfg.effective_rate:.1%} ({tax_cfg.status}; owner handles tax at filing, OD-017)")
    if simulated:
        labels.insert(0, "SIMULATED trading days")
    return Statement(cfg.config_version, tuple(months), dict(turnover), tuple(notes), simulated, tuple(labels))


# ---------------------------------------------------------------- planning metrics
def fixed_cost_frac(fixed_monthly: Decimal, nav: Decimal) -> Decimal:
    if _req_dec(nav, "nav") <= 0:
        raise EconomicsError("nav must be > 0")
    return _req_dec(fixed_monthly, "fixed_monthly") / nav


def nav_for_fixed_cost_threshold(fixed_monthly: Decimal, threshold: Decimal) -> Decimal:
    """The NAV at which fixed monthly costs fall to ``threshold`` (e.g. 0.01 = 1% of NAV a month)."""
    if _req_dec(threshold, "threshold") <= 0:
        raise EconomicsError("threshold must be > 0")
    return _req_dec(fixed_monthly, "fixed_monthly") / threshold


def required_gross_monthly_return(
    nav: Decimal, fixed_monthly: Decimal, trading_charges: Decimal, target_net: Decimal, tax_rate: Decimal
) -> Decimal:
    """Gross monthly return on NAV needed to end the month with ``target_net`` (fraction of NAV) after trading
    charges, fixed costs and tax at ``tax_rate`` on the (positive) pre-tax result."""
    for v, n in ((nav, "nav"), (fixed_monthly, "fixed_monthly"), (trading_charges, "trading_charges")):
        _req_dec(v, n)
    if nav <= 0 or not (ZERO <= _req_dec(tax_rate, "tax_rate") < 1):
        raise EconomicsError("need nav > 0 and 0 <= tax_rate < 1")
    target = _req_dec(target_net, "target_net") * nav
    pre_tax = target / (1 - tax_rate) if target > 0 else target
    return (pre_tax + fixed_monthly + trading_charges) / nav

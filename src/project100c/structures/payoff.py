"""Payoff and risk of a multi-leg option position (research only; OD-019).

A position is a list of ``LegPos`` (side +1 long / -1 short, call or put, strike, quantity in units). All values are
per unit of underlying points times quantity, so the caller multiplies by nothing else: quantities already carry the
lot size. Premiums are the entry prices actually paid (long) or received (short).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from project100c.strategies.library.optmath import bs_price


@dataclass(frozen=True, slots=True)
class LegPos:
    sign: int  # +1 long, -1 short
    call: bool
    strike: float
    qty: int  # units (contracts x lot size)
    premium: float = 0.0  # entry price per unit

    def intrinsic(self, s: float) -> float:
        return max(s - self.strike, 0.0) if self.call else max(self.strike - s, 0.0)


def expiry_payoff(legs: Sequence[LegPos], s: float) -> float:
    """P&L at expiry for terminal price ``s``, including the premiums paid and received (before costs)."""
    return sum(lg.sign * lg.qty * (lg.intrinsic(s) - lg.premium) for lg in legs)


def is_defined_risk(legs: Sequence[LegPos]) -> bool:
    """True when the expiry loss is bounded as the price rises (net calls >= 0). As the price falls the loss is
    always finite (prices cannot go below zero), so a naked short put is "defined" here but its maximum loss is the
    whole strike; the research schema (``spec.structure``) separately requires every short to be covered."""
    return sum(lg.sign * lg.qty for lg in legs if lg.call) >= 0


def max_loss_points(legs: Sequence[LegPos]) -> float:
    """The largest loss at expiry (a positive number, premiums included), evaluated at every strike and at the
    tails; ``inf`` when the risk is not defined."""
    if not is_defined_risk(legs):
        return float("inf")
    ks = sorted({lg.strike for lg in legs})
    pts = [0.0, *ks, ks[-1] * 10 if ks else 0.0]
    return max(0.0, -min(expiry_payoff(legs, s) for s in pts))


def margin_estimate(legs: Sequence[LegPos], spot: float, *, elm_frac: float, expiry_extra_frac: float,
                    expiry_day: bool) -> float:  # fmt: skip
    """ASSUMED margin proxy for a defined-risk structure: its maximum loss plus exposure margin on the largest short
    leg's notional (``elm_frac``), plus the extra short-option ELM on expiry day. Not a SPAN computation."""
    shorts = [lg for lg in legs if lg.sign < 0]
    notional = max((lg.qty * spot for lg in shorts), default=0.0)
    extra = expiry_extra_frac if expiry_day else 0.0
    return max_loss_points(legs) + notional * (elm_frac + extra)


def shock_value(legs: Sequence[LegPos], spot: float, move: float, vol: float, t_years: float) -> float:
    """Mark-to-model P&L (before costs) after an instantaneous index move ``move`` (e.g. -0.13) with every leg
    repriced by Black-Scholes at volatility ``vol`` and ``t_years`` to expiry (intrinsic when t <= 0)."""
    s1 = spot * (1 + move)
    out = 0.0
    for lg in legs:
        v = lg.intrinsic(s1) if t_years <= 0 else bs_price(s1, lg.strike, t_years, vol, lg.call)
        out += lg.sign * lg.qty * (v - lg.premium)
    return out

"""Venue-agnostic option-structure maths for research (OD-019): expiry payoff, defined-risk check, maximum loss,
margin estimate and shock repricing. Strike steps, lot sizes, margin rates and costs come from the caller (configs)."""

from project100c.structures.fills import OptionHalfSpread, fill_price
from project100c.structures.payoff import (
    LegPos,
    expiry_payoff,
    is_defined_risk,
    margin_estimate,
    max_loss_points,
    shock_value,
)

__all__ = [
    "LegPos",
    "OptionHalfSpread",
    "expiry_payoff",
    "fill_price",
    "is_defined_risk",
    "margin_estimate",
    "max_loss_points",
    "shock_value",
]

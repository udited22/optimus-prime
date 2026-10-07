"""Rupee formatting with Indian digit grouping (1,16,381) for the owner-facing report and advisories."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def group_in(digits: str) -> str:
    """Indian grouping of a string of digits: last three, then pairs (12345678 -> 1,23,45,678)."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *pairs, tail]) if head else ",".join([*pairs, tail])


def inr(x: Decimal, dp: int = 2) -> str:
    """-₹1,16,381.25 style. Rounds half-up to ``dp`` places."""
    q = Decimal(1).scaleb(-dp) if dp else Decimal(1)
    v = x.quantize(q, rounding=ROUND_HALF_UP)
    sign = "-" if v < 0 else ""
    whole, _, frac = f"{abs(v):f}".partition(".")
    return f"{sign}₹{group_in(whole)}{'.' + frac if dp else ''}"

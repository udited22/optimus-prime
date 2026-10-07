"""Small shared enums used across modules (kept dependency-free)."""

from __future__ import annotations

from enum import StrEnum


class OptionRight(StrEnum):
    CE = "CE"
    PE = "PE"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

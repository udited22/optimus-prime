"""SYNTHETIC intraday market generator for tests, mechanics checks and the validation toolkit. Never market data."""

from project100c.synthetic.intraday import (
    FUT_KEY,
    INDEX_KEY,
    VIX_KEY,
    DayPlan,
    Segment,
    SyntheticDay,
    generate_day,
    generate_days,
)
from project100c.synthetic.options import (
    LOT_SIZE,
    STRIKE_STEP,
    SyntheticChain,
    generate_chain,
    merge_chains,
    option_key,
)

__all__ = [
    "FUT_KEY",
    "INDEX_KEY",
    "LOT_SIZE",
    "STRIKE_STEP",
    "VIX_KEY",
    "DayPlan",
    "Segment",
    "SyntheticChain",
    "SyntheticDay",
    "generate_chain",
    "generate_day",
    "generate_days",
    "merge_chains",
    "option_key",
]

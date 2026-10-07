"""StructureSpec: the research schema for multi-leg option structures with SELL legs (OD-019, 3-Oct-2026).

OD-019 puts defined-risk option selling into RESEARCH and PAPER scope only. This schema is deliberately separate
from ``StrategySpec`` (which still makes a SELL leg unrepresentable, OD-006): the live spec loader, the Governor and
paper mode never read it. Rules encoded here (a file violating them cannot be loaded):

- status RESEARCH only and scope ``RESEARCH_PAPER_OD019``;
- every leg set is **defined risk**: per right, the long legs cover the short legs at strikes further out of the
  money (calls: higher offset; puts: lower offset), so the expiry loss is bounded;
- entries inside 09:20-14:00, intraday exits by 15:00; an overnight variant must say so (``holds_overnight``), and
  is flagged as needing an OD-002 exception before paper or live;
- sizing by the structure's maximum loss at or below ``risk_frac`` of NAV, 1 lot maximum.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project100c.errors import SpecValidationError

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class LegSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class StructureLeg(BaseModel):
    model_config = _FROZEN
    side: LegSide
    right: Literal["CE", "PE"]
    offset: int = Field(ge=-20, le=20)  # strikes from the ATM strike at entry
    ratio: int = Field(default=1, ge=1, le=4)


def covered(legs: tuple[StructureLeg, ...]) -> bool:
    """Defined risk per right: walking from the money outward, cumulative longs never fall short of cumulative
    shorts at the outermost strike, and every short has a long at least as far out of the money."""
    for right, outward in (("CE", 1), ("PE", -1)):
        mine = [lg for lg in legs if lg.right == right]
        shorts = sum(lg.ratio for lg in mine if lg.side is LegSide.SELL)
        longs = sum(lg.ratio for lg in mine if lg.side is LegSide.BUY)
        if shorts == 0:
            continue
        if longs < shorts:
            return False
        far_short = max(outward * lg.offset for lg in mine if lg.side is LegSide.SELL)
        cover = sum(lg.ratio for lg in mine if lg.side is LegSide.BUY and outward * lg.offset > far_short)
        if cover < shorts:
            return False
    return True


class StructureVariant(BaseModel):
    model_config = _FROZEN
    code: str = Field(pattern=r"^[A-Z]$")
    entry: time
    exit: time
    holds_overnight: bool = False
    sessions_before_expiry: int | None = Field(default=None, ge=1, le=5)
    allow_expiry_day_entry: bool = False
    expiry_day_only: bool = False  # enter only on the expiry day itself (0-DTE)

    @model_validator(mode="after")
    def _windows(self) -> StructureVariant:
        if not (time(9, 20) <= self.entry <= time(14, 0)):
            raise ValueError("entry must be inside 09:20-14:00 (OD-008/OD-009)")
        if self.exit > time(15, 0):
            raise ValueError("exit after 15:00 (OD-002)")
        if self.holds_overnight and self.sessions_before_expiry is None:
            raise ValueError("an overnight variant needs sessions_before_expiry")
        if self.expiry_day_only and (self.holds_overnight or not self.allow_expiry_day_entry):
            raise ValueError("expiry_day_only is an intraday variant and needs allow_expiry_day_entry")
        return self


class StructureSpec(BaseModel):
    model_config = _FROZEN
    id: str = Field(pattern=r"^X-[A-Z0-9-]+-\d{3}$")
    version: str
    hypothesis_code: str = Field(pattern=r"^H\d{2}$")
    status: Literal["RESEARCH"]
    scope: Literal["RESEARCH_PAPER_OD019"]
    hypothesis: str = Field(min_length=40)
    # "ALWAYS", or "TREND_UP" / "TREND_DOWN" chosen by the previous close vs its 200-session average
    leg_sets: dict[Literal["ALWAYS", "TREND_UP", "TREND_DOWN"], tuple[StructureLeg, ...]]
    variants: tuple[StructureVariant, ...]
    # NONE: no volatility gate (only the structure's own entry rule), pre-registered per spec
    gate: Literal["SELL_GATE_SR_2026_10_03_1", "NONE"]
    # None = no profit target / no credit stop: the structure is held to the variant's exit (defined risk only)
    target_frac_of_credit: Decimal | None = Field(default=None, gt=0, le=1)
    stop_loss_multiple_of_credit: Decimal | None = Field(default=None, gt=0, le=5)
    risk_frac: Decimal = Field(gt=0, le=Decimal("0.02"))  # OD-005
    max_lots: int = Field(ge=1, le=1)
    falsification: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _rules(self) -> StructureSpec:
        if not self.leg_sets or ("ALWAYS" in self.leg_sets) == bool({"TREND_UP", "TREND_DOWN"} & set(self.leg_sets)):
            raise ValueError("leg_sets is either ALWAYS or the TREND_UP/TREND_DOWN pair")
        for k, legs in self.leg_sets.items():
            if not any(lg.side is LegSide.SELL for lg in legs):
                raise ValueError(f"{k}: a structure in this schema sells at least one leg")
            if not covered(legs):
                raise ValueError(f"UNDEFINED_RISK: {k} has a short leg not covered further out of the money")
        if len({v.code for v in self.variants}) != len(self.variants):
            raise ValueError("duplicate variant codes")
        return self

    def needs_od002_exception(self) -> list[str]:
        return [f"{self.id}-{v.code}" for v in self.variants if v.holds_overnight]


def load_structure_file(path: Path) -> StructureSpec:
    import yaml

    try:
        return StructureSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except Exception as e:  # pydantic ValidationError or YAML error
        raise SpecValidationError(f"{path.name}: {e}") from e

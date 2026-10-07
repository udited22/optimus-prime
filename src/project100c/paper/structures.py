"""Multi-leg PAPER book for defined-risk option structures with SELL legs (OD-019: research and paper only).

A separate, self-contained book. It never goes through the long-only path (the kernel, the RiskGovernor or a
broker). Under OD-006 the live system buys options only, and nothing here changes that. Fills are SIMULATED from
the caller's reference prices with the research simulator's rule (``project100c.structures.fill_price``: next-open
reference, ± the synthetic half-spread, ± slippage ticks).

Hard locks:

* the venue must be ``PAPER``, and ``LIVE_ALLOWED`` is False. No live module may import this one (enforced by
  tests/paper/test_structure_book.py);
* only ``RESEARCH_PAPER_OD019`` structure specs, and only the variants listed in
  ``configs/research/paper_structures.toml``;
* overnight variants are refused unless the config carries the owner's OD-002 paper exception with its decision
  reference (default: refused). Intraday structures must be flat by 15:00 (OD-002);
* entries only inside 09:20-14:00 IST (OD-008/OD-009), one lot, at most one open structure per variant, every short
  leg covered (finite maximum loss).
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from project100c.errors import ConfigError, Project100CError
from project100c.sessions import IST
from project100c.spec.structure import LegSide, StructureSpec, StructureVariant
from project100c.structures import LegPos, fill_price, max_loss_points

LIVE_ALLOWED = False
FLAT_BY = time(15, 0)
ENTRY_FROM, ENTRY_TO = time(9, 20), time(14, 0)

LegKey = tuple[str, float]  # (right, strike)
HalfSpread = Callable[[str, float, float], float]  # (right, strike, reference premium) -> half-spread in points


class StructurePaperRefused(Project100CError):
    """The paper book refused an action (scope, venue, OD-002, window, risk)."""


@dataclass(frozen=True, slots=True)
class PaperStructureConfig:
    version: str
    od002_paper_exception: bool
    od002_decision_ref: str
    enabled: tuple[str, ...]


def load_paper_structure_config(path: Path) -> PaperStructureConfig:
    raw = tomllib.loads(path.read_text())
    if raw.get("venue") != "PAPER":
        raise ConfigError(f"{path}: venue must be PAPER (OD-019), got {raw.get('venue')!r}")
    exc, ref = bool(raw.get("od002_paper_exception", False)), str(raw.get("od002_decision_ref", "")).strip()
    if exc and not ref:
        raise ConfigError(f"{path}: od002_paper_exception = true needs the owner's decision reference")
    return PaperStructureConfig(str(raw["version"]), exc, ref, tuple(str(x) for x in raw.get("enabled", ())))


@dataclass(slots=True)
class PaperLeg:
    sign: int  # +1 bought, -1 sold
    right: Literal["CE", "PE"]
    strike: float
    qty: int  # units (lots x lot size)
    entry: float
    exit: float | None = None


@dataclass(slots=True)
class PaperStructure:
    name: str
    variant: StructureVariant
    expiry: date
    opened_at: datetime
    legs: list[PaperLeg]
    credit: float  # points per unit, after the simulated fills
    max_loss_inr: float
    charges_in: float
    target: float | None
    stop: float | None
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    closed_at: datetime | None = None
    exit_reason: str | None = None
    net_inr: float | None = None
    events: list[dict[str, object]] = field(default_factory=list)

    def mark_pnl(self, prices: Mapping[LegKey, float]) -> float:
        """Open P&L per unit at the given marks (a missing mark keeps the entry price)."""
        return self.credit - sum(-lg.sign * prices.get((lg.right, lg.strike), lg.entry) for lg in self.legs)


@dataclass
class StructurePaperBook:
    cfg: PaperStructureConfig
    tick: float = 0.05
    slippage_ticks: int = 2
    charges: Callable[[str, float, int, date], float] = lambda side, price, qty, d: 0.0
    venue: str = "PAPER"
    open_positions: dict[str, PaperStructure] = field(default_factory=dict)
    closed: list[PaperStructure] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.venue != "PAPER" or LIVE_ALLOWED:
            raise StructurePaperRefused("the structure book is PAPER only (OD-019); never live")

    def open(
        self,
        spec: StructureSpec,
        code: str,
        at: datetime,
        *,
        atm: float,
        step: float,
        expiry: date,
        lot: int,
        prices: Mapping[LegKey, float],
        half_spread: HalfSpread | None = None,
        legs_key: Literal["ALWAYS", "TREND_UP", "TREND_DOWN"] = "ALWAYS",
    ) -> PaperStructure:
        name = f"{spec.id}-{code}"
        if spec.scope != "RESEARCH_PAPER_OD019" or spec.status != "RESEARCH":
            raise StructurePaperRefused(f"{name}: not an OD-019 research/paper structure")
        if name not in self.cfg.enabled:
            raise StructurePaperRefused(f"{name}: not enabled in paper config {self.cfg.version}")
        var = next((v for v in spec.variants if v.code == code), None)
        if var is None:
            raise StructurePaperRefused(f"{name}: no such variant")
        if var.holds_overnight and not self.cfg.od002_paper_exception:
            raise StructurePaperRefused(f"{name}: holds overnight; needs the owner's OD-002 paper exception (OD-002)")
        t = at.astimezone(IST).time()
        if not (ENTRY_FROM <= t <= ENTRY_TO):
            raise StructurePaperRefused(f"{name}: entry {t} outside 09:20-14:00 (OD-008/OD-009)")
        if name in self.open_positions:
            raise StructurePaperRefused(f"{name}: already open (one lot, one structure per variant)")
        d = at.astimezone(IST).date()
        if var.expiry_day_only and d != expiry:
            raise StructurePaperRefused(f"{name}: expiry-day-only variant on a non-expiry day")
        legs: list[PaperLeg] = []
        for lg in spec.leg_sets[legs_key]:
            k = atm + lg.offset * step
            ref = prices.get((lg.right, k))
            if ref is None:
                raise StructurePaperRefused(f"{name}: no price for {lg.right} {k}")
            buy = lg.side is LegSide.BUY
            hs = half_spread(lg.right, k, ref) if half_spread else 0.0
            px = fill_price(ref, buy, self.slippage_ticks, self.tick, hs)
            legs.append(PaperLeg(1 if buy else -1, lg.right, k, lot * lg.ratio, px))
        credit = sum(-lg.sign * lg.entry * (lg.qty / lot) for lg in legs)
        pos = [LegPos(lg.sign, lg.right == "CE", lg.strike, lg.qty, lg.entry) for lg in legs]
        ml = max_loss_points(pos)
        if ml == float("inf"):
            raise StructurePaperRefused(f"{name}: undefined risk (a short leg is not covered)")
        ch = sum(self.charges("BUY" if lg.sign > 0 else "SELL", lg.entry, lg.qty, d) for lg in legs)
        tgt = None if spec.target_frac_of_credit is None else float(spec.target_frac_of_credit) * credit
        stp = None if spec.stop_loss_multiple_of_credit is None else -float(spec.stop_loss_multiple_of_credit) * credit
        ps = PaperStructure(name, var, expiry, at, legs, credit, ml + 2 * ch, ch, tgt, stp)
        ps.events.append({"at": at.isoformat(), "event": "OPEN", "fills": [lg.entry for lg in legs], "sim": True})
        self.open_positions[name] = ps
        return ps

    def due_exits(self, at: datetime, prices: Mapping[LegKey, float]) -> dict[str, str]:
        """Which open structures must close at this minute, and why (target, stop, time exit, 15:00 flat)."""
        out: dict[str, str] = {}
        now = at.astimezone(IST)
        for name, ps in self.open_positions.items():
            exit_day = ps.expiry if ps.variant.holds_overnight else ps.opened_at.astimezone(IST).date()
            if not ps.variant.holds_overnight and now.date() > exit_day:
                raise StructurePaperRefused(f"{name}: an intraday structure is still open on {now.date()} (OD-002)")
            pnl = ps.mark_pnl(prices)
            if ps.target is not None and pnl >= ps.target:
                out[name] = "TARGET"
            elif ps.stop is not None and pnl <= ps.stop:
                out[name] = "STOP"
            elif now.date() >= exit_day and now.time() >= min(ps.variant.exit, FLAT_BY):
                out[name] = "TIME_EXIT"
            elif now.time() >= FLAT_BY and not ps.variant.holds_overnight:
                out[name] = "OD002_FLAT"
        return out

    def close(
        self,
        name: str,
        at: datetime,
        prices: Mapping[LegKey, float],
        reason: str,
        half_spread: HalfSpread | None = None,
    ) -> PaperStructure:
        ps = self.open_positions.pop(name)
        d = at.astimezone(IST).date()
        ch = ps.charges_in
        gross = 0.0
        for lg in ps.legs:
            ref = prices.get((lg.right, lg.strike), lg.entry)
            hs = half_spread(lg.right, lg.strike, ref) if half_spread else 0.0
            lg.exit = fill_price(ref, lg.sign < 0, self.slippage_ticks, self.tick, hs)
            gross += lg.sign * (lg.exit - lg.entry) * lg.qty
            ch += self.charges("SELL" if lg.sign > 0 else "BUY", lg.exit, lg.qty, d)
        ps.status, ps.closed_at, ps.exit_reason, ps.net_inr = "CLOSED", at, reason, gross - ch
        ps.events.append({"at": at.isoformat(), "event": "CLOSE", "reason": reason, "sim": True})
        self.closed.append(ps)
        return ps

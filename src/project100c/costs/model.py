"""Pure cost computation. No I/O; every invalid input raises CostModelError (never a silent default)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from project100c.costs.config import BrokeragePlan, ChargeBook, ChargeSchedule
from project100c.errors import CostModelError, UnverifiedConfigError

Number = Decimal | int | str
_PAISE = Decimal("0.01")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


def to_decimal(value: Number, name: str) -> Decimal:
    """Convert to Decimal, refusing floats/bools (binary floats silently corrupt money maths)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise CostModelError(f"{name} must be Decimal, int or str, not {type(value).__name__}")
    if not isinstance(value, (Decimal, int, str)):
        raise CostModelError(f"{name} has unsupported type {type(value).__name__}")
    try:
        d = Decimal(str(value))
    except Exception as e:  # decimal.InvalidOperation
        raise CostModelError(f"{name}={value!r} is not a number") from e
    if not d.is_finite():
        raise CostModelError(f"{name} must be finite")
    return d


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi_fee: Decimal
    stamp_duty: Decimal
    gst: Decimal
    schedule_versions: tuple[str, ...]
    plan_id: str
    uses_unverified: bool

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi_fee + self.stamp_duty + self.gst

    def total_rounded(self) -> Decimal:
        """Total rounded to paise (ROUND_HALF_UP). Contract notes may round per component; see D-05 AT."""
        return self.total.quantize(_PAISE, rounding=ROUND_HALF_UP)

    def __add__(self, other: ChargeBreakdown) -> ChargeBreakdown:
        if not isinstance(other, ChargeBreakdown):
            return NotImplemented
        if other.plan_id != self.plan_id:
            raise CostModelError(f"cannot add breakdowns from different plans {self.plan_id}/{other.plan_id}")
        versions = tuple(dict.fromkeys(self.schedule_versions + other.schedule_versions))
        return ChargeBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange_txn=self.exchange_txn + other.exchange_txn,
            sebi_fee=self.sebi_fee + other.sebi_fee,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            gst=self.gst + other.gst,
            schedule_versions=versions,
            plan_id=self.plan_id,
            uses_unverified=self.uses_unverified or other.uses_unverified,
        )


class CostModel:
    """Prices executed orders of NSE index options. Immutable after construction."""

    def __init__(
        self,
        book: ChargeBook,
        plans: dict[str, BrokeragePlan],
        *,
        allow_unverified: bool = False,
    ) -> None:
        if not plans:
            raise CostModelError("no brokerage plans supplied")
        self._book = book
        self._plans = dict(plans)
        self._allow_unverified = allow_unverified

    @property
    def book(self) -> ChargeBook:
        return self._book

    def plan(self, plan_id: str) -> BrokeragePlan:
        try:
            plan = self._plans[plan_id]
        except KeyError as e:
            raise CostModelError(f"unknown brokerage plan {plan_id!r}") from e
        if not plan.verified and not self._allow_unverified:
            raise UnverifiedConfigError(f"brokerage plan {plan_id} is UNVERIFIED")
        return plan

    def schedule(self, trade_date: date) -> ChargeSchedule:
        return self._book.schedule_for(trade_date, allow_unverified=self._allow_unverified)

    @staticmethod
    def _check_qty(quantity: int) -> None:
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise CostModelError(f"quantity must be int, got {type(quantity).__name__}")
        if quantity <= 0:
            raise CostModelError(f"quantity must be > 0, got {quantity}")

    def order_charges(
        self, side: Side, price: Number, quantity: int, trade_date: date, plan_id: str
    ) -> ChargeBreakdown:
        """Charges for ONE executed order (one brokerage fee) of ``quantity`` units at ``price`` premium."""
        if not isinstance(side, Side):
            raise CostModelError(f"side must be Side, got {side!r}")
        p = to_decimal(price, "price")
        if p <= 0:
            raise CostModelError(f"price must be > 0, got {p}")
        self._check_qty(quantity)
        sched = self.schedule(trade_date)
        plan = self.plan(plan_id)
        turnover = p * quantity
        brokerage = plan.per_order(turnover)
        stt = sched.stt_sell_rate * turnover if side is Side.SELL else Decimal(0)
        txn = sched.exchange_txn_rate * turnover
        sebi = sched.sebi_fee_rate * turnover
        stamp = sched.stamp_buy_rate * turnover if side is Side.BUY else Decimal(0)
        gst = sched.gst_rate * (brokerage + txn + sebi)
        return ChargeBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_txn=txn,
            sebi_fee=sebi,
            stamp_duty=stamp,
            gst=gst,
            schedule_versions=(sched.version,),
            plan_id=plan.plan_id,
            uses_unverified=(not sched.verified) or (not plan.verified),
        )

    def round_trip(
        self,
        entry_price: Number,
        exit_price: Number,
        quantity: int,
        trade_date: date,
        plan_id: str,
        *,
        exit_date: date | None = None,
    ) -> ChargeBreakdown:
        """Long round trip: BUY to open at entry_price, SELL to close at exit_price (long-only mandate, OD-006)."""
        buy = self.order_charges(Side.BUY, entry_price, quantity, trade_date, plan_id)
        sell = self.order_charges(Side.SELL, exit_price, quantity, exit_date or trade_date, plan_id)
        return buy + sell

    def exercise_charges(
        self, intrinsic_per_unit: Number, quantity: int, trade_date: date, plan_id: str
    ) -> ChargeBreakdown:
        """Charges when a long option is exercised at expiry (STT on settlement value, payable by the holder).

        The design never holds to expiry; this exists so a failure case can be costed honestly.
        """
        iv = to_decimal(intrinsic_per_unit, "intrinsic_per_unit")
        if iv <= 0:
            raise CostModelError("intrinsic_per_unit must be > 0 for an exercised option")
        self._check_qty(quantity)
        sched = self.schedule(trade_date)
        plan = self.plan(plan_id)
        if plan.exercise_brokerage is None:
            raise CostModelError(f"plan {plan_id} has no exercise_brokerage configured (UNVERIFIED)")
        settlement = iv * quantity
        stt = sched.stt_exercise_rate * settlement
        gst = sched.gst_rate * plan.exercise_brokerage
        return ChargeBreakdown(
            brokerage=plan.exercise_brokerage,
            stt=stt,
            exchange_txn=Decimal(0),
            sebi_fee=Decimal(0),
            stamp_duty=Decimal(0),
            gst=gst,
            schedule_versions=(sched.version,),
            plan_id=plan.plan_id,
            uses_unverified=(not sched.verified) or (not plan.verified),
        )

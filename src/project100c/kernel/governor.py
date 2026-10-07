"""Risk Governor (backlog K-02, docs/risk/risk-engine.md §9.2). Pure and deterministic: no I/O, no clock reads, no
randomness.

    evaluate(intent, state, market) -> Decision{APPROVE(ticket) | REJECT(reasons)}

Every applicable check runs and every failing reason is reported (mandate first). A REJECT is final: callers
must not retry the same intent with modified parameters. SELL intents are exits (sell-to-close only, OD-006) and
are allowed under kills (that is how positions get flattened), but never outside the order-activity window and
never while an EXIT_ALL_FAILED halt is latched (OD-007: halt ALL trading).

`required_actions(state, market_clock_now)` tells the runtime what it MUST do now (flatten, Exit-All, latch, alert).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from project100c.calendar import MarketClock
from project100c.core_types import OrderSide
from project100c.costs import CostModel, Side
from project100c.errors import ConfigError, CostModelError, GovernorError
from project100c.instruments import Contract, InstrumentKind
from project100c.kernel.kills import KILL_ACTIONS, HaltKind, KillAction, KillSwitch
from project100c.kernel.limits import RiskLimits
from project100c.kernel.mandate import MandateOrder, PositionSnapshot, check_long_only
from project100c.kernel.regime_gate import RegimeGate, RegimeReading
from project100c.kernel.state import UNEXPECTED_STRATEGY, KernelState, OrderKind
from project100c.kernel.tickets import TicketSigner
from project100c.market_types import Quote
from project100c.sessions import IST, ResidualPositionPolicy, WindowPhase
from project100c.spec.models import Lifecycle, Regime, StrategySpec

LIVE_STATUSES = frozenset({Lifecycle.CANARY, Lifecycle.PRODUCTION})
SIMULATE_STATUSES = frozenset({Lifecycle.SHADOW})
PAPER_STATUSES = frozenset({Lifecycle.PAPER})  # only with RiskGovernor(paper_venue=True)


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class Reason(StrEnum):
    # mandate (K-02a) -- same codes as kernel.mandate.RejectReason
    MANDATE_LONG_ONLY = "MANDATE_LONG_ONLY"
    MANDATE_UNDERLYING = "MANDATE_UNDERLYING"
    MANDATE_INSTRUMENT_TYPE = "MANDATE_INSTRUMENT_TYPE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    # window
    OUTSIDE_TRADING_WINDOW = "OUTSIDE_TRADING_WINDOW"
    ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
    # state / kills
    NAV_UNKNOWN = "NAV_UNKNOWN"
    KILL_ACTIVE = "KILL_ACTIVE"
    STRATEGY_KILLED = "STRATEGY_KILLED"
    HALT_ACTIVE = "HALT_ACTIVE"
    ALL_TRADING_HALTED = "ALL_TRADING_HALTED"
    STRATEGY_STATUS = "STRATEGY_STATUS"
    # instrument / position
    INSTRUMENT_NOT_TRADEABLE = "INSTRUMENT_NOT_TRADEABLE"
    MAX_POSITION = "MAX_POSITION"
    NO_AVERAGING = "NO_AVERAGING"
    NOT_A_STRADDLE = "NOT_A_STRADDLE"  # OD-013: the 2-lot exception is a CE+PE pair only
    OPEN_ORDER_LIMIT = "OPEN_ORDER_LIMIT"
    MARTINGALE = "MARTINGALE"
    MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY"
    STRATEGY_MAX_ENTRIES = "STRATEGY_MAX_ENTRIES"  # the spec's entry.max_entries_per_day (OD-014)
    COOLDOWN = "COOLDOWN"
    # protective exit
    NO_PROTECTIVE_EXIT = "NO_PROTECTIVE_EXIT"
    STOP_INVALID = "STOP_INVALID"
    STOP_WIDENED = "STOP_WIDENED"
    # market
    NO_QUOTE = "NO_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    PRICE_SANITY = "PRICE_SANITY"
    TICK_SIZE = "TICK_SIZE"
    # money
    COST_MODEL_UNAVAILABLE = "COST_MODEL_UNAVAILABLE"
    SMALLEST_LOT_EXCEEDS_BUDGET = "SMALLEST_LOT_EXCEEDS_BUDGET"
    RISK_BUDGET_EXCEEDED = "RISK_BUDGET_EXCEEDED"
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    DAILY_HEADROOM = "DAILY_HEADROOM"
    WEEKLY_HEADROOM = "WEEKLY_HEADROOM"
    DRAWDOWN_HEADROOM = "DRAWDOWN_HEADROOM"
    EVENT_DAY_NOT_CERTIFIED = "EVENT_DAY_NOT_CERTIFIED"
    ABNORMAL_MARKET = "ABNORMAL_MARKET"  # OD-017: index/VIX move from the open beyond the limit, or a halt (entries)
    # regime (K-11 classifier + the spec's regime policy; only when a RegimeGate is configured)
    REGIME_UNKNOWN = "REGIME_UNKNOWN"
    REGIME_STALE = "REGIME_STALE"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    REGIME_NOT_ALLOWED = "REGIME_NOT_ALLOWED"
    # gateway / runtime
    TICKET_EXPIRED = "TICKET_EXPIRED"
    SYSTEM_INTEGRITY_FAILURE = "SYSTEM_INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class TradeIntent:
    intent_id: str
    strategy_id: str
    strategy_status: Lifecycle
    contract: Contract
    side: OrderSide
    qty: int
    limit_price: Decimal
    decided_at: datetime
    # protective exit (required for BUY): SL-limit trigger/limit, and the stop the spec rule produced
    stop_trigger: Decimal | None = None
    stop_limit: Decimal | None = None
    spec_stop_limit: Decimal | None = None
    event_certified: bool = False
    exit_kind: OrderKind | None = None  # for SELL intents: PROTECTIVE / EXIT
    # a leg of a multi-leg long spec (e.g. a long straddle): with a per-strategy lot cap > 1 (limits
    # strategy_max_lots) the strategy may hold one lot per leg on different instruments, summed risk <= budget
    multi_leg: bool = False


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    now: datetime
    quote: Quote | None
    available_cash: Decimal
    event_day: bool = False
    price_band: tuple[Decimal, Decimal] | None = None  # exchange operating range, if known
    measured_slippage_per_side: Decimal | None = None  # p90 per unit, once measured
    regime: RegimeReading | None = None  # the K-11 classifier's latest label (checked only with a RegimeGate)
    # OD-017 abnormal-market readings (kernel.health.market_moves); None = not measured (the kill path still runs)
    index_move_from_open_frac: Decimal | None = None
    vix_jump_frac: Decimal | None = None
    market_halt_or_circuit: bool = False


@dataclass(frozen=True, slots=True)
class RiskTicket:
    ticket_id: str
    intent_id: str
    approved_at: datetime
    expires_at: datetime
    risk_at_stop: Decimal | None
    budget: Decimal | None
    simulate_only: bool
    limits_version: str
    window_version: str
    is_exit: bool
    # K-07: the order this ticket authorises, and the Governor's HMAC over every field (kernel.tickets)
    instrument_key: str = ""
    side: str = ""
    qty: int = 0
    price_ceiling: Decimal | None = None
    signature: str = ""

    def valid_at(self, ts: datetime) -> bool:
        return self.approved_at <= ts < self.expires_at


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reasons: tuple[Reason, ...]
    ticket: RiskTicket | None
    risk_at_stop: Decimal | None = None
    budget: Decimal | None = None
    notes: tuple[str, ...] = ()
    # False for the second leg of an OD-013 long straddle: the straddle is ONE entry toward the per-strategy and
    # system entry caps, so its completing leg is journalled with counts_as_entry = false and not counted again
    counts_as_entry: bool = True

    @property
    def approved(self) -> bool:
        return self.verdict is Verdict.APPROVE


class ActionKind(StrEnum):
    CANCEL_ALL = "CANCEL_ALL"
    FLATTEN = "FLATTEN"
    EXIT_ALL = "EXIT_ALL"  # OD-007
    HALT = "HALT"
    LATCH_KILL = "LATCH_KILL"
    LATCH_HALT = "LATCH_HALT"
    ALERT = "ALERT"
    ALERT_URGENT = "ALERT_URGENT"
    VERIFY_PROTECTIVE = "VERIFY_PROTECTIVE"


@dataclass(frozen=True, slots=True)
class RequiredAction:
    kind: ActionKind
    reason: str
    instrument_key: str | None = None
    kill: KillSwitch | None = None
    halt: HaltKind | None = None
    scope: str = ""


@dataclass
class _Acc:
    reasons: list[Reason] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, r: Reason, note: str | None = None) -> None:
        if r not in self.reasons:
            self.reasons.append(r)
        if note:
            self.notes.append(f"{r}: {note}")


def _on_tick(px: Decimal, tick: Decimal) -> bool:
    return px > 0 and (px / tick) == (px / tick).to_integral_value()


def modelled_slippage(market: MarketSnapshot, tick: Decimal, limits: RiskLimits) -> Decimal:
    """Per-unit, per-side slippage the risk budget allows for: the measured p90 if any, never below
    ``min_slippage_ticks_per_side`` ticks. The OD-017 slippage kill compares realised fills against this figure."""
    return max(market.measured_slippage_per_side or Decimal(0), limits.min_slippage_ticks_per_side * tick)


def abnormal_market_notes(market: MarketSnapshot, limits: RiskLimits) -> list[str]:
    """OD-017 abnormal-market conditions present in one snapshot (empty = none)."""
    out: list[str] = []
    frm = limits.abnormal_index_from_open_frac
    if frm is not None and market.index_move_from_open_frac is not None and market.index_move_from_open_frac > frm:
        out.append(f"index {market.index_move_from_open_frac:.4f} from the open > {frm}")
    if market.vix_jump_frac is not None and market.vix_jump_frac > limits.abnormal_vix_jump_frac:
        out.append(f"VIX up {market.vix_jump_frac:.4f} since the open > {limits.abnormal_vix_jump_frac}")
    if market.market_halt_or_circuit:
        out.append("exchange halt / market-wide circuit")
    return out


def slippage_breach(fills: Sequence[tuple[Decimal, Decimal]], limits: RiskLimits) -> str:
    """OD-017 slippage kill rule over a strategy's latest fills, each (realised, modelled) per unit. Returns the
    reason, or "" when within limits or the rule is not in this limits version.

    * any single fill among the last ``slippage_window_fills`` with realised > ``slippage_single_fill_multiple`` x
      modelled; or
    * once that many fills exist, sum(realised) > ``strategy_slippage_multiple`` x sum(modelled) over them.
    """
    n, single = limits.slippage_window_fills, limits.slippage_single_fill_multiple
    if n is None or single is None:
        return ""
    last = list(fills)[-n:]
    for i, (realised, modelled) in enumerate(last):
        if realised > single * modelled:
            return f"single fill slippage {realised} > {single}x modelled {modelled} (fill {i + 1} of last {len(last)})"
    if len(last) >= n:
        r, m = sum((x for x, _ in last), Decimal(0)), sum((y for _, y in last), Decimal(0))
        if r > limits.strategy_slippage_multiple * m:
            return f"slippage {r} > {limits.strategy_slippage_multiple}x modelled {m} over the last {n} fills"
    return ""


class RiskGovernor:
    def __init__(
        self,
        limits: RiskLimits,
        clock: MarketClock,
        costs: CostModel,
        plan_id: str,
        *,
        regime_gate: RegimeGate | None = None,
        paper_venue: bool = False,
        strategy_entry_caps: Mapping[str, int] | None = None,
        event_certified_strategies: Iterable[str] | None = None,
        ticket_signer: TicketSigner | None = None,
    ) -> None:
        self._l = limits
        # K-07: when set, every approved ticket is signed; give the gateway the same signer so it checks entries
        self._signer = ticket_signer
        # PAPER (docs/research/paper-trading.md §14.1) trades only on a paper venue: the runtime refuses a paper-venue
        # Governor unless
        # its broker declares ``paper_venue`` (FakeBroker / PaperBroker), so a PAPER intent can never reach a real
        # broker. Every other rule is enforced exactly as in live.
        self.paper_venue = paper_venue
        # OD-014: each spec's entry.max_entries_per_day (``entry_caps_from_specs``). When configured, a strategy that
        # is not listed gets 1 entry a day (fail closed). The system-wide limits.max_trades_per_day applies on top.
        self._entry_caps = dict(strategy_entry_caps) if strategy_entry_caps is not None else None
        # OD-014 event certification: the ids whose spec is event_certified (``event_certified_from_specs``, after
        # validation.event_cert verifies each gate result). When configured, the spec decides and the intent's own
        # event_certified flag is ignored (a strategy cannot vouch for itself).
        self._event_cert = frozenset(event_certified_strategies) if event_certified_strategies is not None else None
        # OD-013: contracts of approved multi-leg BUY entries, so a second leg can be checked against the first.
        # In memory only: after a restart the first leg is unknown and a second leg is refused (fail closed).
        self._legs: dict[str, Contract] = {}
        self._regime = regime_gate
        self._clock = clock
        self._costs = costs
        self._plan = plan_id
        costs.plan(plan_id)  # fail fast: unknown / unverified plan raises now, not mid-session

    @property
    def limits(self) -> RiskLimits:
        return self._l

    @property
    def clock(self) -> MarketClock:
        return self._clock

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, intent: TradeIntent, state: KernelState, market: MarketSnapshot) -> Decision:
        now = market.now
        if now.tzinfo is None or now.utcoffset() is None:
            raise GovernorError("market.now must be timezone-aware")
        acc = _Acc()
        c = intent.contract
        # 1. mandate first (OD-006)
        md = check_long_only(
            MandateOrder(c.instrument_key, c.underlying, c.right, intent.side, intent.qty, c.lot_size),
            PositionSnapshot(state.open_qty(), state.pending_sell_qty()),
        )
        for r in md.reasons:
            acc.add(Reason(r.value))
        # 2. window (OD-002 / OD-008)
        phase = self._clock.phase(now)
        if phase in (WindowPhase.BEFORE_WINDOW, WindowPhase.CLOSED):
            acc.add(Reason.OUTSIDE_TRADING_WINDOW, f"phase {phase}")
        for hk in (HaltKind.EXIT_ALL_FAILED, HaltKind.RESIDUAL_AT_HARD_FLAT):
            if hk in state.halts:
                acc.add(Reason.ALL_TRADING_HALTED, f"{hk}: owner must resolve")
        if intent.side is OrderSide.SELL:
            return self._exit(intent, state, market, acc)
        if phase is not WindowPhase.ENTRY_ALLOWED and Reason.OUTSIDE_TRADING_WINDOW not in acc.reasons:
            acc.add(Reason.ENTRY_WINDOW_CLOSED, f"phase {phase}")
        return self._entry(intent, state, market, acc)

    def _exit(self, intent: TradeIntent, state: KernelState, market: MarketSnapshot, acc: _Acc) -> Decision:
        tick = intent.contract.tick_size
        if not _on_tick(intent.limit_price, tick):
            acc.add(Reason.TICK_SIZE)
        if intent.exit_kind is OrderKind.PROTECTIVE:
            if intent.stop_trigger is None or intent.stop_trigger < intent.limit_price:
                acc.add(Reason.STOP_INVALID, "sell SL needs trigger >= limit")
            elif not _on_tick(intent.stop_trigger, tick):
                acc.add(Reason.TICK_SIZE)
        q = market.quote
        if q is not None and q.ask is not None and intent.exit_kind is not OrderKind.PROTECTIVE:
            # an exit LIMIT far above the ask would never fill: that is a stuck exit, not a safe one
            if intent.limit_price > q.ask + self._l.limit_above_ask_ticks * tick:
                acc.add(Reason.PRICE_SANITY, "exit limit above ask + band")
        return self._finish(intent, market, acc, None, None, is_exit=True)

    def _entry(self, intent: TradeIntent, state: KernelState, market: MarketSnapshot, acc: _Acc) -> Decision:
        L, c, now = self._l, intent.contract, market.now
        today = now.astimezone(IST).date()
        tick = c.tick_size
        # 3. state known
        if state.trading_date != today or state.sod_nav <= 0:
            acc.add(Reason.NAV_UNKNOWN, "no DAY_START for today")
        # 4. kills / halts
        for k in state.global_kills():
            if KillAction.BLOCK_ENTRIES in KILL_ACTIONS[k.switch]:
                acc.add(Reason.KILL_ACTIVE, f"{k.switch}: {k.reason}")
        if state.strategy_killed(intent.strategy_id):
            acc.add(Reason.STRATEGY_KILLED)
        for h in state.halts.values():
            acc.add(Reason.HALT_ACTIVE, f"{h.kind}: {h.reason}")
        # 5. strategy lifecycle
        allowed = LIVE_STATUSES | SIMULATE_STATUSES | (PAPER_STATUSES if self.paper_venue else frozenset())
        if intent.strategy_status not in allowed:
            where = " (PAPER trades only on a paper venue)" if intent.strategy_status in PAPER_STATUSES else ""
            acc.add(Reason.STRATEGY_STATUS, f"{intent.strategy_status} cannot trade{where}")
        # 6. instrument + position
        if c.kind is not InstrumentKind.OPTION or c.expiry < today:
            acc.add(Reason.INSTRUMENT_NOT_TRADEABLE)
        lots = intent.qty // c.lot_size if c.lot_size > 0 and intent.qty > 0 else 0
        open_lots = sum(p.qty // p.lot_size for p in state.positions.values())
        pending_lots = sum(
            o.remaining // o.lot_size for o in state.open_orders.values() if o.side is OrderSide.BUY and o.lot_size > 0
        )
        cap = L.lot_cap(intent.strategy_id) if intent.multi_leg else L.max_lots
        own = [p for p in state.positions.values() if p.strategy_id == intent.strategy_id]
        own_entries = [
            o for o in state.open_orders.values() if o.kind is OrderKind.ENTRY and o.strategy_id == intent.strategy_id
        ]
        own_lots = sum(p.qty // p.lot_size for p in own) + sum(
            o.remaining // o.lot_size for o in own_entries if o.side is OrderSide.BUY and o.lot_size > 0
        )
        if cap > 1:  # OD-013: the straddle's own legs up to its cap, and nothing else open in the book
            over = (open_lots + pending_lots - own_lots) > 0 or own_lots + lots > cap
        else:
            over = lots + open_lots + pending_lots > L.max_lots
        if over or lots > L.max_lots:
            acc.add(Reason.MAX_POSITION, f"{lots}+{open_lots} open+{pending_lots} pending > {cap} lot(s)")
        # a further leg of a multi-leg spec is not pyramiding: another instrument, within the strategy's lot cap,
        # and (OD-013) it must complete a long straddle: the other right, same underlying, expiry and strike
        leg_ok = (
            cap > 1
            and c.instrument_key not in state.positions
            and all(o.instrument_key != c.instrument_key for o in own_entries)
        )
        if leg_ok and (own or own_entries):
            why = self._straddle_mismatch(c, {p.instrument_key for p in own} | {o.instrument_key for o in own_entries})
            if why:
                acc.add(Reason.NOT_A_STRADDLE, why)
                leg_ok = False
        if c.instrument_key in state.positions or (own and not leg_ok):
            acc.add(Reason.NO_AVERAGING, "already long: no averaging down / pyramiding")
        # the leg that completes a straddle is part of the entry already counted (a straddle is ONE entry)
        completes_straddle = leg_ok and bool(own or own_entries)
        # 7. open orders
        open_entries = sum(1 for o in state.open_orders.values() if o.kind is OrderKind.ENTRY)
        if open_entries >= (max(L.max_open_entry_orders, cap) if leg_ok else L.max_open_entry_orders):
            acc.add(Reason.OPEN_ORDER_LIMIT)
        # 8. martingale: no size increase within N trades after a loss
        recent = [t for t in state.closed_trades if t.strategy_id == intent.strategy_id][
            -L.martingale_lookback_trades :
        ]
        if any(t.pnl < 0 and lots > t.lots for t in recent):
            acc.add(Reason.MARTINGALE, "size up after a loss")
        # 9. trades/day, cooldown (a straddle's completing leg is the same entry: not counted twice)
        if state.entries_today >= L.max_trades_per_day and not completes_straddle:
            acc.add(
                Reason.MAX_TRADES_PER_DAY, f"{state.entries_today} entries today, system cap {L.max_trades_per_day}"
            )
        if self._entry_caps is not None and not completes_straddle:
            mine, own_cap = (
                state.entries_by_strategy.get(intent.strategy_id, 0),
                self._entry_caps.get(intent.strategy_id, 1),
            )
            if mine >= own_cap:
                acc.add(Reason.STRATEGY_MAX_ENTRIES, f"{intent.strategy_id}: {mine} entries today, spec cap {own_cap}")
        last = state.last_stop_out.get(intent.strategy_id)
        if last is not None and now - last < timedelta(minutes=L.cooldown_after_stop_min):
            acc.add(Reason.COOLDOWN, f"stopped out at {last.isoformat()}")
        # 10. protective exit
        stop_ok = False
        if intent.stop_trigger is None or intent.stop_limit is None or intent.spec_stop_limit is None:
            acc.add(Reason.NO_PROTECTIVE_EXIT, "entry needs SL trigger, SL limit and the spec's stop")
        elif not (Decimal(0) < intent.stop_limit <= intent.stop_trigger < intent.limit_price):
            acc.add(Reason.STOP_INVALID, "need 0 < stop_limit <= stop_trigger < entry limit")
        else:
            stop_ok = True
            if intent.stop_limit < intent.spec_stop_limit:
                acc.add(Reason.STOP_WIDENED, "stop below the spec rule (never move a stop to fit)")
            if not (_on_tick(intent.stop_limit, tick) and _on_tick(intent.stop_trigger, tick)):
                acc.add(Reason.TICK_SIZE)
        # 11. liquidity / freshness
        q = market.quote
        if q is None or q.bid is None or q.ask is None or q.bid <= 0 or q.ask < q.bid:
            acc.add(Reason.NO_QUOTE)
        else:
            if q.instrument_key != c.instrument_key:
                raise GovernorError("market quote is for a different instrument than the intent")
            age = (now - q.receive_ts).total_seconds()
            if age < 0 or Decimal(str(age)) > L.quote_max_age_s:
                acc.add(Reason.STALE_QUOTE, f"age {age:.3f}s")
            mid = (q.bid + q.ask) / 2
            max_spread = max(L.spread_max_ticks * tick, L.spread_max_frac_of_mid * mid)
            if q.ask - q.bid > max_spread:
                acc.add(Reason.LOW_LIQUIDITY, f"spread {q.ask - q.bid} > {max_spread}")
            if (q.ask_qty or 0) < L.min_top_of_book_lots * c.lot_size:
                acc.add(Reason.LOW_LIQUIDITY, "top-of-book size")
            if L.min_oi and (q.oi or 0) < L.min_oi:
                acc.add(Reason.LOW_LIQUIDITY, "open interest")
            # 12. price sanity
            lo, hi = q.bid - L.limit_below_bid_ticks * tick, q.ask + L.limit_above_ask_ticks * tick
            if not lo <= intent.limit_price <= hi:
                acc.add(Reason.PRICE_SANITY, f"limit {intent.limit_price} outside [{lo}, {hi}]")
        if market.price_band is not None and not market.price_band[0] <= intent.limit_price <= market.price_band[1]:
            acc.add(Reason.PRICE_SANITY, "outside exchange price band")
        if not _on_tick(intent.limit_price, tick):
            acc.add(Reason.TICK_SIZE)
        # 13. risk at stop vs budget (cost model + slippage allowance)
        risk: Decimal | None = None
        budget = L.per_trade_max_loss_frac * state.nav if state.nav > 0 else None
        if stop_ok and lots >= 1 and intent.stop_limit is not None:
            slip = modelled_slippage(market, tick, L)
            try:
                risk = self._risk_at_stop(intent.limit_price, intent.stop_limit, intent.qty, slip, today)
                one_lot = self._risk_at_stop(intent.limit_price, intent.stop_limit, c.lot_size, slip, today)
                buy_cost = self._costs.order_charges(Side.BUY, intent.limit_price, intent.qty, today, self._plan).total
            except (CostModelError, ConfigError) as e:
                acc.add(Reason.COST_MODEL_UNAVAILABLE, str(e))
            else:
                if budget is not None and leg_ok:
                    # the other legs' worst case counts against the same per-trade budget
                    risk += self._open_leg_risk(intent.strategy_id, state)
                if budget is not None:
                    if one_lot > budget:
                        acc.add(Reason.SMALLEST_LOT_EXCEEDS_BUDGET, f"1 lot risks {one_lot:.2f} > {budget:.2f}")
                    elif risk > budget:
                        acc.add(Reason.RISK_BUDGET_EXCEEDED, f"{risk:.2f} > {budget:.2f}")
                # 14. buying power (upfront premium)
                need = intent.limit_price * intent.qty + buy_cost
                if need > market.available_cash - L.buying_power_buffer:
                    acc.add(Reason.INSUFFICIENT_BUYING_POWER, f"need {need:.2f} + buffer {L.buying_power_buffer}")
                # 15. daily / weekly / drawdown headroom (the worst case of THIS trade must not breach a limit)
                if state.sod_nav > 0 and state.daily_loss + risk > L.daily_stop_frac * state.sod_nav:
                    acc.add(Reason.DAILY_HEADROOM)
                if state.sow_nav > 0 and state.weekly_loss + risk > L.weekly_freeze_frac * state.sow_nav:
                    acc.add(Reason.WEEKLY_HEADROOM)
                hwm = max(state.hwm, state.nav)
                if hwm > 0 and (hwm - (state.nav - risk)) / hwm >= L.dd_suspend_frac:
                    acc.add(Reason.DRAWDOWN_HEADROOM)
        # 16. event day: the calendar flag or an EVENT_REGIME tag from the classifier
        event_now = market.event_day or (market.regime is not None and Regime.EVENT_REGIME in market.regime.tags)
        certified = intent.event_certified if self._event_cert is None else intent.strategy_id in self._event_cert
        if event_now and not certified:
            acc.add(Reason.EVENT_DAY_NOT_CERTIFIED, f"{intent.strategy_id} is not event_certified")
        # 17. regime (entries only: exits are never blocked by a regime)
        if self._regime is not None:
            for code, note in self._regime.check(intent.strategy_id, intent.strategy_status, market.regime, now):
                acc.add(Reason(code), note)
        # 18. abnormal market (OD-017; entries only): the same thresholds as the ABNORMAL_MARKET kill, applied to
        # this intent's own snapshot so an entry is refused even before the next step latches the kill
        for note in abnormal_market_notes(market, L):
            acc.add(Reason.ABNORMAL_MARKET, note)
        d = self._finish(intent, market, acc, risk, budget, is_exit=False)
        if d.approved and intent.multi_leg:
            self._legs[c.instrument_key] = c
        if d.approved and completes_straddle:
            d = replace(d, counts_as_entry=False)
        return d

    def _straddle_mismatch(self, c: Contract, other_keys: set[str]) -> str:
        """Empty when ``c`` completes a two-leg long straddle with the strategy's one other leg (OD-013)."""
        if len(other_keys) != 1:
            return f"a straddle has exactly two legs; {len(other_keys)} already held or working"
        (key,) = other_keys
        o = self._legs.get(key)
        if o is None:
            return f"the other leg {key} is unknown to this Governor (not approved as a leg, or a restart)"
        if c.kind is not InstrumentKind.OPTION or o.kind is not InstrumentKind.OPTION:
            return "both legs must be options"
        if c.right is None or o.right is None or c.right == o.right:
            return f"a straddle is one CE and one PE (have {o.right}, adding {c.right})"
        if (c.underlying, c.expiry, c.strike) != (o.underlying, o.expiry, o.strike):
            return (f"legs differ: {o.underlying} {o.expiry} {o.strike} vs {c.underlying} {c.expiry} {c.strike} "
                    "(same underlying, expiry and strike)")  # fmt: skip
        return ""

    @staticmethod
    def _open_leg_risk(strategy_id: str, state: KernelState) -> Decimal:
        """Worst case of a strategy's other legs: entry minus the resting protective limit, or the whole premium
        while a leg is unprotected (or its entry is still working)."""
        total = Decimal(0)
        for p in state.positions.values():
            if p.strategy_id != strategy_id:
                continue
            stops = [
                o.price
                for o in state.open_orders.values()
                if o.instrument_key == p.instrument_key and o.kind is OrderKind.PROTECTIVE and o.side is OrderSide.SELL
            ]
            total += (p.avg_price - min(stops)) * p.qty if stops else p.cost_value
        for o in state.open_orders.values():
            if o.strategy_id == strategy_id and o.kind is OrderKind.ENTRY and o.side is OrderSide.BUY:
                total += o.price * o.remaining
        return total

    def _risk_at_stop(self, entry: Decimal, stop_limit: Decimal, qty: int, slip: Decimal, d: date) -> Decimal:
        charges = self._costs.round_trip(entry, stop_limit, qty, d, self._plan).total
        return (entry - stop_limit) * qty + charges + 2 * slip * qty

    def _finish(
        self,
        intent: TradeIntent,
        market: MarketSnapshot,
        acc: _Acc,
        risk: Decimal | None,
        budget: Decimal | None,
        *,
        is_exit: bool,
    ) -> Decision:
        if acc.reasons:
            return Decision(Verdict.REJECT, tuple(acc.reasons), None, risk, budget, tuple(acc.notes))
        simulate = (not is_exit) and intent.strategy_status in SIMULATE_STATUSES
        ttl = timedelta(seconds=float(self._l.ticket_ttl_s))
        ticket = RiskTicket(
            ticket_id=f"T-{intent.intent_id}",
            intent_id=intent.intent_id,
            approved_at=market.now,
            expires_at=market.now + ttl,
            risk_at_stop=risk,
            budget=budget,
            simulate_only=simulate,
            limits_version=self._l.version,
            window_version=self._clock.sessions.window.version,
            is_exit=is_exit,
            instrument_key=intent.contract.instrument_key,
            side=str(intent.side),
            qty=intent.qty,
            price_ceiling=intent.limit_price,
        )
        if self._signer is not None:
            ticket = replace(ticket, signature=self._signer.sign(ticket))
        notes = ("SIMULATE_ONLY: SHADOW strategy",) if simulate else ()
        return Decision(Verdict.APPROVE, (), ticket, risk, budget, notes)

    # ------------------------------------------------------------------ required actions
    def required_actions(self, state: KernelState, now: datetime) -> list[RequiredAction]:
        """What the runtime must do now. Deterministic function of (state, time, limits, window)."""
        L = self._l
        out: list[RequiredAction] = []
        has_pos = bool(state.positions)
        # loss limits -> latches
        if state.sod_nav > 0 and state.daily_loss >= L.daily_stop_frac * state.sod_nav:
            if (KillSwitch.DAILY_LOSS, "") not in state.kills:
                out.append(
                    RequiredAction(ActionKind.LATCH_KILL, "daily loss >= 4% of SOD NAV", kill=KillSwitch.DAILY_LOSS)
                )
        if state.sow_nav > 0 and state.weekly_loss >= L.weekly_freeze_frac * state.sow_nav:
            if HaltKind.WEEKLY_FREEZE not in state.halts:
                out.append(RequiredAction(ActionKind.LATCH_HALT, "weekly loss >= 8%", halt=HaltKind.WEEKLY_FREEZE))
        dd = state.drawdown_frac
        if dd >= L.dd_suspend_frac and HaltKind.DD_SUSPENSION not in state.halts:
            out.append(
                RequiredAction(ActionKind.LATCH_HALT, f"drawdown {dd:.4f} >= suspend", halt=HaltKind.DD_SUSPENSION)
            )
        elif dd >= L.dd_warning_frac:
            out.append(RequiredAction(ActionKind.ALERT, f"drawdown warning {dd:.4f}"))
        # strategy kill triggers from journal-derived counters
        for sid, n in state.consecutive_stops.items():
            if n >= L.strategy_consecutive_stops and not state.strategy_killed(sid):
                out.append(
                    RequiredAction(ActionKind.LATCH_KILL, f"{n} consecutive stops", kill=KillSwitch.STRATEGY, scope=sid)
                )
        for sid, fills in state.slippage_fills.items():
            why = slippage_breach(fills, L)
            if why and not state.strategy_killed(sid):
                out.append(RequiredAction(ActionKind.LATCH_KILL, why, kill=KillSwitch.STRATEGY, scope=sid))
        for sid, n in state.slippage_breaches.items():
            if n >= L.strategy_slippage_breach_trades and not state.strategy_killed(sid):
                out.append(
                    RequiredAction(
                        ActionKind.LATCH_KILL,
                        f"slippage > {L.strategy_slippage_multiple}x on {n} trades",
                        kill=KillSwitch.STRATEGY,
                        scope=sid,
                    )
                )
        for sid, n in state.invalid_intents.items():
            if n >= L.strategy_invalid_intents and not state.strategy_killed(sid):
                out.append(
                    RequiredAction(ActionKind.LATCH_KILL, f"{n} invalid intents", kill=KillSwitch.STRATEGY, scope=sid)
                )
        if HaltKind.EXIT_ALL_FAILED in state.halts or HaltKind.RESIDUAL_AT_HARD_FLAT in state.halts:
            out.append(
                RequiredAction(ActionKind.ALERT_URGENT, "OD-007: Exit-All failed; ALL trading halted; owner must act")
            )
            return out  # nothing automated may run
        if not has_pos:
            return out
        window = self._clock.sessions.window
        if self._clock.is_trading_day(now) and self._clock.must_be_flat(now):
            pol = window.residual_position_policy
            detail = ", ".join(f"{k}={p.qty}" for k, p in sorted(state.positions.items()))
            if pol is ResidualPositionPolicy.BROKER_EXIT_ALL:
                out.append(RequiredAction(ActionKind.EXIT_ALL, f"OD-007: still open at hard flat: {detail}"))
                out.append(RequiredAction(ActionKind.ALERT_URGENT, f"OD-007: Exit-All triggered for {detail}"))
            elif pol is ResidualPositionPolicy.HALT_AND_ALERT:
                out.append(RequiredAction(ActionKind.HALT, f"residual position at hard flat: {detail}"))
                out.append(RequiredAction(ActionKind.ALERT_URGENT, f"manual exit required: {detail}"))
            else:  # EXIT_ONLY_EXTENSION (needs its own OD; validated in TradingWindow)
                out.append(RequiredAction(ActionKind.FLATTEN, "exit-only extension"))
            return out
        flatten_reasons: list[str] = []
        if self._clock.phase(now) is WindowPhase.FLATTENING:
            flatten_reasons.append(f"forced flatten from {window.flatten_start}")
        for k in state.kills.values():
            acts = KILL_ACTIONS[k.switch]
            if k.scope and KillAction.FLATTEN in acts:
                for key, p in state.positions.items():
                    if p.strategy_id == k.scope:
                        out.append(RequiredAction(ActionKind.FLATTEN, f"{k.switch}({k.scope})", instrument_key=key))
                continue
            if KillAction.CANCEL_ALL in acts:
                out.append(RequiredAction(ActionKind.CANCEL_ALL, str(k.switch)))
            if KillAction.FLATTEN in acts:
                flatten_reasons.append(str(k.switch))
            if KillAction.VERIFY_PROTECTIVE in acts:
                out.append(RequiredAction(ActionKind.VERIFY_PROTECTIVE, str(k.switch)))
            if KillAction.FLATTEN_UNEXPECTED in acts:
                for key, p in state.positions.items():
                    if p.strategy_id == UNEXPECTED_STRATEGY:
                        out.append(RequiredAction(ActionKind.FLATTEN, f"{k.switch}: unexpected", instrument_key=key))
        for h in state.halts.values():
            flatten_reasons.append(str(h.kind))
        if flatten_reasons:
            out.append(RequiredAction(ActionKind.FLATTEN, "; ".join(flatten_reasons)))
        confirm = timedelta(seconds=float(L.protective_stop_confirm_s))
        for key, p in state.positions.items():
            if not p.protective_confirmed and now - p.opened_at > confirm:
                out.append(
                    RequiredAction(
                        ActionKind.FLATTEN,
                        f"no protective stop within {L.protective_stop_confirm_s}s",
                        instrument_key=key,
                    )
                )
                out.append(RequiredAction(ActionKind.ALERT_URGENT, f"unprotected position {key}", instrument_key=key))
        return out


def event_certified_from_specs(specs: Iterable[StrategySpec]) -> frozenset[str]:
    """The ids whose spec is ``event_certified`` (the record itself is verified by validation.event_cert)."""
    return frozenset(s.id for s in specs if s.event_certified)


def entry_caps_from_specs(specs: Iterable[StrategySpec]) -> dict[str, int]:
    """Each spec's own entries-per-day cap (``entry.max_entries_per_day``), for the Governor."""
    return {s.id: s.entry.max_entries_per_day for s in specs}

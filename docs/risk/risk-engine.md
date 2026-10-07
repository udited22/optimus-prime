# Risk engine

**Status:** implemented and exercised in replay/paper modes. The public build does not place live orders.

The Risk Governor exists to make one architectural statement enforceable in code:

> **Strategy logic may propose exposure; it never has authority to grant itself exposure.**

The Governor is deterministic, side-effect free at decision time and independent of the strategy implementation that produced the intent.

## Decision contract

Conceptually:

```text
evaluate(
    trade_intent,
    portfolio_state,
    risk_policy,
    market_snapshot,
    cost_model
) -> APPROVE(risk_ticket) | REJECT(reasons)
```

An approval is not a suggestion. It is a bounded authorisation tied to the intent and the state that was evaluated. A rejection is final for that intent; strategy code cannot silently resize, widen protection or otherwise mutate the rejected request until it passes.

## What the Governor evaluates

The implementation groups checks into a few durable categories rather than letting every strategy invent its own controls.

### 1. Mandate and instrument validity

- Is the instrument permitted by the active mandate?
- Is the instrument metadata current and internally consistent?
- Is the requested direction/structure permitted?
- Does the intent refer to a known strategy/specification version?

### 2. Position and portfolio exposure

- Existing and pending exposure are evaluated together.
- Position concentration and aggregate exposure must remain inside policy.
- Worst-case loss is evaluated at portfolio level, not only at individual-order level.
- Strategy-specific limits may be tighter than portfolio limits but never looser.

### 3. Loss budget and economics

Risk is evaluated net of expected friction rather than on an idealised price path. The admission calculation may include:

- entry/exit price assumptions;
- explicit exchange/broker costs;
- slippage allowance;
- protective-exit assumptions;
- available cash or collateral;
- current drawdown and remaining portfolio loss budget.

A trade that only fits when realistic friction is ignored is not considered capital-eligible.

### 4. Market and data quality

New exposure can be refused when:

- required quotes are stale;
- spread or liquidity is outside policy;
- reference/instrument data is invalid;
- market state is missing or inconsistent;
- configured abnormal-market conditions are active.

The risk engine does not repair market data. Bad data is a state to respond to, not a value to smooth away.

### 5. Time and lifecycle constraints

Order activity is constrained by versioned session/trading configuration. The Governor also considers lifecycle state such as existing protective orders, pending exits and reconciliation status so that a new intent cannot be evaluated as if the book were empty when it is not.

## Capital-preservation state

The kernel maintains the minimum state needed for deterministic admission and recovery, including:

- cash/NAV and high-water mark;
- realised and unrealised P&L;
- drawdown state;
- open and pending exposure;
- strategy attribution;
- selected portfolio risk measures;
- order lifecycle state;
- reconciliation state and freshness;
- active safety/kill conditions.

State is journalled so it can be rebuilt by replay. A restart is not allowed to erase an adverse condition or reset a latched safety state.

## Kill switches

Kill switches are deliberately separate from ordinary strategy exits. They represent system conditions under which authority should contract immediately.

| Category | Example trigger | Default effect |
|---|---|---|
| **Strategy** | repeated invalid behaviour or strategy-specific risk breach | disable that strategy and manage its exposure |
| **Portfolio** | aggregate exposure or loss state outside policy | block entries; reduce/flatten exposure as policy requires |
| **Data quality** | stale/invalid market data or reference-data inconsistency | block new exposure until data is trustworthy |
| **Connectivity** | execution dependency becomes unreliable or unknowable | stop new entries and reconcile external state |
| **Reconciliation** | external positions/orders disagree with internal state | treat external state as authoritative; block new exposure |
| **System integrity** | journal/config/time/process integrity failure | halt new authority and surface the failure |
| **Manual master kill** | explicit operator safety action | cancel/flatten/halt through the deterministic control path |

Kill state is recorded in the journal. Conditions that require explicit review remain latched across restarts.

## Reconciliation is part of risk

Execution success is not inferred merely because an order submission call returned successfully.

An independent reconciler compares the internal journal with the paper/external execution interface. Any disagreement is treated as a risk condition because the system cannot safely size new exposure while it is uncertain what it already owns.

This separation prevents the component that created an order from also being the sole authority on whether that order exists or filled.

## Risk tickets and execution authority

An approved intent produces a bounded risk decision that execution can validate before constructing/transmitting an order. The ticket binds approval to the evaluated intent rather than granting a general licence to trade.

This design prevents a downstream component from taking an approval for one quantity/price/intent and using it for materially different exposure.

## Fail-closed defaults

Representative conditions that block new entries include:

- missing/invalid required market data;
- untrusted or incompatible configuration;
- reconciliation mismatch;
- active kill state;
- invalid order lifecycle state;
- exposure outside policy;
- inability to establish current portfolio state.

The system prefers a visible missed trade over invisible risk expansion.

## Verification strategy

Safety-critical behaviour is tested with more than happy-path examples. The suite includes unit tests, property-style invariants and failure-injection scenarios around the Governor, state reducer, execution lifecycle and reconciliation.

Important invariant classes include:

- an intent cannot bypass the Governor;
- exposure cannot exceed the active mandate/policy through retry or partial-fill races;
- invalid state does not silently become a permissive default;
- a latched kill survives state reconstruction;
- reconciliation mismatch blocks new authority;
- repeated/idempotent execution requests do not duplicate economic intent.

The goal is not to prove trading profitability. It is to make risk behaviour explicit, deterministic and falsifiable.

## Public/private boundary

Exact personal capital limits, broker-account configuration, live strategy parameters and deployment runbooks are intentionally not part of this public document. They are operating configuration, not enduring system architecture.

The public repository demonstrates the control model and the code paths that enforce it while keeping account-specific and candidate-specific details out of the portfolio surface.

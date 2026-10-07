# Execution engine

**Status:** implemented against fake/paper interfaces. Live transmission is disabled in the public build.

The execution layer converts an approved `TradeIntent` into a controlled order lifecycle. Its job is not to discover edge or decide portfolio risk; those decisions happen before execution receives authority.

## Flow

```text
Strategy / allocator
      ↓
TradeIntent
      ↓
Deterministic Risk Governor
      ↓ APPROVE(bound risk decision)
Order construction
      ↓
Order state machine
      ↓
Execution adapter (fake / paper / external)
      ↓
Independent reconciliation
      ↓
Journal + read-only observability
```

## Typed trade intents

A trade intent contains the information required to evaluate and execute a proposed economic action, including:

- stable intent and strategy/specification identifiers;
- resolved instrument identity;
- side and quantity;
- bounded price/entry instructions;
- protection/exit definition;
- validity/expiry information;
- decision-time market snapshot reference;
- expected cost/slippage inputs used by the risk model.

Invalid or incomplete intents are rejected rather than inferred into a plausible order.

## Order construction

Order construction is deliberately separate from strategy logic. It converts an approved intent into broker-agnostic requests while preserving the bounds that were evaluated by risk.

Representative responsibilities include:

- tick/price normalisation;
- quantity validation;
- protection/exit construction;
- enforcing the intent's approved price/quantity envelope;
- assigning idempotent request identifiers;
- refusing unsupported or unsafe order forms.

The execution layer is not permitted to make a rejected trade fit by widening risk or materially changing its economic meaning.

## Order state machine

Orders move through an explicit lifecycle rather than being represented by a single boolean success flag.

```text
NEW
  → SUBMITTING
  → ACKED / OPEN
  → PARTIALLY_FILLED
  → FILLED

with explicit branches to:
REJECTED | CANCEL_REQUESTED | CANCELLED | EXPIRED | UNKNOWN
```

Illegal or ambiguous transitions are surfaced as integrity/reconciliation conditions. `UNKNOWN` is a real state: a network timeout does not prove that an external venue failed to receive an order.

## Idempotency

Retries must not create duplicate economic exposure.

Execution requests therefore carry stable idempotency identifiers derived from the originating intent and leg/attempt semantics. The adapter and state machine can distinguish a retry of the same request from a genuinely new intent.

This becomes especially important around timeouts, partial fills and cancel/fill races.

## Adapter boundary

External execution is behind a narrow interface. Conceptually an adapter exposes operations such as:

```text
place(request)
modify(order_id, changes)
cancel(order_id)
orders()
trades()
positions()
funds()
order_updates()
```

Broker/vendor-specific payloads are normalised into internal `OrderEvent`, `Fill`, `Position` and error types before they enter the core engine.

The kernel therefore does not need to understand a vendor's REST field names, WebSocket schema or error codes.

## Reconciliation

The order-update stream is not treated as the sole source of truth.

A separate reconciler compares internal journal state with the execution interface's current orders, trades and positions. Any material disagreement blocks new authority until the system understands what exposure exists.

This is an intentional separation of duties:

> the component that asked for an order should not be the only component allowed to declare what happened to it.

## Partial fills and race conditions

Execution code is built for the cases that make trading systems difficult rather than only the happy path:

- partial fills;
- cancel requests racing with fills;
- acknowledgements arriving after a timeout;
- repeated/reordered updates;
- adapter/network errors;
- restart and state reconstruction;
- external/internal reconciliation mismatch.

These conditions are represented in the domain model and exercised with deterministic fake interfaces and failure-injection tests.

## Protection and exits

Protection belongs to the authoritative execution/risk path, not discretionary UI logic. When an open position requires protective state, the system tracks whether that protection actually exists and treats missing/invalid protection as a safety condition.

Exact live order types, broker behaviour and account-specific emergency procedures are deployment concerns and are intentionally not documented in the public repository.

## Observability

Execution events are appended to the system journal with enough context to reconstruct the lifecycle of an intent and order. The command centre consumes those events read-only.

Useful execution observability includes:

- intent and risk-decision identifiers;
- lifecycle state;
- timestamps/latency observations;
- requested versus filled prices/quantities;
- reconciliation state;
- explicit failure/rejection reason codes.

## Public/private boundary

The public repository demonstrates the execution model, state machine, adapter contracts and failure handling. It intentionally excludes account identifiers, live credentials, broker-specific operational runbooks and deployment secrets.

# 10 — Execution Engine Specification

## 10.1 Flow
`Strategy → TradeIntent → Risk Governor (APPROVE + RiskTicket) → Order Construction Engine → Broker Execution Gateway → Broker adapter → Broker → NSE`

## 10.2 TradeIntent (immutable, typed)
```
intent_id: UUIDv7            strategy_id, strategy_version, spec_hash
created_ts_exchange, created_ts_local
instrument_token, tradingsymbol, expiry, strike, option_type   # resolved from today's instrument master
side: BUY|SELL               qty_lots: int (=1 in canary)   lot_size (from master, echoed)
entry_logic: LIMIT_AT|LIMIT_MID_PLUS_TICKS|...  max_price (buy) / min_price (sell)
stop: {type: PREMIUM|UNDERLYING|TIME, trigger, limit_offset_ticks}
target/exit: {...}           validity: {ttl_seconds, not_after_ts}
reason_code                  confidence (from spec/evidence, not free-form)
expected_edge_inr, expected_cost_inr, expected_slippage_ticks
market_snapshot_id           # pointer to the exact snapshot used for the decision
```
Validation: all fields required, qty in lots, prices on the tick grid, expiry exists in today's master, and `not_after_ts` in the future. Otherwise the intent is dropped with an `INVALID_INTENT` event and counted against the strategy.

## 10.3 Order Construction Engine
- **Long-only (OD-006):** the only BUY orders are opening or adding to a long. Every SELL order the engine builds (protective SL, target, flatten ladder) is a **sell-to-close** whose qty is checked against the reconciled open long qty minus pending sells. The Gateway re-runs `mandate.long_only` before serialising. A SELL that could open a short is refused and raises SYSTEM_INTEGRITY_KILL, because it indicates a kernel bug.
- Converts an approved intent into broker-agnostic `OrderRequest`s: an entry LIMIT, then (on fill) a protective **SL-LIMIT**: trigger = stop, limit = trigger − offset for a long exit, and optionally a target LIMIT. It does *not* use broker OCO/GTT as protection in v1 (Upstox GTT legs live for 365 days; see 10.6a).
- Rounds prices to the tick. Clamps to the price band and to `max_price`. **Never uses MARKET or IOC** (C7).
- Chasing policy: at most `max_chase_ticks` re-prices, each ≥ 1 s apart, within the ticket's price ceiling. Then cancel, and the intent expires.
- Assigns an **idempotency key** (client order id / tag) derived from intent_id + leg + attempt, so retries never duplicate orders.

## 10.4 Order state machine (per order)
`NEW → SUBMITTING → ACKED(OPEN) → PARTIALLY_FILLED → FILLED`
with branches `→ REJECTED`, `→ CANCEL_REQUESTED → CANCELLED`, `→ EXPIRED`, `→ UNKNOWN`.
- `UNKNOWN` is entered on a timeout without an ack. It triggers an immediate order-book query. Until resolved, **no new orders for that instrument**. If still unresolved after 10 s → BROKER_CONNECTIVITY_KILL.
- Transitions are validated. An illegal transition (e.g. a FILLED order receives a CANCELLED update) → SYSTEM_INTEGRITY_KILL + reconciliation.
- Partial fills: the protective stop is placed for the filled qty immediately. The residual entry is cancelled when the TTL expires (with 1 lot = 65 qty, partials are possible but rare).

## 10.5 Broker Execution Gateway
- Re-validates each request against the RiskTicket (hash, qty, price ceiling, ticket expiry of 2 s). Mismatch → reject + SYSTEM_INTEGRITY_KILL.
- Rate limiting: token bucket ≤ 2 orders/s sustained, ≤ 5 burst, always ≤ min(broker limit, TOPS 10 OPS). Modification counter < 25 per order.
- Translates to broker adapter calls. Normalises broker error codes to an internal taxonomy (`RATE_LIMIT, AUTH, VALIDATION, RMS_REJECT, EXCHANGE_REJECT, NETWORK, UNKNOWN`).
- Handles `429` with backoff, and never retries non-idempotent calls blindly.
- Emits every request and response to the journal with latency stamps: intended, submitted, acked, filled (§15).

## 10.6 Broker adapter interface (per broker)
```
authenticate(session_material) -> Session          # daily; owner-completed 2FA where required
instruments() -> InstrumentMaster
subscribe(tokens, mode) / stream() -> events
place(OrderRequest) -> BrokerOrderId | Error
modify(id, changes) / cancel(id)
orders() / trades() / positions() / funds()
order_updates() -> stream
```
Each adapter ships with a **contract test suite** that runs against: (a) a fake broker (deterministic, scriptable faults), (b) the Upstox sandbox, which covers only the order place/modify/cancel endpoints (S49), and (c) a read-only live session (no orders).

## 10.6a Upstox adapter (OD-004). The first and only live adapter
The adapter implements the broker-agnostic interface above. Nothing Upstox-specific leaks past it: the kernel sees `OrderRequest`, `OrderEvent`, `Fill` and `Position`. Mapping and rules (facts in 03 §3.5):

| Interface | Upstox mapping | Adapter rules |
|---|---|---|
| `authenticate` | OAuth code flow or the owner-approved semi-automated flow (method = open decision); token expires 03:30 IST | Token in memory only; pre-market check `GET /user/ip` equals host egress IP, else HALTED |
| `place` | `POST /v3/order/place` (api-hft host), `order_type ∈ {LIMIT, SL}`, `validity=DAY`, `product=I`, `slice=false`, `tag=<idempotency key>` | Serialiser rejects MARKET, SL-M, IOC and AMO; refuses outside 09:15–15:00 (OD-002) before any network I/O; long-only check re-run (OD-006) |
| `modify` / `cancel` | v3 modify/cancel | Modification counter per order (≤ 20, then cancel-replace) |
| `orders/trades/positions/funds` | v2/v3 read APIs | Polled every 5 s (read budget metered) |
| `order_updates` | Portfolio/order-update WebSocket | Treated as a hint; polling is authoritative for reconciliation |
| `stream` | Market Data Feed V3, `full` mode | Heartbeat/staleness detector; auto-reconnect with gap flag |
| `broker_lockout` (optional capability) | `POST /v2/user/kill-switch` `NSE_FO: DISABLE` | Only when flat (the API requires it); 12 h cooling; used after DAILY_LOSS_KILL flatten, weekly freeze and MANUAL_MASTER_KILL |
| Error taxonomy | UDAPI codes → `Rejected(reason)` / `Retryable` / `Fatal` | UDAPI1154 (static IP) → BROKER_CONNECTIVITY_KILL; UDAPI1158 (market order) → SYSTEM_INTEGRITY_KILL (it should be impossible); rate-limit → backoff and alert |

**Used only for OD-007:** `exit_all` (Upstox Exit All Positions) is called only at the 15:00 hard flat when a position remains. Its pricing and order type are **UNVERIFIED** until the Upstox sandbox test.

Not used: GTT (365-day leg validity conflicts with no-overnight), the Upstox MCP server and Agent Skill (LLM order placement is prohibited, §10 of the directive).

## 10.7 Reconciliation (independent of the order-update stream)
- Every 5 s during the exchange session (read-only), and on every fill/kill event, fetch broker `positions()`, `orders()` and `trades()` and diff them against the journal.
- Any mismatch in qty, avg price (tolerance 1 tick) or unknown order → POSITION_RECONCILIATION_KILL.
- End of day: reconcile against the broker's contract note / tradebook (download) and the cost model. A charges mismatch > ₹1 per trade → cost-model review ticket.

## 10.7a Trading window enforcement (OD-002, OD-007, OD-008)
- **Our trading window (09:15–15:00 IST) is separate from the exchange session** (F&O normal market 09:15–15:40 since 3-Aug-2026; cash normal market 09:15–15:30, then CAS). Both are versioned config with an `effective_from` date. The Gateway loads the window version from the signed config and records it in every order event.
- **The Gateway refuses any place/modify/cancel outside 09:15:00–14:59:59 IST.** This is a second, independent check after the Governor's. A refusal is journalled and alerted, never silently dropped.
- **Entry cutoff 14:00** (OD-008, superseding OD-003's 14:45): entry TradeIntents at or after 14:00:00 are rejected by the Governor and re-checked at the Gateway.
- **Forced flatten** starts at 14:50 (OD-008, owner-confirmed). It cancels resting protective orders, then works exits as escalating LIMITs (no MARKET/IOC).
- **At 15:00:00 all automated order activity stops (OD-002), with one exception (OD-007).** If any position remains, the kernel calls the adapter's `exit_all()` once and sends an URGENT owner alert. It then re-reads broker positions:
  - **Flat:** journal it and alert that it resolved.
  - **Exit-All raised, timed out, or left any position open:** halt all trading (every order path refuses), keep alerting with the exact residual position, and latch POSITION_RECONCILIATION_KILL. The owner resolves it via the broker app.
  - There are no automated retries and no other order activity after 15:00.
- Reconciliation polling (10.7) runs during the exchange session and post-market. It is read-only, so it is not "order activity".

## 10.8 Exit hierarchy for an open position when things go wrong
1. Protective SL-LIMIT resting at the broker (primary).
2. If the SL order is rejected, or the stop gaps through its limit unfilled: the kernel places a LIMIT exit at `bid − k ticks` and re-prices up to N times within a hard floor.
3. If the broker is unreachable: alert the owner with the exact position and suggested manual action in the broker app. The owner holds the out-of-band manual control. The system never "retries harder" through undocumented endpoints.

## 10.9 Implementation status (3-Oct-2026)
- **Built:** `execution/order_fsm.py` (§10.4), `execution/gateway.py` (§10.5), `execution/ids.py` (idempotency keys; `broker_tag` gives a 20-character tag for brokers with short tag fields; the Upstox limit is UNVERIFIED), `execution/reconcile.py` (§10.7, pure). The kernel runtime's tests run every scenario both against the fake broker directly and through `ExecutionGateway`, including the 15:00 Exit-All success and failure paths.
- **Venue-agnostic (OD-017):** the gateway speaks only `broker.BrokerAdapter`; broker specifics stay in the adapter.
- **No credentials** reach the gateway or the kernel: an adapter is built with its credentials outside the core and passed in. `tests/test_architecture_boundaries.py` enforces that only the credential adapters read the environment and that `agents/` cannot import a broker, the execution layer or a credential loader.


# 08 — Backtesting Architecture

## 8.1 Principles
- **One engine, production code.** The backtester drives the *same* `Strategy`, `RiskGovernor`, `OrderConstructionEngine` and OMS state machine used live. Only the clock, the data source and the fill adapter are simulated.
- **Event-driven, point-in-time.** Events are replayed in `exchange_ts` order with a configurable **decision latency** (default 500 ms from event to intent, plus 300 ms from intent to exchange ack. These are conservative placeholders to be replaced by measured values). A strategy cannot see an event until `ts + latency`.
- **Pessimistic by default.** Wherever the data is ambiguous (OHLC bars, missing quotes), the fill model assumes the worse outcome.
- **Every run is registered** (code SHA, data manifest hash, params, seed, cost-model version, fill-model version) so results can be reproduced bit-for-bit and multiple testing is counted.

## 8.2 Components
| Component | Responsibility |
|---|---|
| `ReplayFeed` | Reads Parquet partitions and merges streams (spot, futures, options, VIX, constituents, events) into one ordered event stream. Emits DQ flags exactly as the live DQ gate would |
| `SimClock` | Deterministic clock and trading calendar (holidays, special sessions, 15:30→15:40 close change on 3-Aug-2026, expiry-day rules) |
| `ChainBuilder` | Point-in-time option-chain snapshots with staleness flags; own IV/Greeks |
| `FillModel` (pluggable) | `QuoteFillModel` (when bid/ask exists): buy fills at ask, or at limit if limit ≥ ask. Queue-position model for passive limits: fill only if the price trades *through* the limit, or if ≥ Q volume trades at the limit after arrival. `BarFillModel` (OHLC only): synthetic spread table by time × moneyness × DTE × VIX (built without VIX, ASSUMED: a limit buy fills only if bar low + half-spread < limit, and the half-spread is ≥ 1 tick). Stops fill at `min(stop_limit, next bar open)` with gap handling. SL-limit orders that gap past their limit remain **unfilled** (realistic NSE behaviour), and the kernel's fallback exit logic is exercised |
| `CostModel` | Dated table (see 04 §4.4): brokerage, STT (sell and exercise), NSE txn, SEBI, stamp, GST. Applied per executed order. **Versioned**, e.g. `CM-2026-04-01` |
| `LatencyModel` | Distributions for decision, submit, ack, fill. Missed-order probability; API-downtime windows injected from a scenario file |
| `Portfolio/Kernel` | Same NAV/HWM/DD/Greeks accounting as live |
| `Analytics` | Per-trade record identical to the live execution-analytics schema (§15), plus summary stats, CIs and regime slices |
| `ExperimentRegistry` | Stores every run. The Validation agent reads the full trial count for deflated-Sharpe / multiple-testing adjustments |

## 8.3 NSE/NIFTY specifics the engine must model
- Tick size ₹0.05, lot 65 (point-in-time from the instrument master), freeze quantity, daily price bands on options (reject orders outside them).
- Weekly expiry on Tuesday (from 1-Sep-2025; Thursday before). Holiday shifts. Monthly = last Tuesday.
- Expiry-day behaviour: the extra 2% ELM for shorts; no calendar-spread benefit; the kernel's forced flat (15:00 IST hard, OD-002).
- STT on exercise if held to expiry ITM (the engine **flags any position reaching expiry as a violation**, since the kernel forbids it).
- Upfront premium: buying power = cash − open premium − charges reserve.
- Session changes: pre-open 09:00–09:08, normal 09:15–15:30 (15:40 from 3-Aug-2026), CAS effects on the underlying at 15:15–15:35.

## 8.4 Event replay mode (for kernel testing)
The same replay can be driven with **fault scripts**:
- WebSocket drop for 5–120 s
- stale quotes
- crossed book
- broker 5xx/429 responses
- partial fills
- rejected SL order
- duplicate fill messages
- out-of-order order updates
- clock jump
- lot-size change mid-session
- instrument master missing the next expiry

Each fault has an expected kernel reaction (kill switch, flatten, halt), and those expectations are asserted in tests (18-backlog, K-T*).

## 8.5 Outputs per run
*Built so far (1-Oct-2026): `backtest/report.py` writes `ledger.jsonl`, which can be re-verified with `verify_ledger_file`. It also writes `cost_breakdown.json`, with brokerage, STT, exchange txn, SEBI fee, stamp duty and GST per fill and in total, the assumed half-spread per fill, and gross versus net. It writes `summary.json` as well, with the metadata: lake input fingerprints, assumptions, spec deviations and model versions. The rest of this section is still to do.*

- Trade ledger (Parquet), equity curve (net of all costs), exposure/Greeks time series.
- Stats: expectancy per trade in ₹ and in R, hit rate, payoff ratio, profit factor, Sharpe/Sortino **with bootstrap CIs**, max DD, time under water, tail (CVaR 95/99), turnover, cost share of gross P&L, and slippage sensitivity (P&L at 1×, 1.5×, 2×, 3× assumed slippage).
- Regime slices (P&L by regime label and by DTE bucket).
- A "what killed it" breakdown: P&L versus cost versus slippage attribution.

## 8.6 Anti-patterns that are banned
- Using close prices of the same bar for signal and fill.
- Using any vendor "IV" or "Greeks" computed with end-of-bar data at the start of the bar.
- Using today's instrument master or lot size for historical dates.
- Optimising for CAGR (§20). Model selection uses out-of-sample expectancy CI and robustness, never peak backtest return.

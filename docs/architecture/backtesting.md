# Backtesting architecture

Optimus Prime uses backtesting primarily as a **falsification environment**. A backtest is useful when it makes a strategy easier to reject for the right reasons, not when it produces the largest historical return.

The design goal is to keep research behaviour close to the paper/execution domain model while making assumptions explicit and reproducible.

## Principles

### Event-driven and point-in-time

Historical events are replayed in timestamp order. Strategy logic can only observe information that would have been available at the simulated decision time.

This rules out common forms of accidental look-ahead such as using end-of-bar values to make a start-of-bar decision or applying today's instrument metadata to historical contracts.

### Pessimistic when data is ambiguous

When historical data cannot establish an optimistic fill with confidence, the fill model should prefer the less favourable interpretation or mark the assumption explicitly.

Examples include:

- passive limit orders where queue position is unknown;
- stop orders around gaps;
- bars without bid/ask data;
- missing or stale option-chain observations;
- assumed rather than measured slippage.

### Costs are part of the strategy

Research is evaluated after explicit transaction costs and execution friction. Cost schedules are versioned by effective date so a long historical test does not incorrectly apply today's fees to every period.

A strategy that only has edge before realistic costs does not have deployable edge.

### Every experiment counts

Runs are registered with enough metadata to reproduce and audit them, including data fingerprints, code/config versions, parameters, seed and model versions.

The experiment history is retained so repeated trial-and-error is visible to validation rather than disappearing behind the final selected backtest.

## Components

| Component | Responsibility |
|---|---|
| **Replay feed** | merges historical market/reference streams into deterministic event order |
| **Simulated clock** | provides point-in-time session/calendar behaviour |
| **Market-state builder** | constructs the snapshot a strategy could actually have observed |
| **Fill model** | translates historical quotes/bars into conservative simulated executions |
| **Cost model** | applies dated explicit fees and configurable execution friction |
| **Portfolio / risk kernel** | uses the same portfolio/risk domain rules exercised in paper mode |
| **Experiment registry** | records every research run and its reproducibility metadata |
| **Analytics** | produces net-of-cost trade evidence, uncertainty and robustness diagnostics |

## Fill modelling

Different data quality supports different confidence levels.

### Quote-aware fills

When executable bid/ask data exists, fills can be modelled relative to the available quote and order limit. Passive fills remain conservative because historical top-of-book data does not automatically reveal queue priority.

### Bar-based fills

OHLC-only research requires stronger assumptions. The engine therefore treats spread/slippage as model inputs rather than pretending the bar contains an executable market.

If the path inside a bar is ambiguous, the simulator does not choose the sequence that helps the strategy.

### Gaps and protection

Protective orders are modelled with gap behaviour rather than assuming a stop always fills exactly at its trigger. This matters because risk estimates that depend on perfect stop execution are generally fragile.

## Fault replay

The replay environment also exercises system behaviour rather than only strategy P&L. Deterministic scenarios can inject conditions such as:

- stale or missing market data;
- feed interruption;
- partial fills;
- order rejection/timeouts;
- duplicate or reordered updates;
- reconciliation mismatch;
- clock/configuration anomalies;
- instrument/reference-data changes.

The expected outcome is a risk/execution state transition that can be asserted in tests.

## Research outputs

A useful run should make it possible to answer more than "did it make money?". Outputs can include:

- trade ledger and net equity path;
- gross versus net P&L and cost attribution;
- expectancy and payoff distribution;
- drawdown and time-under-water measures;
- bootstrap confidence intervals;
- turnover and cost share of gross P&L;
- slippage sensitivity;
- performance sliced by relevant regimes or contract features;
- explicit assumptions and data-quality limitations.

The emphasis is on understanding **why** a result exists and what would make it disappear.

## Anti-patterns

The research process deliberately rejects several convenient shortcuts:

- signal and fill using information from the same completed bar when that information was not yet available;
- present-day lot sizes, calendars or instrument metadata applied retrospectively;
- unlabelled synthetic spreads or execution assumptions;
- selecting a strategy because of peak CAGR rather than out-of-sample expectancy and robustness;
- discarding failed trials from the multiple-testing history;
- treating a backtest as evidence of future returns.

## Relationship to validation

Backtesting generates evidence; it does not promote a strategy by itself. Promotion is governed by the separate [validation framework](../research/validation.md), which considers out-of-sample behaviour, uncertainty, costs and robustness before a candidate can progress.

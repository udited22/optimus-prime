# Policy rules in force (OD-001 … OD-019)

Code, configs and tests cite these rules by ID (for example `OD-006` in `kernel/mandate.py`). Each rule is implemented
in versioned config and enforced by tests. This is the rule text only; the decision records are private.

| ID | Rule | Where it is enforced |
|---|---|---|
| OD-001 | Live trading starts as a small prove-the-machine canary that will mostly not trade; at most a handful of Governor-bound plumbing-test trades, only after the canary gate | [canary-criteria.md](canary-criteria.md) |
| OD-002 | Order window 09:15–15:00 IST, hard flat by 15:00; no place/modify/cancel outside it. Exchange sessions and the trading window are separate versioned configs | `configs/sessions/`, `sessions/`, Governor |
| OD-003 | Superseded by OD-008 (kept as `TW-2026-09-30` for reproducibility) | — |
| OD-004 | One live execution broker behind the broker-agnostic `BrokerAdapter`; default brokerage ₹20/order | `broker/`, `configs/costs/brokerage_plans.toml` |
| OD-005 | Per-trade max loss 2% of current NAV, costs included | `configs/risk/limits.toml` |
| OD-006 | Long options only; a SELL may only close an existing long (quantity ≤ open long minus pending sells) | `kernel/mandate.py` (the Governor's first check), StrategySpec schema, property tests |
| OD-007 | A position still open at 15:00 triggers the broker's Exit-All plus an urgent alert; a failed Exit-All halts and latches `POSITION_RECONCILIATION_KILL` | `kernel/runtime.py` |
| OD-008 | No new entries at or after 14:00; forced flatten from 14:50 | `TW-2026-10-01`, StrategySpec, Governor |
| OD-009 | Entries open at 09:20 (entry window 09:20–14:00); exits and cancels allowed from 09:15 | `TW-2026-10-01.2` |
| OD-010 | Drawdown suspension at 12.5% from the high-water mark (latched, owner reset); 15% is the hard ceiling | `RL-2026-10-01.1` |
| OD-011 | The historical-data client is data only: no order, portfolio or funds endpoint; token from the environment only | `data/dhan/`, `tests/data/test_dhan_data_only.py` |
| OD-012 | Whole-system economics are measured net of everything; the cost-justification check is advisory and can never block trading | `economics/` |
| OD-013 | A two-leg long straddle (same expiry and strike) may use 2 lots if the combined risk at both stops is ≤ 2% of NAV; the only exception to the 1-lot cap | `limits.toml`, Governor `NOT_A_STRADDLE` |
| OD-014 | At most 10 new entries a day system-wide, plus each spec's own cap; a straddle counts as one entry; on an event day only event-certified specs may enter | `limits.toml`, Governor `STRATEGY_MAX_ENTRIES` |
| OD-015 | Data-quality rules for vendor history: short opening vendor gaps are a WARNING; 0.001 OHLC float tolerance; everything else stays BLOCKING | `configs/dq/thresholds.toml`, `dq/checks.py` |
| OD-016 | Conflicting duplicate rows across vendor series are dropped and the minute is treated as missing | `backtest/lake_source.py` |
| OD-017 | Urgent alerts and the daily broker-token prompt go to one alert channel (env-only credentials); abnormal-market, broker-error and slippage kill thresholds; the core stays market- and broker-agnostic | `notify/`, `limits.toml`, `tests/test_architecture_boundaries.py` |
| OD-018 | Bank Nifty and Sensex join NIFTY in research scope; live rules unchanged | documentation only |
| OD-019 | Defined-risk option selling is allowed in RESEARCH and PAPER scope only (never live); sized by max loss ≤ 2% of NAV and stress-tested | `spec/structure.py`, `paper/structures.py` |

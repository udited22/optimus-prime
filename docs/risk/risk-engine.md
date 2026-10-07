# 09 — Risk Governor and Capital Preservation Kernel Specification

## 9.1 Nature
- A **pure, deterministic function** with no I/O, no LLM, no randomness:
  `evaluate(intent: TradeIntent, state: KernelState, limits: RiskLimits, market: MarketSnapshot, cost_model: CostModel) -> Decision{APPROVE(ticket) | REJECT(reasons[])}`
- The kernel state is rebuilt from the journal. Limits are a signed, versioned config file. Any change is a git commit plus owner approval, and it is loaded only outside market hours.
- The Governor's REJECT is final. No component may retry the same intent with modified parameters to "fit" the limits. The strategy must emit a new intent, with its own reason code, on a later decision cycle. The Governor also rejects intents whose stop was widened relative to the strategy's spec rule (§8, "never move stop to fit").

## 9.2 Limits for the ₹10k canary phase (owner-configurable, defaults from the directive)

| Limit | Default | Notes |
|---|---|---|
| Instruments | NIFTY index options only (CE/PE) | Instrument master check |
| Max open position | **1 lot** (65 qty) across all strategies | Aggregate check including pending orders. The only exception is OD-013 (2-Oct-2026). `strategy_max_lots` in `limits.toml` (RL-2026-10-02.1: S-VIXSTR-001 and S-IVRV-001 = 2) lets a two-leg LONG straddle hold one lot per leg. The second leg must be the other right with the same underlying, expiry and strike, otherwise it is NOT_A_STRADDLE. Nothing else may be open in the book. The summed worst case of both legs must fit the 2% budget; an unprotected or still-working leg counts its whole premium. A leg this Governor did not approve (for example after a restart) is refused |
| Max concurrent open orders | 1 entry + its protective exit | Prevents double fills |
| **Mandate: long options only (OD-006)** | **Any sell-to-open is forbidden**: no naked shorts, and no short leg of any spread, straddle, strangle, condor or fly | Deterministic rule `mandate.long_only`, evaluated **first**. BUY opens or adds a long. SELL is allowed only as **sell-to-close**: same instrument, qty ≤ (open long qty − qty of pending SELL orders). Anything else → REJECT `MANDATE_LONG_ONLY`. Implemented in `project100c.kernel.mandate` with unit and property tests |
| Unlimited-loss structures | Forbidden (implied by OD-006: a long option's max loss is the premium paid plus charges) | Belt-and-braces payoff check retained |
| Max intended loss per trade | **2% of NAV at decision time** (₹200 at ₹10k) | `risk_at_stop = (entry_limit − stop_expected_fill) × qty + round-trip charges (Governor's cost model) + slippage allowance (measured p90, or ≥ 2 ticks/side if not yet measured)` |
| Daily loss stop | **4% of start-of-day NAV**, realised + unrealised (mark-to-bid for longs) | Breach → DAILY_LOSS_KILL: flatten, no entries until next session pre-flight |
| Weekly freeze | **8%** of start-of-week NAV | Breach → trading frozen until owner review + diagnostic report |
| Drawdown from HWM | Warning at 10%; **suspend live at 12.5%** (**OD-010, owner-confirmed**); hard 15% (directive ceiling) | The directive says "suspend if approaching 15%". Suspension triggers at 12.5% so slippage on the exit cannot carry DD past 15% |
| Overnight positions | Forbidden | **Hard flat by 15:00 IST** |
| Trading window | **Order activity (place/modify/cancel) only within 09:15–15:00 IST** (OD-002) | Separate from the exchange session (F&O 09:15–15:40, cash 09:15–15:30 + CAS). Both are versioned config (`configs/sessions/`). Any order request outside the window → REJECT `OUTSIDE_TRADING_WINDOW` |
| Holding to expiry | Forbidden | Exit expiring positions by 15:00 (the flat deadline) |
| Forced flatten | From **14:50 IST** (**OD-008, owner-confirmed**), escalating LIMIT exits so the book is flat by 15:00 | If still not flat at 15:00 (**OD-007**): trigger the broker's **Exit-All** through the adapter and send an URGENT owner alert. If Exit-All fails or only partly succeeds: halt all trading, keep alerting, latch POSITION_RECONCILIATION_KILL. Exit-All pricing is UNVERIFIED until the Upstox sandbox test. Exit-All is the only order activity allowed at or after 15:00 |
| Averaging down / pyramiding | Forbidden unless the spec defines a validated multi-entry *and* total risk ≤ budget | |
| Martingale / size-up after loss | Forbidden; size is a function of NAV and evidence only | Checked: no size increase within N trades after a loss unless NAV rule dictates |
| Max new entries/day | **10 system-wide** (OD-014, `max_trades_per_day`; was 3), plus each spec's `entry.max_entries_per_day` under it (`STRATEGY_MAX_ENTRIES`; an unlisted strategy gets 1). A long straddle (OD-013: CE+PE, same expiry and strike) counts as ONE entry: its completing leg is journalled `counts_as_entry = false` | Limits cost bleed and revenge patterns. The count depends on the strategy mix; the daily loss stop and the drawdown limits still bind first |
| Cooldown after a stop-out | 15 minutes, per strategy | |
| Minimum liquidity | spread ≤ max(2 ticks, 3% of mid); top-of-book size ≥ 2 lots; OI ≥ configurable | Else REJECT (LOW_LIQUIDITY) |
| Price sanity | limit within [bid − 5 ticks, ask + 2 ticks] and inside exchange price band | Else REJECT |
| Stop protection | Every position needs a confirmed protective SL-limit at the broker (or a kernel stop monitor if the broker rejects) within 3 s of fill | Else flatten |
| Buying power | premium + charges reserve ≤ available cash − ₹500 buffer | Upfront premium rule (F1) |
| Strategy status | Only CANARY/PRODUCTION strategies can get APPROVE for live; SHADOW produces tickets marked `SIMULATE_ONLY`. PAPER is approved only by a Governor built with `paper_venue=True`, and the runtime refuses that Governor unless its broker declares `paper_venue` (the fake broker / PaperBroker). So a PAPER intent can never reach a real broker. | |
| Entry window | No new entries at or after **14:00 IST** (**OD-008**, superseding OD-003's 14:45); no entries before **09:20** (**OD-009, owner-confirmed**) | Leaves 50 min before the 14:50 flatten start and an hour before the 15:00 hard flat; post-14:45 no-cure snapshot rule (F3); opening noise |
| Event days | Entries only for strategies with `event_certified: true` on EVENT_REGIME days (RBI, Budget, election results, Fed, US CPI). An event day is the calendar flag or the classifier's `EVENT_REGIME` tag. The flag comes from the spec, never the intent, and needs a VALIDATED event-day gate result on record (docs/research/validation.md §13.2a). None is certified (2-Oct-2026). US FOMC statements and CPI releases land after the NSE close (`impact: next_session`), so their EVENT_REGIME day is the next Indian trading session (`configs/calendar/events.yaml`). | §14 |

**Owner decision OD-005:** the per-trade limit stays **2% of current NAV** during the canary. A ₹2,000 per-trade limit is to be **reviewed at NAV ≈ ₹1 lakh** (≈ 2%); that is a review gate, not an automatic change.

**Scaling rule (later phases):** `risk_budget_per_trade = min(2% NAV, f × Kelly_fraction(estimated edge lower CI) × NAV)` with `f ≤ 0.25` (capped fractional Kelly). Concentration caps come down as NAV grows (e.g. max 20% of daily risk in one strategy once there are ≥ 3 PRODUCTION strategies). Changing it needs owner approval.

## 9.3 Capital Preservation Kernel state
Tracked continuously and journalled:
- NAV (cash + mark-to-bid of longs − mark-to-ask of shorts − accrued charges), HWM, realised/unrealised P&L (total and per strategy)
- Drawdowns: daily, weekly, monthly, from HWM
- Gross/net exposure (premium and delta-notional); portfolio Greeks Δ, Γ, ν, Θ from own IV
- Liquidity score of held contracts; concentration by strategy/expiry/strike
- Order state per order (see 10 §10.4); reconciliation status and age

## 9.4 Kill switches

| Kill | Trigger (initial thresholds; tuned in Phase 1/3) | Action | Reset |
|---|---|---|---|
| STRATEGY_KILL | Strategy breaches its own loss/slippage/behaviour bounds (e.g. 3 consecutive stops, slippage > 2× assumed on 3 trades, emitted invalid intents) | Strategy → DEGRADED; its orders cancelled and positions exited | Validation agent report + owner OK |
| PORTFOLIO_KILL | Aggregate exposure or Greek limit breach; unexplained P&L jump > 1% NAV | Cancel all, flatten, halt entries | Owner |
| DAILY_LOSS_KILL | Daily loss ≥ 4% | Flatten, halt for the day | Automatic next-day pre-flight |
| DATA_QUALITY_KILL | ATM quotes stale > 3 s; feed disagreement; timestamp drift > 3 s sustained; instrument master inconsistent | Block entries. Exit open positions using the last good protective order at the broker; do not market-chase | Automatic after DQ healthy for 60 s + resync |
| BROKER_CONNECTIVITY_KILL | Order API errors/timeouts above threshold; WebSocket down > 10 s; session invalid | Block entries. Verify the protective SL rests at the broker. If broker state is unknowable, alert the owner immediately | Reconnect + full reconciliation |
| ABNORMAL_MARKET_KILL | Index move > X% in Y min, VIX jump > Z%, market-wide circuit, exchange halt, CAS anomaly | Block entries, tighten or execute exits per spec | Owner or cool-off timer |
| POSITION_RECONCILIATION_KILL | Any mismatch in positions/orders/trades between journal and broker | Block entries. The broker is treated as the truth for positions; flatten any unexpected position | Owner review of reconciliation report |
| SYSTEM_INTEGRITY_KILL | Config/code hash mismatch, clock drift > 250 ms, disk < 10%, journal write failure, process crash-loop, memory pressure | Block entries, flatten, halt | Owner |
| MANUAL_MASTER_KILL | Owner command (CLI on host, signed message, or broker-app manual action) | Cancel all, flatten, halt. Survives restart | Owner only |

Kill states are **latched** in the journal. A restart never clears a kill.

### 9.4a Thresholds chosen under OD-017 (2-Oct-2026, RL-2026-10-02.3)
the owner delegated the remaining thresholds (OD-017). They are set for capital preservation; the reasoning is in OD-017.

| Kill | Rule | Where |
|---|---|---|
| ABNORMAL_MARKET_KILL | NIFTY more than 2% from the session open (either way); more than 1% inside 5 min; India VIX up more than 20% since the open; market-wide circuit or exchange halt. Cool-off 30 min or owner reset; it re-latches while the condition holds | `health.detect_kills` (inputs from `health.market_moves`); the Governor also refuses entries on the same thresholds from the intent's own snapshot (check 18, `ABNORMAL_MARKET`) |
| BROKER_CONNECTIVITY_KILL | 3 order/API errors in a row (order path and read path counted separately), or 5 errors in 15 min | `KernelRuntime` counters → `HealthObservation.broker_consecutive_errors` / `broker_errors_in_window` |
| STRATEGY_KILL (slippage) | over the last 5 fills, realised slippage above 2× modelled in total; or any single fill above 3× its modelled slippage | `KernelRuntime._observe_slippage` journals `SLIPPAGE_OBSERVED`; `governor.slippage_breach` in `required_actions` |

- **Modelled slippage** is the per-unit, per-side allowance the risk budget uses (`governor.modelled_slippage`: the measured p90, never below 2 ticks). **Realised** is the adverse distance of a submitted intent's fill from the decision-time mid; a better fill counts as 0. Forced flattens and Exit-All orders have no reference price and are not measured. Protective SL-limit fills are not measured either: they cannot fill below their limit, and the budget already assumes a fill at the limit plus the modelled slippage.
- **Limit of the rule:** entries are marketable limits capped at the ask + 2 ticks (price sanity), so an entry fill can exceed the 2-tick allowance by at most about 1.5× (half a 2-tick spread plus 2 ticks). The rule bites mostly on strategy exits (limits down to the bid − 5 ticks) and once a lower measured p90 is configured.
- **Reason code added:** ABNORMAL_MARKET (44 in total), mapped to the RISK "Quote and liquidity checks" assembly.

## 9.5 Verification requirements
- 100% branch coverage on the Governor. **Property-based tests**, e.g. "for any sequence of intents and fills, open qty ≤ 1 lot" and "no approved intent has risk_at_stop > budget".
- A golden-scenario suite of ≥ 50 scripted days, including all kill triggers.
- **Mutation testing** (e.g. `mutmut`) on the Governor: a surviving mutant is a test gap.

## 9.6 Implementation status (K-02, 1-Oct-2026)
- **Code:**
  - `src/project100c/kernel/governor.py`: `RiskGovernor.evaluate` and `required_actions`.
  - `limits.py` plus `configs/risk/limits.toml`, version `RL-2026-09-30.1`.
  - `kills.py`: the nine kills, halts and reset rules. `health.py`: observation triggers.
  - `state.py`: the journal reducer. `runtime.py`: the harness against the fake broker.
- **Owner sign-off:** nothing is pending. The abnormal-market, broker-error and slippage thresholds were delegated by OD-017 and are set in `RL-2026-10-02.3` (§9.4a).
- **Decided and implemented:**
  - Entry start 09:20 (OD-009), trading-window version `TW-2026-10-01.2`.
  - DD suspension at 12.5% (OD-010), limits version `RL-2026-10-01.1`. The validator enforces warning < suspend < hard ≤ 15%.
  - Entry cutoff 14:00 and flatten from 14:50 (OD-008).
  - 15:00 residual → broker Exit-All + URGENT alert. Failure or partial → `EXIT_ALL_FAILED` halt (all trading) + POSITION_RECONCILIATION_KILL + repeated alerts (OD-007).
- **Observations from testing:**
  - At ₹10k and 1 lot, the worst single-trade loss (premium to ≈ 0: ≈ ₹195 + charges) is below the ₹400 daily stop. With up to 10 entries a day (OD-014) at ≤ ₹200 each, two full stop-outs bring the day to the ₹400 stop: DAILY_HEADROOM refuses an entry whose worst case would breach it, and DAILY_LOSS_KILL latches on a breach. The path is tested with test-only limits.
  - A **cancel/fill race** (the exchange fills part of an order after our cancel was journalled) is real. The reducer applies such late fills to the closed order instead of flagging a false reconciliation mismatch, and the runtime alerts about it.
  - A newly latched kill is acted on in the **same** runtime step. For example, DAILY_LOSS → flatten immediately.
- **Not yet met (AT):**
  - Branch coverage is 99%; the target is 100%.
  - Mutation testing has not been run.
  - The golden-scenario suite of ≥ 50 scripted days is not built. The failure-injection tests cover each kill trigger and the Exit-All failure modes.

## 9.7 Regime gate (check 17, 2-Oct-2026)
- **Code:** `kernel/regime_gate.py` (`RegimeGate`, `RegimeReading`) and check 17 in `RiskGovernor._entry`. Enabled by passing `regime_gate=RegimeGate.from_specs(specs)` to the Governor; without a gate the Governor behaves exactly as before.
- **Policy source:** the registered StrategySpec, looked up by `strategy_id`. The intent cannot carry or vouch for its own regime. An unknown strategy id is refused (REGIME_NOT_ALLOWED).
- **Reading:** `MarketSnapshot.regime`, built from the K-11 classifier's latest label (`RegimeReading.from_label`). Missing → REGIME_UNKNOWN. Older than 150 s, or stamped in the future → REGIME_STALE.
- **Unvalidated classifier (docs/research/validation.md §13.5):** for CANARY and PRODUCTION intents an unvalidated reading becomes {NO_EDGE}, which every spec prohibits → REGIME_BLOCKED. SHADOW (simulate-only) and PAPER (paper venue, virtual money) intents see the labels as they are.
- **Exits are never regime-blocked.** Leaving a position on a regime change is the strategy's invalidation rule, sent as a sell-to-close.
- **Reason codes added:** REGIME_UNKNOWN, REGIME_STALE, REGIME_BLOCKED, REGIME_NOT_ALLOWED (43 in total). The connectome maps them to the RISK "Quote and liquidity checks" assembly.


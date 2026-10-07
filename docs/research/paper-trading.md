# 14 — Paper-Trading Plan (Phase 3) and Shadow Live

## 14.1 Paper trading
- **What:** the full deterministic trading plane runs live, with the execution adapter replaced by `PaperBroker`. PaperBroker fills against the **live executable quote** (ask for buys, bid for sells; limits fill only if the market trades through or the quote crosses the limit). It includes the measured latency and a queue-position haircut. Charges come from the cost model.
- **Scope:** every VALIDATED strategy. They are allocated virtual capital equal to the **intended canary NAV**, not an inflated amount, and all Governor limits are enforced exactly as in live.
- **Timing:** paper and shadow run with the **same trading window as live**. That means order activity only 09:15–15:00 IST, no new entries at or after 14:00, forced flatten from 14:50, and hard flat by 15:00 with broker Exit-All for any residual (OD-002/OD-007/OD-008). This keeps paper results representative of the live constraints.
- **Duration / minimums (per strategy):** ≥ **20 trading sessions and ≥ 30 trades**, whichever is later. Low-frequency strategies need an owner waiver with wider CIs.
- **Daily operations:** the full §16 cycle runs, including the pre-market checklist, reconciliation (paper journal vs PaperBroker state) and the daily owner report (17).
- **Data capture:** every quote around each decision (±60 s) is stored, building the proprietary spread/slippage dataset.
- **Exit criteria to SHADOW:**
  - net expectancy in paper within the validated CI
  - no kernel incident
  - DQ kill rate < 1 per 5 sessions
  - fill-model sanity: PaperBroker fill rate on passive limits within 20% of the backtest assumption

- **Kernel support (built).** A PAPER strategy needs `RiskGovernor(paper_venue=True)` (docs/risk/risk-engine.md). `KernelRuntime` refuses to pair that Governor with a broker that does not declare `paper_venue`. Every other Governor rule is the live rule. `KernelRuntime.cancel_entries()` cancels an entry the strategy no longer wants, and nothing else.
- **Paper loop (built, SIMULATED; `src/project100c/paper/`).** `PaperLoop` runs library strategies on seeded SYNTHETIC days: K-11 reading, then the plug-in signal, then allocator v1, then `TradeIntent`, then the Governor (paper venue, `RegimeGate` on), then `KernelRuntime`, then `FakeBroker`. Quotes are the synthetic option close ± an ASSUMED half-spread. The loop decides entries and the spec's exits (`request_exit`) and cancels entries not filled in time (`cancel_entries`). The kernel owns the stop, the 14:50 flatten and the 15:00 Exit-All. Each signal outcome is recorded: SKIPPED, ALLOCATOR_REFUSED, GOVERNOR_REJECTED or APPROVED, with reasons; repeats are folded.
- **PaperBroker and the host step (built 3-Oct-2026, scripted quotes only; `broker/paper.py`, `paper/live.py`).** `PaperBroker` applies the fill rules above: buys at the ask and sells at the bid only when the limit is reached; ack latency 0.3 s and fill latency 0.5 s (ASSUMED until K-B4 measures them); a queue haircut of 50% of the displayed size, used up until the next quote; no fill on a quote older than 3 s, future-dated, or without a size. It runs behind the same `ExecutionGateway` and `KernelRuntime` as live. `LivePaperSession.step()` ticks the daily token gate (docs/engineering/alerts-and-daily-token.md), handles the owner's Telegram commands (`/kill`, `/deny`, `/status`), feeds quotes, steps the runtime and submits intents only when the gate allows; a `/deny` with open positions asks each to exit every 30 s. The live quote feed client is not built, so the 20-session run (P-02) cannot start yet.
- **What it shows (tested; SYNTHETIC).**
  - A disallowed regime is refused end to end.
  - The book-wide 1-lot cap binds, and so does the system-wide entry cap. The test lowers `max_trades_per_day` to 3 to show it; the live value is 10 (OD-014), with each spec's own cap under it.
  - On the verified 7-Oct-2026 RBI MPC day every entry is refused: no strategy is event-certified.
  - At the ₹10,000 canary NAV a one-lot NIFTY option does not fit the allocation, so nothing trades.

## 14.2 Shadow live
- **What:** the real broker session is authenticated, and orders are **constructed, risk-checked and ticketed with `SIMULATE_ONLY`**. The gateway calls broker **read-only** endpoints: margin/charges calculation where available, order-book and quote snapshots at the decision instant. It does **not** place orders.
- **Purpose:** validate the broker adapter end to end (auth, instruments, streaming, positions/funds reads, margin API), measure real latencies to the broker, and exercise daily re-auth under the real static-IP setup.
- **Kernel fault drills (mandatory before canary), run in shadow with the fake broker plus the real feed:**
  - kill the WebSocket mid-position
  - revoke the session
  - clock skew
  - disk full
  - journal corruption
  - instrument-master missing tomorrow's expiry
  - lot-size change
  - duplicate fill messages
  - stop rejected
  - MANUAL_MASTER_KILL

  Each drill has an expected outcome and a pass/fail record. **All must pass** (§23: "no capital until the kernel is deterministic under simulated failures").
- **Duration:** ≥ 10 sessions of stable operation after the last code change to the kernel.

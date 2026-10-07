# 01 — Target Architecture

Status: DESIGN, v0.1 (30-Sep-2026). No code, no broker, no capital.

> **Optimus Prime.** Project 100C, the NIFTY index-options system, is the first vertical slice of Optimus Prime; the broader architecture is moving toward market-wide opportunity discovery, multi-strategy allocation and more asset classes. Bank Nifty and Sensex are in research scope (OD-018); the live trading rules are unchanged. This document is kept as written (30-Sep-2026 design).

---

## ⚠️ PART A — FEASIBILITY FACTS THE OWNER MUST SEE FIRST

These numbers come from verified rules and charges (see [`../engineering/sources.md`](../engineering/sources.md)) and the reproducible script `tools/feasibility_calc.py`. The option **premiums are model estimates, not quotes**: Black-Scholes, NIFTY 22,716 (close reported for 29-Sep-2026), IV ≈ India VIX 13.3%.

### A1. What one NIFTY option lot costs today

| Fact | Value | Source |
|---|---|---|
| NIFTY lot size | **65** (since Jan-2026 expiries) | S10, S11 |
| Weekly expiry | **Tuesday** (NSE; monthly = last Tuesday; holiday → previous trading day) | S12, S13 |
| Notional of 1 lot | 65 × 22,716 ≈ **₹14.8 lakh** | calc |
| Est. ATM call premium | ~41 pts (expiry-day afternoon), ~74 (1 day), ~123 (3 days), ~174 (6 days) | calc (estimate) |
| Cost of 1 ATM lot | **₹2,650 – ₹11,300**, i.e. 27%–113% of a ₹10k NAV | calc |
| Est. near-OTM (+100 to +300) premiums | ~2–125 pts depending on DTE → ₹160 – ₹8,150 per lot | calc |
| Option buyers pay the full premium upfront | yes (since 1-Feb-2025) | S9 |

### A2. What a 2% (≈₹200) max loss implies

- **Stop width = ₹200 ÷ 65 = 3.08 premium points in total**, before costs and slippage.
- Round-trip statutory + exchange charges for one lot (buy + sell, rates as of 30-Sep-2026):

| Premium | At ₹20/order: **Upstox, the chosen live broker (OD-004)** | At ₹0 API brokerage (e.g. Kotak Neo; reference only) |
|---|---|---|
| ₹10 | ₹48.7 (0.49% of NAV, 24% of the ₹200 budget) | ₹1.5 (1%) |
| ₹30 | ₹51.8 (0.52% NAV, 26%) | ₹4.6 (2%) |
| ₹60 | ₹56.5 (0.56% NAV, 28%) | ₹9.3 (5%) |
| ₹100 | ₹62.6 (0.63% NAV, 31%) | ₹15.4 (8%) |
| ₹150 | ₹70.3 (0.70% NAV, 35%) | ₹23.1 (12%) |

  Components: STT 0.15% of sell premium, NSE 0.03553% each side, SEBI ₹10/crore, stamp 0.003% buy side, GST 18% on (brokerage + exchange + SEBI).
- **Slippage is on top of charges.** It is UNVERIFIED and must be measured. At 0.5 pt per side it costs ₹65 per round trip (32% of the budget). At 1 pt per side it costs ₹130 (65%).
- **Net stop room at ₹10k:** ₹200 − ~₹49 charges − ₹65 slippage ≈ ₹86 ≈ **1.3 points** at a ₹20 broker. At ₹0 API brokerage it is about ₹133 ≈ **2.0 points**.
- **Restated for Upstox (OD-004, ₹20/order is the live default):** the ₹20 column applies. About ₹40 of every round trip (brokerage) plus ₹7–8 GST is fixed. The H07 example (₹8 premium, 2-pt stop, 0.25 pt/side slippage) now costs **₹210.68 at the stop, above the ₹200 budget**, so **no drafted hypothesis is eligible at exactly ₹10k NAV**. (H07's example needs NAV ≥ ≈ ₹10,534.) An OD-001 plumbing-test trade can still fit, e.g. a ₹5 option with a 1-pt stop: ₹65 + ₹47.85 charges + ₹32.50 slippage = **₹145.35 ≤ ₹200**.
- A realistic ATM stop is roughly ≥ 20–30% of premium, i.e. 10–45 points. At ₹10k that stop would lose **₹650 – ₹2,900, which is 6.5% – 29% of NAV**. That breaks the 2% rule by 3–15x.
- *(Moot under OD-006, long options only: spreads are prohibited. Kept for the record.)* A **defined-risk debit spread** has 4 orders. That is ~₹95 in brokerage + GST alone at a ₹20 broker (47% of the budget). The max loss (net debit × 65) must also be ≤ ₹200, so the net debit must be ≤ ~3 points. That forces far-OTM structures.
- A **credit spread**, even a 50-point-wide one, has a max loss of 50 × 65 = ₹3,250 = 32% of NAV. It is **not permitted** under the 2% rule at ₹10k.

### A3. Verdict on ₹10,000

**The directive's own rules almost never allow a trade in a ₹10k account.** The Risk Governor (§8: "if smallest executable position exceeds risk budget, do not trade; never move stop to fit") will reject almost every ATM and near-OTM NIFTY option trade. The only trades left are low-priced options (≈₹5–15 premium, low delta, usually 0–1 DTE) with 1.3–2.5 point stops. Such stops sit inside normal bid-ask and tick noise. They are statistically unlikely to show an edge net of costs, and no such edge has been demonstrated.

Fixed monthly costs are also large relative to ₹10k. One data subscription such as Dhan Data API costs ₹499 + GST ≈ ₹589 a month, about 5.9% of NAV per month. A static-IP VPS adds more (price UNVERIFIED). **Infrastructure cost must be budgeted separately from trading NAV**, or it alone will breach the drawdown limits.

**Minimum capital that makes the rules workable** (one lot; stop = 25% of premium; round-trip friction ≤ 25% of the per-trade budget; premium outlay ≤ 25% of NAV):

| Option premium traded | Minimum NAV |
|---|---|
| ₹10–20 (far OTM) | ≈ ₹23,000 |
| ₹30 | ≈ ₹30,000 |
| ₹60 (near OTM) | ≈ ₹55,000 |
| ₹100 (ATM, 1–3 DTE) | ≈ ₹87,500 |
| ₹150 (ATM, ~5 DTE) | ≈ ₹1.28 lakh |

**Recommendation.** Keep the ₹10k phase as "prove the machine" (§18). In practice, most days will be NO TRADE, and that is valid by design (§1). Real strategy validation on ATM/near-OTM structures needs about **₹55k–₹1.3 lakh**, and only after paper and shadow evidence. The owner decides the capital amount (see Open Decisions in README). The system will not loosen its rules to fit ₹10k.

### A4. Reality check

- SEBI's Aug-2026 study (S22) shows **87.7% of individual equity-derivatives traders lost money in FY26** (90.9% in FY25). Average loss per trader was ₹1.17 lakh. Options made up 92% of individual losses. **99% of FPI and proprietary-trader profits came from algo entities**, mostly large, well-capitalised firms.
- **How the system treats return targets:** they cannot change risk. The only optimisation target is long-run log growth under constraints (§2).

---

## PART B — ARCHITECTURE

### B1. Design principles
1. **Two planes, one-way trust.** A *Research/Advisory plane* (LLM agents, notebooks, backtests) and a *Deterministic Trading plane* (strategy runtime, Risk Governor, OMS, gateway). Information flows from research to trading only as **versioned, validated, signed artefacts**: strategy packages and config. There is never a live channel.
2. **The kernel defaults to STOP.** Every uncertain state (data, broker, reconciliation, clock, config hash) resolves to *no new entries*, then *flatten if safe*, then *halt*.
3. **Event-sourced truth.** Every market event, decision, intent, order state change and fill is appended to an immutable journal. State can be rebuilt by replay, and backtest, paper and live share the same code path.
4. **Same code, three modes.** `BACKTEST | PAPER | LIVE` differ only in the injected clock, market-data source and execution adapter.
5. **Nothing hard-coded about contracts.** Lot size, expiries, tick size, freeze qty and trading hours come from a daily-refreshed, validated instrument master and calendar.

### B2. Component map

```
                        ┌──────────────────────── RESEARCH / ADVISORY PLANE (no broker access) ───────────────────────┐
                        │  CIO/Orchestrator agent   Market-Intel agent   Research agents   Validation (red-team) agent │
                        │        │ reports/recommendations only            │ code PRs + experiment records             │
                        │        ▼                                         ▼                                           │
                        │  Experiment Registry ◄── Backtester / Event Replay ◄── Data Lake (Parquet + DuckDB, read-only)│
                        └────────────────┬──────────────────────────────────────────────────────────────────────────────┘
                                         │ signed, versioned StrategyPackage + promotion record (human-approved gate)
                                         ▼
┌──────────────────────────────────── DETERMINISTIC TRADING PLANE (separate host/user, no LLM) ───────────────────────────────────┐
│ Market Data Ingest ─► Data Quality Gate ─► Market State / Regime Classifier (validated, deterministic)                            │
│        │ (ticks, chain, depth)      │ DQ flags                     │ regime vector                                                   │
│        ▼                            ▼                              ▼                                                                │
│  Tick Recorder (journal)     Strategy Runtime (only PROMOTED/CANARY/SHADOW packages) ─► TradeIntent (typed)                        │
│                                                                              │                                                      │
│                                                                              ▼                                                      │
│               Capital Preservation Kernel ◄──► RISK GOVERNOR (pure function, absolute veto, kill switches)                         │
│                (NAV, HWM, DD, Greeks,              │ approved intent (with risk ticket)                                            │
│                 exposure, reconciliation)          ▼                                                                               │
│                                           Order Construction Engine (limit/SL-limit, price bands, tick rounding, protection)       │
│                                                    ▼                                                                               │
│                                           Broker Execution Gateway (re-validates, rate-limits ≤ broker/TOPS, idempotency keys)     │
│                                                    ▼                                                                               │
│                                           Broker adapter (Upstox, OD-004; interface broker-agnostic)  ──►  Broker ──► NSE                         │
│                                                    ▲                                                                               │
│                           Reconciler (independent: broker positions/orders/trades vs internal journal every N sec)                 │
│ Audit Journal (append-only, hash-chained) ◄── everything above                                                                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
          │ metrics/logs (one-way export)                                   ▲ MANUAL_MASTER_KILL (owner, out-of-band)
          ▼                                                                 │
   Observability: Prometheus + Grafana, log store, daily owner report (read-only)
```

### B3. Runtime topology (Phase 1–4)
- **Trading host.** One small Linux VM in an Indian cloud region (e.g. Mumbai) with a **static public IP**, whitelisted with the broker as SEBI/NSE require (S2). It runs the kernel processes under a dedicated service user. It exposes no inbound ports except SSH via key plus an allow-list.
- **Research host.** It can be the existing box or a separate machine. It holds the data lake, backtests and LLM agents. It has **no broker credentials and no route to the trading host's secrets**. It receives read-only exports of journal and metrics.
- **Processes on the trading host**, supervised by systemd with watchdogs:
  `md_ingest`, `dq_gate`, `state_engine`, `strategy_runtime`, `risk_governor`, `oms_gateway`, `reconciler`, `journal_writer`, `metrics_exporter`, `kill_listener`.
  Inter-process communication is a local message bus with typed messages (see 02-tech-stack). Every message carries a monotonic sequence and a config hash.
- **Clock.** Chrony/NTP sync is required. A drift above 250 ms against exchange timestamps raises `SYSTEM_INTEGRITY_KILL` (threshold to be tuned in Phase 1).

### B4. Mode matrix
| Mode | Data source | Execution adapter | Capital |
|---|---|---|---|
| BACKTEST | Historical lake replay | Simulated fill model (conservative) | none |
| PAPER | Live feed | Simulated fills against the **live executable quote** (bid/ask + depth) | none |
| SHADOW LIVE | Live feed | Orders built and risk-checked, **not sent**. Fill simulation on real quotes, with latency measured | none |
| CANARY LIVE | Live feed | Real broker, 1 lot max, canary budget | owner-approved canary NAV |
| PRODUCTION | Live feed | Real broker | allocator-approved |

### B5. Safety invariants (enforced in code and tested)
1. There is no code path from any LLM process to `oms_gateway`.
2. `oms_gateway` accepts only messages signed by `risk_governor` with a valid risk ticket. The ticket is bound to intent id, qty, price limit and expiry of 2 s.
3. Open positions ≤ 1 lot of NIFTY options in the ₹10k phase. **Long options only (OD-006):** no sell-to-open ever. A SELL is only a sell-to-close of an existing long, qty ≤ open long qty.
4. Every open position has a resting protective SL-limit order at the broker, or a kernel-side stop monitor if the broker cannot hold one. If neither is confirmed within T seconds, the position is flattened.
5. When the reconciler finds a mismatch, entries are blocked immediately and `POSITION_RECONCILIATION_KILL` is raised.
6. No deployment, config change or package promotion is accepted between 08:45 and 16:30 IST on trading days (§13). The allowed exception is a kill/halt command.
7. Order activity only 09:15–15:00 IST. No new entries at or after **14:00** (OD-008, superseding OD-003's 14:45), forced flatten from 14:50, and hard flat by **15:00 IST**, with the broker's Exit-All triggered for any residual position (OD-002/OD-007/OD-008). That is well before the 15:40 F&O close and the 15:10–15:40 closing-price window (S18). The system never holds to expiry. That avoids the 0.15% STT on the intrinsic value of exercised options (S14) and any physical/exercise complexity.

### B6. What is deliberately *not* in scope
- Latency-sensitive strategies. A retail API in 2026 has broker-side latency of about 50–100 ms by the brokers' own claims (S26, S33), a 10 OPS cap, and no market/IOC orders for algos.
- Offering the system or its signals to anyone else. That would make it an algo provider or research analyst activity under SEBI (S1, S6). It is personal use only, with family use permitted.
- Overnight positions (initially), **any option selling including spread legs (OD-006)**, and averaging down.

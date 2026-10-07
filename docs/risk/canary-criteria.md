# 15 — Canary Deployment Criteria (Phase 4)

## 15.1 Entry gate (ALL required; the owner signs the checklist)
- [ ] Kernel: Phase 1 acceptance tests 100% green; Governor mutation score ≥ 90%; all fault drills (14 §14.2) passed within the last 10 sessions.
- [ ] Reconciliation: 100% match over ≥ 10 shadow sessions (positions, funds, order states).
- [ ] Broker: **Upstox** readiness report K-B4 passed (03 §3.5, OD-004); static IP whitelisted (primary + secondary); daily auth procedure documented and compliant with the broker's terms; orders confirmed tagged as algo orders (check contract note or order-book field where visible).
- [ ] Compliance checklist (04) re-verified within the last 7 days against the NSE/SEBI websites; Regulatory-Watch report clean.
- [ ] At least one strategy in SHADOW with passing criteria **and** `min_capital_inr ≤ canary NAV`. **If no strategy is eligible at the approved canary NAV, the canary does not start.** The kernel alone may be canaried with a **plumbing test**: see 15.3.
- [ ] Owner-approved canary NAV, limits file hash, and the out-of-band MANUAL_MASTER_KILL procedure (including logging into the broker app manually to exit). The OD-007 Exit-All path must have been exercised and its pricing verified. Exit-All is **not** sandbox-enabled at Upstox (verified 3-Oct-2026), so this happens in the live plumbing test, not the sandbox (K-B2 covers only place, modify and cancel).
- [ ] Separate infrastructure budget approved (data, VPS), so fixed costs are not taken from the trading NAV.

## 15.2 Canary operating rules
- 1 lot max; the directive's ₹10k limits (09 §9.2) scaled to the canary NAV; no overnight; order activity only 09:15–15:00 IST; no new entries at or after 14:00; forced flatten from 14:50; **hard flat by 15:00**, broker Exit-All for any residual (OD-002/OD-007/OD-008).
- Maximum **one strategy live** at a time for the first 20 live trades.
- Daily review by the owner of the report (17) for the first 10 sessions.
- **Automatic halt conditions (in addition to kill switches):**
  - any reconciliation mismatch
  - any order found in an `UNKNOWN` state > 10 s
  - realised slippage > 3× assumption on any single trade
  - 2 DAILY_LOSS_KILLs within 10 sessions
  - drawdown ≥ 12.5% from HWM

## 15.3 Kernel plumbing test (optional, owner decision)
If no strategy is capital-eligible, the owner may approve a small number (e.g. ≤ 5) of **scripted, minimum-risk live orders** to prove the live order path: place, ack, protective SL, cancel/exit, reconcile, contract-note match. Example: 1 lot of a low-priced option with a pre-defined stop and risk ≤ ₹200 including costs. Its P&L is booked as an **infrastructure test cost**, not strategy evidence.

## 15.4 Exit from canary
- **To PRODUCTION:** 13 §13.4 criteria.
- **To SUSPENDED:** any halt condition; review and a root-cause report are required before re-entry.

# 13 — Validation Methodology (Research Validation Agent = hostile reviewer)

## 13.1 Data partitioning (fixed before any research)
- **Untouched holdout:** the most recent **6 months** of history available at Phase 2 start, plus a random **20% of weeks** from earlier periods (stratified by year). Only the Validation agent holds the access key. Each strategy version may be evaluated on it **once**. A second look requires a new version and is logged as a new trial.
- **Development set:** everything else, used with **rolling walk-forward**: train 12 months → test 1 month, rolled monthly. Parameters are chosen only on training windows.
- **Rule-era segmentation.** Results are reported separately for:
  - pre-20-Nov-2024 (multiple weekly expiries; old lot sizes)
  - 20-Nov-2024 → 31-Aug-2025 (single weekly index per exchange; Thursday expiry)
  - 1-Sep-2025 → present (Tuesday expiry; lot 65 from Jan-2026; STT 0.15% from Apr-2026; 15:40 close from Aug-2026)

  A strategy must be positive in the **most recent era** on its own merits.

## 13.2 Promotion gate BACKTESTED → VALIDATED (all must pass)
| # | Test | Pass criterion |
|---|---|---|
| V1 | Look-ahead / leakage audit | Automated: every feature's max input `ts` < decision `ts` − latency; shuffled-future canary feature yields no edge |
| V2 | Survivorship / universe | Contract universe from point-in-time instrument master/bhavcopy; expired contracts included |
| V3 | Realistic costs | Dated cost model (04 §4.4), applied per order |
| V4 | Realistic fills | Pessimistic fill model; stops can gap; limit fills need trade-through |
| V5 | OOS expectancy | Walk-forward OOS net expectancy > 0 with **90% bootstrap CI lower bound > 0** (block bootstrap by day) |
| V6 | Sample size | ≥ 100 OOS trades (≥ 40 in the most recent era). Rare-event strategies need Bayesian shrinkage + owner waiver |
| V7 | Concentration | Top 5% of trades contribute < 50% of net P&L; not dependent on one month/regime |
| V8 | Parameter robustness | Net expectancy stays > 0 across ≥ 70% of the pre-registered parameter grid; no sharp peaks (neighbourhood median ≥ 50% of the best) |
| V9 | Multiple-testing control | **Deflated Sharpe Ratio** (Bailey & López de Prado) > 0.95 probability using the *total* trial count from the experiment registry, or White's Reality Check / Hansen SPA p < 0.05 against the family's trial set |
| V10 | Cost / slippage stress | Still > 0 at **2× assumed slippage** and +25% charges |
| V11 | Delayed entry | Still > 0 with +1 bar (or +5 s) entry delay |
| V12 | Degraded fills | 10% random missed entries, 5% stop fills at a worse price by 3 ticks → still > 0 |
| V13 | Monte Carlo trade sequence | 10,000 reshuffles/bootstraps: P(max DD > 15% NAV at the intended size) < 5%; P(ruin to 50%) < 0.1% |
| V14 | Regime dependence | P&L by regime label reported. The strategy must be flat-or-positive in its eligible regimes, and the spec's prohibited regimes must be justified by data |
| V15 | Event contamination | Results with and without event days; a strategy not event-certified must be positive without them |
| V16 | Holdout | Single evaluation: net expectancy > 0, and within the walk-forward CI (otherwise → overfit, reject) |
| V17 | Reproducibility | A re-run from the registry record reproduces trades bit-for-bit |
| V18 | Capital eligibility | `min_capital_inr` computed; reported against the current NAV |

### 13.2a Event-day certification (OD-014 default, 2-Oct-2026)
- A strategy may enter on an EVENT_REGIME day only if its spec has `event_certified: true`.
- `validation/event_cert.py` `certify_event_days` sets it:
  - It runs V1–V18 on the subset of the strategy's historical trades taken on event days.
  - V15 is moot on an event-only subset, so it is treated as certified for that run. Every other gate applies unchanged.
  - It certifies only on a VALIDATED verdict, so SYNTHETIC data never certifies.
- The result is saved as `configs/validation/event_certifications/<gate_result_id>.json`. The id is `VR-` plus 12 hex characters of the SHA-256 of the saved document.
- The spec records the id, the gate config version, the subset, REAL, VALIDATED, the number of event days and the date.
- `verify_event_certification` refuses a missing or edited file, or a result for another spec version. The Governor enforces the rule (docs/risk/risk-engine.md check 16).
- No strategy is certified.

## 13.3 Pre-registration and trial accounting
- Hypothesis, primary metric, parameter ranges and invalidation criteria are committed (hash-stamped) **before** the first backtest run.
- The experiment registry counts every run, including failed and abandoned ones. The Validation agent's DSR/SPA uses this count. Deleting runs is technically prevented (append-only).

## 13.4 Later gates
- **VALIDATED → PAPER:** automatic once V1–V18 pass and the owner has acknowledged.
- **PAPER → SHADOW:** see 14.
- **SHADOW → CANARY:** see 15.
- **CANARY → PRODUCTION:**
  - ≥ 60 live trades (or 3 months)
  - live net expectancy CI overlaps the validated CI and its lower bound is > −0.1R
  - realised slippage ≤ 1.5× the validated assumption
  - zero unresolved risk incidents attributable to the strategy
  - owner approval
- **Demotion (automatic):**
  - PRODUCTION → DEGRADED when rolling-40-trade expectancy CI upper bound < 0, **or** slippage > 2× assumption, **or** the feature-drift PSI > 0.25 on key features
  - DEGRADED → QUARANTINED if unchanged after 20 more trades or 2 weeks
  - QUARANTINED → RETIRED after review

## 13.5 Regime classifier validation (§14)
The deterministic regime classifier is itself a model and is validated:
- Labels are defined ex-ante from future-realised outcomes; for training evaluation only, never as features.
- OOS confusion matrix.
- Stability: label flips/day below a threshold.
- Economic value: strategies gated by the classifier must beat ungated versions OOS.

An unvalidated classifier ⇒ all regimes = NO_EDGE ⇒ reduce/do nothing.

## 13.6 Toolkit (S-02, built: thresholds ASSUMED)
`src/project100c/validation/` automates V1–V18. The thresholds live in `configs/validation/gates.toml` (version `VG-2026-10-02.1`, ASSUMED), and every report carries that version.
- `trades.py` holds the per-trade record, the rule eras (multi-weekly before 20-Nov-2024, single weekly Thursday until 1-Sep-2025, Tuesday from then on) and `trades_from_library_run()`. Library signal records carry `decided_at` and `inputs_end` stamps, so V1 can audit them.
- `stats.py` provides the day-block bootstrap CI, the Deflated Sharpe Ratio (expected maximum Sharpe from the registry's trial count), the drawdown and the Monte Carlo drawdown/ruin probabilities. All seeded and deterministic.
- `gates.py`: `validate(ValidationInputs, GateConfig)` returns a `ValidationReport` with one result per gate: PASS, FAIL or NOT_EVALUATED, with metrics and the criterion.
- **Verdict.** Any FAIL means REJECTED. Any NOT_EVALUATED means INCOMPLETE. All PASS on real data means VALIDATED. **SYNTHETIC data can never be VALIDATED.** At best it reaches PASSES_ON_SYNTHETIC, because V2 (point-in-time universe) and V16 (holdout) are not evaluated on synthetic data.
- **V16 interpretation.** The holdout is looked at once: a second look fails. Its mean must be above zero and not below the walk-forward CI. A holdout above the CI is not treated as overfitting.
- **V9.** Without SPA, the DSR is the only multiple-testing control. White's Reality Check and Hansen's SPA are not built.
- **Self-tests (backlog AT).** A known-edge synthetic strategy passes every gate synthetic data can exercise. A known-overfit one (many trials, no edge) is rejected. Each gate also has a targeted failing case. A library run (gap-go on a SYNTHETIC day) goes through the gates and is REJECTED: one trade, and the mechanics rig has no spread model. No strategy has a validation report on real data yet.

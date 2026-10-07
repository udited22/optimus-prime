# Regime classifier validation on the real lake (3-Oct-2026)

**REAL data** (Dhan NIFTY 1-minute index and India VIX, 2021-10 to 2026-03, DQ-passed parts only). This file holds statistics only, no prices. Produced by `scripts/regime_research.py` against the criteria **pre-registered** in [`configs/regime/validation.toml`](../../configs/regime/validation.toml) (`RV-2026-10-03.1`, committed before any result was read), with every day of the untouched holdout [`HD-2026-10-03.1`](../../configs/validation/holdout.toml) dropped before scoring. Every evaluation is registered as a trial in the experiment registry.

## Method (docs/research/validation.md §13.5)

- **Stream.** The classifier runs bar by bar over every NSE F&O session in the lake (09:15–15:30, days with ≥ 300 bars), carrying its cross-session range history. Labels use only past bars.
- **Periods.** In-sample (IS) 25-Jul-2022..30-Jun-2024 (385 sessions after the holdout weeks); out-of-sample (OOS) 1-Jul-2024..31-Mar-2026 (344 sessions). The recent holdout (1-Apr..1-Oct-2026) and 20% of earlier ISO weeks are excluded.
- **Samples.** One per session every 15 minutes with a complete 30-minute forward window inside the session and a non-warm-up label: 8,470 IS and 7,568 OOS.
- **Stability.** Median label run length (1-minute bars) and median label changes per session.
- **Predictive validity** against ex-ante defined, future-realised outcomes (evaluation only, never features). Day-block bootstrap, 90% CI, 2,000 resamples, seed 20261003:
  - `TREND_DIRECTION`: mean of s × forward 30-min return (bp), s = +1 UP, −1 DOWN.
  - `TREND_MAGNITUDE`: mean |forward return| on UP/DOWN minus on RANGE.
  - `VOL_LOW` / `VOL_HIGH`: forward 30-min realised vol (annualised %) NORMAL minus COMPRESSION / EXPANSION minus NORMAL.
  - Pass = OOS estimate > 0 **and** OOS CI lower bound > 0, IS estimate > 0, and positive in ≥ 3 calendar years.

## v0 `RC-2026-10-02.1` (UNVALIDATED, thresholds ASSUMED before any data): **FAIL**

| | IS | OOS | Criterion | |
|---|---|---|---|---|
| Trend run, median bars | 26 | 25 | ≥ 20 | pass |
| Trend changes / session, median | **9** | **9** | ≤ 8 | **FAIL** |
| Vol run, median bars | 34 | 35 | ≥ 20 | pass |
| Vol changes / session, median | 4 | 3 | ≤ 8 | pass |

| Test | IS estimate [90% CI] | OOS estimate [90% CI] | Years positive | |
|---|---|---|---|---|
| TREND_DIRECTION (bp / 30 min) | 0.71 [0.24, 1.16] | **0.29 [−0.18, 0.73]** | 6/6 | **FAIL** (OOS CI includes 0) |
| TREND_MAGNITUDE (bp) | 1.11 [0.67, 1.55] | 0.65 [0.12, 1.18] | 6/6 | pass |
| VOL_LOW (vol points) | 2.58 [2.33, 2.84] | 2.82 [2.53, 3.12] | 6/6 | pass |
| VOL_HIGH (vol points) | 4.70 [2.68, 7.16] | 5.23 [4.29, 6.05] | 6/6 | pass |

**Confusion matrices (reported, not judged).**
- *Trend* vs the realised 30-minute move (±1 trailing σ): Cohen's κ = **0.001 IS, −0.010 OOS**. UP and DOWN labels lift the chance of the realised direction by 1.05× IS and 1.08× / 0.95× OOS: no information.
- *Volatility* vs the realised-vol tercile (cut points fixed on IS): κ = **0.19 IS, 0.19 OOS**. EXPANSION is followed by top-tercile vol 77% of the time IS and **91% OOS** (lift 2.5×). COMPRESSION is followed by bottom-tercile vol 52% of the time (lift 1.6×).
- *Day to day*, the session-dominant trend does not persist (after an UP day: 74 UP, 75 DOWN, 94 RANGE), while the volatility state does (after a COMPRESSION day: 281 of 358 stay COMPRESSION).

**What this means.**
1. **The volatility dimension is valid** on every pre-registered test, in and out of sample and in every year. It is a useful regime variable.
2. **The trend dimension is not.** Its labels change about 9 times a session, and what it calls UP or DOWN does not predict the next 30 minutes out of sample. Even in sample the effect is 0.7 bp over 30 minutes: about 1.8 NIFTY points at 25,000, or roughly 0.9 premium point on an ATM option (delta about 0.5). Out of sample it is 0.29 bp, about 0.7 NIFTY point. A round trip costs roughly 2–3 premium points in charges and spread.
3. Vol-label shares are not stationary across years (COMPRESSION 66% of 2023 minutes, 30% of 2024), because the VIX bands are fixed levels. This is reported, not judged.
4. **Verdict under RV-2026-10-03.1: FAIL.** The classifier stays UNVALIDATED, and docs/research/validation.md §13.5 keeps every live regime NO_EDGE. The regime → strategy map may condition only on the dimension that passed (volatility); see `configs/portfolio/regime_map.toml`.

## Tuning in sample, then one out-of-sample look: candidate `RC-2026-10-03.1`: **FAIL**

**What was tried.** `scripts/regime_tune.py` scored a 12-point grid of trend settings, fixed in the script before it ran: confirm bars {5, 10, 15} × slope window {30, 60} × ADX enter/exit {25/18, 30/20}. Scoring used **in-sample days only** (25-Jul-2022..30-Jun-2024, holdout days excluded). The selection rule was also fixed in advance: among configs that meet the stability criteria, take the one with the highest TREND_DIRECTION 90% CI lower bound. All 12 configs are registered as IN_SAMPLE trials (strategy id `REGIME-CLASSIFIER`).

| confirm | slope | ADX | stable IS | trend flips / session | TREND_DIRECTION IS (bp) | IS CI lower | κ IS |
|---|---|---|---|---|---|---|---|
| 5 | 30 | 25/18 | no | 9 | 0.71 | 0.24 | 0.001 |
| 5 | 30 | 30/20 | no | 9 | 0.75 | 0.28 | 0.005 |
| 5 | 60 | 25/18 | yes | 7 | 0.57 | 0.10 | −0.007 |
| 5 | 60 | 30/20 | yes | 6 | 0.61 | 0.11 | −0.012 |
| 10 | 30 | 25/18 | yes | 6 | 0.53 | 0.02 | −0.002 |
| 10 | 30 | 30/20 | yes | 5 | 0.35 | −0.14 | −0.007 |
| 10 | 60 | 25/18 | yes | 5 | 0.39 | −0.12 | −0.006 |
| 10 | 60 | 30/20 | yes | 5 | 0.32 | −0.24 | −0.007 |
| **15** | **30** | **25/18** | **yes** | **3** | **0.67** | **0.14** | 0.002 |
| 15 | 30 | 30/20 | yes | 3 | 0.54 | 0.03 | 0.000 |
| 15 | 60 | 25/18 | yes | 4 | 0.33 | −0.22 | −0.002 |
| 15 | 60 | 30/20 | yes | 4 | 0.26 | −0.29 | −0.004 |

The winner, confirm 15 / slope 30 / ADX 25-18, differs from v0 only in `confirm_bars` (5 → 15). It became the candidate `RC-2026-10-03.1` in `configs/regime/candidates.toml`. That file is not read by the live loader, so the paper loop, the dashboard and the tests still use v0.

**One out-of-sample evaluation** (`scripts/regime_research.py --classifier-file configs/regime/candidates.toml`, registered as an OUT_OF_SAMPLE trial):

| | IS | OOS | Criterion | |
|---|---|---|---|---|
| Trend run, median bars | 58 | 58 | ≥ 20 | pass |
| Trend changes / session, median | 3 | 4 | ≤ 8 | pass |
| TREND_DIRECTION (bp / 30 min) | 0.67 | **0.26 [−0.17, …]** | OOS CI lower > 0 | **FAIL** (positive in 4 of 6 years) |
| TREND_MAGNITUDE (bp) | 0.84 | 0.85 [0.15, …] | | pass |
| VOL_LOW / VOL_HIGH (vol points) | 2.58 / 4.70 | 2.82 / 5.23 | | pass (unchanged: vol settings unchanged) |

**Verdict: FAIL.** Slower confirmation fixes the flip-flopping: label changes per session fall from 9 to 4. But the trend labels still carry no out-of-sample directional information. The small in-sample effect (0.67 bp) halves out of sample (0.26 bp) with a confidence interval that includes zero, which is what a fitted-to-noise parameter looks like. The candidate is **not adopted**, and v0 stays the classifier of record.

**Conclusion.** The whole classifier is still UNVALIDATED, and every live regime stays NO_EDGE (docs/research/validation.md §13.5). No strategy can be promoted to VALIDATED on these labels. The research map may condition on volatility only. A trend dimension would need a different construction, not another threshold search: for example a slower, day-level trend state, or a direction signal tested directly as a strategy rather than as a label. Each such attempt counts as a new trial.

# Regime → strategy map and portfolio on the real lake (3-Oct-2026)

**Verdict: no regime model passed. No (strategy, regime) pair and no strategy on its own passes the gates. The shortlist is empty and nothing is promoted to VALIDATED.** The walk-forward map selected no pair in any window, at either NAV, so the regime-driven portfolio and the regime-agnostic fallback both trade nothing. Almost every strategy loses money before costs, not only after them. Nothing here is fit for paper trading on evidence. Research only; spreads are ASSUMED.

Runner: `scripts/regime_portfolio.py`. Map: `RM-2026-10-03.2` (`configs/portfolio/regime_map.toml`), pre-registered before the H19–H23 results existed and before the library results were tabulated. Output: `lake/runs/regime/portfolio/RM-2026-10-03.2.json` and `-standalone.json` (git-ignored).

## 1. Data and discipline

- **Strategy runs.** Each of the 17 specs in `specs/` ran on the Dhan lake, 29-Aug-2022..1-Oct-2026, in 26 segments, at ₹10k and at a **HYPOTHETICAL ₹1L**. All runs are ungated, with dated verified costs, the 2% per-trade cap, 1 lot (2 for the H19 straddle under OD-013), ASSUMED synthetic spreads and the bar fill model. The H23 overlay (`S-ORBML-001`) is computed from the H01b trades (§5).
- **Regime tags.** Each trade's tags come from the classifier stream `RC-2026-10-02.1` at its signal bar. The map's cell is the validated vol dimension only (COMPRESSION / NORMAL / EXPANSION). Event-day, abnormal, warm-up and spec-prohibited trades are excluded: at ₹1L that is 378 S-VWAPMR-001 trades (prohibited regime), 24 S-IMOM-001 trades, and a few event-day and abnormal trades.
- **Holdout `HD-2026-10-03.1`** (PROPOSED, pending the owner): 1-Apr..1-Oct-2026 plus a hashed 20% of earlier ISO weeks. That is 301 sessions, never used here. Research data: 706 sessions. In-sample: 29-Aug-2022..31-Aug-2023. Walk-forward test windows: quarterly, 1-Sep-2023..31-Mar-2026.
- **Trials.** Every evaluation is registered in the experiment registry. That covers each library run, the H23 overlay (4 passes, including one buggy pass), 6 portfolio books, 16 standalone books, the 2 alternative regime models and the 57 gate evaluations. The V9 count at the gate step was 137; the registry total is now 153.
- **V15 is weak before Apr-2025.** The event calendar only starts in Apr-2025, so earlier event days are not tagged.
- **H01 ≡ H01b on this lake.** There are no NIFTY futures volumes here, so H01's futures-volume filter uses the same near-the-money option-volume proxy as H01b. Their trades are identical: one strategy, counted twice.
- **Engine fix during the run.** On 10-Jan-2023, S-NOISE-001 hit a net-short invariant. Another instrument's 14:50 bar arrived first, so the forced flatten sold while the strategy's exit was still working, and both filled. The engine now waits for the cancel (regression test added). That segment was re-run for H20/H21. Earlier runs predate the fix; the only effect would be a flatten one minute later in rare cases.

## 2. Classifier and alternative regime models

| Model | Out-of-sample result | Verdict |
|---|---|---|
| K-11 v0 `RC-2026-10-02.1` | Vol passes (EXPANSION followed by top-tercile RV 91% of the time; κ 0.19). Trend fails (9 changes a session, no directional information). | **FAIL** (unvalidated) |
| K-11 candidate `RC-2026-10-03.1` | Stable trend (4 changes a session), but direction +0.26 bp with a CI that includes 0 | **FAIL**, not adopted |
| H25 `F-TERM-001` (`TS-2026-10-03.1`) | Informative: the next session's RV after TERM_LOW (flat or inverted front) is 9.9% vs 8.3% after TERM_HIGH (difference CI excludes 0). Economic test: no pair qualified in any window, so there was nothing to gate. | **FAIL** |
| H26 `F-HMMREG-001` (`HMM-2026-10-03.1`) | First fit Mar-2024 (test 1-Mar-2024..31-Mar-2026). Informative: same-session RV is TURBULENT 10.9% vs CALM 7.8% (difference +3.0 points, 90% CI [2.3, 3.8]). Economic test: no pair qualified, so there was nothing to gate. | **FAIL** |

The H25 and H26 labels carry volatility information, as K-11's vol labels do. That cannot help when no strategy has positive expectancy in any state. The pre-registered pass test needs a gated book that beats the K-11-gated and ungated books, and no gated book trades.

## 3. Per-regime results (walk-forward test windows, ₹1L HYPOTHETICAL, holdout excluded)

Totals across all strategies: 1,226 trades, net −₹3,94,236, charges ₹78,773, **gross −₹3,15,464**. In-sample (Aug-2022..Aug-2023): 740 trades, gross −₹1,37,698. Not shown: S-VIXSTR-001 (no trades), S-EVTBO-001 (one trade, on an event day) and S-ORBML-001 (every signal vetoed). At ₹10k the 2% cap (₹200 at the stop) refused almost everything: 2 trades in four years, both S-EXP0-001, both losers (−₹155 and −₹183).

| Strategy | Cell | Trades | Net ₹ | Hit rate | Expectancy ₹ | Expectancy R |
|---|---|---:|---:|---:|---:|---:|
| S-DAYVOL-001 | **all** | 1 | -273 | 0% | -273 | -0.17 |
|  | NORMAL | 1 | -273 | 0% | -273 | -0.17 |
| S-EXP0-001 | **all** | 12 | -2,288 | 17% | -191 | -0.43 |
|  | COMPRESSION | 3 | -5,744 | 0% | -1,915 | -1.75 |
|  | EXPANSION | 2 | 3,925 | 50% | 1,962 | +1.42 |
|  | NORMAL | 7 | -468 | 14% | -67 | -0.40 |
| S-FBO-001 | **all** | 112 | -40,080 | 30% | -358 | -0.29 |
|  | COMPRESSION | 37 | -11,000 | 32% | -297 | -0.23 |
|  | EXPANSION | 14 | -3,504 | 29% | -250 | -0.16 |
|  | NORMAL | 61 | -25,576 | 30% | -419 | -0.35 |
| S-GAPFADE-001 | **all** | 10 | 721 | 20% | 72 | -0.06 |
|  | COMPRESSION | 3 | -1,337 | 0% | -446 | -0.41 |
|  | EXPANSION | 4 | -2,581 | 25% | -645 | -0.46 |
|  | NORMAL | 3 | 4,639 | 33% | 1,546 | +0.82 |
| S-GAPGO-001 | **all** | 4 | 2,810 | 50% | 703 | +0.26 |
|  | EXPANSION | 1 | 3,244 | 100% | 3,244 | +1.63 |
|  | NORMAL | 3 | -434 | 33% | -145 | -0.19 |
| S-GEXMO-001 | **all** | 49 | -13,062 | 29% | -267 | -0.18 |
|  | COMPRESSION | 21 | -5,378 | 29% | -256 | -0.09 |
|  | EXPANSION | 3 | -2,707 | 0% | -902 | -0.79 |
|  | NORMAL | 25 | -4,977 | 32% | -199 | -0.18 |
| S-IMOM-001 | **all** | 53 | -11,903 | 32% | -225 | -0.18 |
|  | COMPRESSION | 19 | -4,986 | 32% | -262 | -0.14 |
|  | EXPANSION | 2 | 404 | 50% | 202 | +0.22 |
|  | NORMAL | 32 | -7,321 | 31% | -229 | -0.23 |
| S-IVRV-001 | **all** | 22 | -6,417 | 23% | -292 | -0.24 |
|  | COMPRESSION | 2 | -2,078 | 0% | -1,039 | -0.63 |
|  | EXPANSION | 14 | -2,734 | 29% | -195 | -0.20 |
|  | NORMAL | 6 | -1,605 | 17% | -267 | -0.20 |
| S-LUNCH-001 | **all** | 61 | -18,045 | 16% | -296 | -0.26 |
|  | COMPRESSION | 50 | -14,624 | 14% | -292 | -0.28 |
|  | NORMAL | 11 | -3,422 | 27% | -311 | -0.19 |
| S-NOISE-001 | **all** | 74 | -31,801 | 23% | -430 | -0.30 |
|  | COMPRESSION | 21 | 1,039 | 33% | 49 | +0.02 |
|  | EXPANSION | 11 | -9,733 | 9% | -885 | -0.62 |
|  | NORMAL | 42 | -23,108 | 21% | -550 | -0.38 |
| S-ORB-001 | **all** | 107 | -33,550 | 32% | -314 | -0.23 |
|  | COMPRESSION | 35 | -13,610 | 26% | -389 | -0.29 |
|  | EXPANSION | 6 | -2,160 | 17% | -360 | -0.51 |
|  | NORMAL | 66 | -17,780 | 36% | -269 | -0.17 |
| S-ORB-002 | **all** | 107 | -33,550 | 32% | -314 | -0.23 |
|  | COMPRESSION | 35 | -13,610 | 26% | -389 | -0.29 |
|  | EXPANSION | 6 | -2,160 | 17% | -360 | -0.51 |
|  | NORMAL | 66 | -17,780 | 36% | -269 | -0.17 |
| S-VOLX-001 | **all** | 152 | -45,625 | 20% | -300 | -0.25 |
|  | COMPRESSION | 98 | -26,578 | 22% | -271 | -0.22 |
|  | NORMAL | 54 | -19,047 | 17% | -353 | -0.30 |
| S-VWAPC-001 | **all** | 241 | -81,990 | 19% | -340 | -0.25 |
|  | COMPRESSION | 81 | -29,418 | 20% | -363 | -0.27 |
|  | EXPANSION | 20 | -4,916 | 35% | -246 | -0.15 |
|  | NORMAL | 140 | -47,656 | 16% | -340 | -0.26 |
| S-VWAPMR-001 | **all** | 221 | -79,183 | 24% | -358 | -0.28 |
|  | COMPRESSION | 76 | -31,706 | 25% | -417 | -0.30 |
|  | EXPANSION | 21 | -4,702 | 29% | -224 | -0.20 |
|  | NORMAL | 124 | -42,775 | 23% | -345 | -0.27 |

The few positive cells have 1–21 trades and fail V5/V6 (S-NOISE-001 COMPRESSION: 21 trades, +0.02 R; S-GAPFADE-001 NORMAL: 3 trades). They are not edges.

## 4. Portfolio backtests (walk-forward 1-Sep-2023..31-Mar-2026, OD-014 caps)

The rules are the same in every book: 10 entries a day system-wide plus each spec's own cap, one position at a time, 2% of current NAV at the stop, a 4% daily stop, an 8% weekly freeze, the latched 12.5% drawdown suspension, cooldown and exclusive groups.

| Book | NAV | Trades | Net ₹ | Net CAGR | Sharpe | Sortino | Max DD | Calmar | Hit rate | Exp. ₹ | Exp. R | Note |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| REGIME (map, K-11 vol cells) | ₹1L | 0 | 0 | 0% | – | – | 0% | – | – | – | – | no pair selected in any window |
| AGNOSTIC (same rule per strategy) | ₹1L | 0 | 0 | 0% | – | – | 0% | – | – | – | – | no strategy selected |
| UNGATED (every eligible trade) | ₹1L | 44 | −12,918 | −6.8% | −1.70 | −1.76 | 12.9% | −0.52 | 25% | −294 | −0.29 | suspended by the 12.5% latch on 12-Oct-2023 |
| REGIME / AGNOSTIC | ₹10k | 0 | 0 | 0% | – | – | 0% | – | – | – | – | |
| UNGATED | ₹10k | 1 | −183 | −0.9% | −0.71 | −0.71 | 1.8% | −0.51 | 0% | −183 | −0.94 | |

**Each strategy alone** (diagnostic, the same book with every eligible trade, ₹1L): 10 of 15 hit the 12.5% suspension. Only S-GAPFADE-001 ends positive (+₹721 on 10 trades, expectancy −0.06 R).

| Strategy (₹1L) | Trades | Net ₹ | Net CAGR | Sharpe | Sortino | Max DD | Calmar | Hit rate | Exp. ₹ | Exp. R | 12.5% latch hit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| S-DAYVOL-001 | 1 | -273 | -0.1% | -0.71 | -0.71 | 0.3% | -0.51 | 0% | -273 | -0.17 | no |
| S-EXP0-001 | 12 | -2,288 | -1.2% | -0.20 | -0.28 | 6.6% | -0.17 | 17% | -191 | -0.43 | no |
| S-FBO-001 | 31 | -13,237 | -6.9% | -1.93 | -2.00 | 13.2% | -0.52 | 35% | -427 | -0.30 | 2024-02-14 |
| S-GAPFADE-001 | 10 | 721 | 0.4% | 0.10 | 0.27 | 5.2% | 0.07 | 20% | 72 | -0.06 | no |
| S-GAPGO-001 | 3 | -434 | -0.2% | -0.15 | -0.21 | 1.8% | -0.12 | 33% | -145 | -0.19 | no |
| S-GEXMO-001 | 26 | -11,360 | -5.9% | -1.74 | -1.96 | 13.1% | -0.45 | 23% | -437 | -0.27 | 2024-08-29 |
| S-IMOM-001 | 46 | -9,910 | -5.1% | -1.44 | -1.76 | 12.6% | -0.41 | 33% | -215 | -0.19 | 2026-01-09 |
| S-IVRV-001 | 19 | -7,849 | -4.0% | -1.86 | -2.04 | 8.8% | -0.46 | 16% | -413 | -0.31 | no |
| S-LUNCH-001 | 53 | -12,662 | -6.6% | -1.71 | -2.14 | 12.7% | -0.52 | 15% | -239 | -0.24 | 2025-10-09 |
| S-NOISE-001 | 28 | -11,858 | -6.2% | -1.91 | -2.11 | 13.3% | -0.47 | 29% | -424 | -0.33 | 2024-05-16 |
| S-ORB-001 | 66 | -13,081 | -6.8% | -1.28 | -1.76 | 13.1% | -0.52 | 32% | -198 | -0.19 | 2024-10-01 |
| S-ORB-002 | 66 | -13,081 | -6.8% | -1.28 | -1.76 | 13.1% | -0.52 | 32% | -198 | -0.19 | 2024-10-01 |
| S-VOLX-001 | 74 | -11,851 | -6.2% | -1.27 | -2.11 | 12.7% | -0.49 | 24% | -160 | -0.16 | 2024-07-04 |
| S-VWAPC-001 | 28 | -12,511 | -6.5% | -2.67 | -2.64 | 12.5% | -0.52 | 11% | -447 | -0.35 | 2023-12-05 |
| S-VWAPMR-001 | 25 | -12,507 | -6.5% | -2.63 | -2.61 | 12.5% | -0.52 | 12% | -500 | -0.45 | 2023-12-18 |

## 5. H23 meta-label overlay (`S-ORBML-001`)

The H01b trades (257 at ₹1L, none at ₹10k) were run through the pre-registered model: ten features, L2 logistic with C = 1, monthly refits, at least 150 training signals, p ≥ 0.55. The label base rate on research data is **6.3%**: 1.5 R reached within 60 minutes. Purged 5-fold CV (diagnostic only) gives **AUC 0.31**, worse than chance. The model had its 150 non-holdout training signals only late: 201 signals had no model, 18 had a missing feature, and the 38 scored signals were all vetoed. **Zero trades. Falsified as specified.** The spec stays in `specs/drafts/`: it is a research overlay with no live plug-in.

## 6. Gates V1–V18 (57 pairs: every (strategy, cell, NAV) and (strategy, ALL, NAV) with trades)

| Gate | PASS | FAIL | Not evaluated |
|---|---:|---:|---:|
| V1 look-ahead, V2 universe, V3 costs, V4 fills | 57 | 0 | 0 |
| V18 capital (PASS = computed and reported; eligible at ₹1L: only S-EXP0-001) | 57 | 0 | 0 |
| V5 expectancy CI > 0 | 1* | 56 | 0 |
| V6 sample size (100 OOS, 40 recent) | 1 (S-VWAPMR-001 all, 221 trades) | 56 | 0 |
| V7 concentration | 0 | 57 | 0 |
| V9 deflated Sharpe (137 trials) | 0 | 57 | 0 |
| V10 cost stress | 6 | 51 | 0 |
| V12 degraded execution | 7 | 50 | 0 |
| V13 Monte Carlo drawdown | 33 | 24 | 0 |
| V14 regime consistency | 8 | 49 | 0 |
| V15 event robustness (weak before Apr-2025) | 7 | 50 | 0 |
| V16 holdout | 0 | 8 (H01/H01b prior look) | 49 |
| V8 grid, V11 delayed entry, V17 re-run | – | – | 57 (run only for survivors; there are none) |

\* The one V5 pass is S-GAPGO-001 EXPANSION with a single trade: a one-trade bootstrap CI collapses to a point. V6 rejects it. The toolkit should require a minimum count for V5.

All 57 pairs are **REJECTED**. Every pair fails at least V5 or V6, V7 and V9.

## 7. Holdout (read once, 3-Oct-2026 15:30 IST; reported separately from walk-forward)

The owner's brief asked for every book under every classifier with the holdout reported separately, so the single look was taken (`--holdout-look`, map `RM-2026-10-03.2`, holdout `HD-2026-10-03.1` still **PROPOSED**). Nothing was tuned on it, and nothing can be: the marker `lake/runs/regime/portfolio/HOLDOUT-LOOK-RM-2026-10-03.2.json` makes the script refuse a second read. The final maps (refitted on all research trades) were frozen before the read: K-11 map, agnostic fallback, H25 `F-TERM-001` and H26 `F-HMMREG-001` maps all select **nothing**.

₹1L HYPOTHETICAL, OD-014 caps, 2% risk cap. *All holdout* = 301 sessions, run in date order (hashed earlier weeks first); *recent block* = 1-Apr..1-Oct-2026 (126 sessions) on its own fresh NAV.

| Book | Scope | Trades | Net ₹ | Net CAGR | Sharpe | Sortino | Max DD | Calmar | Hit | Expectancy | Suspended |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| REGIME (K-11 v0) | both | 0 | 0 | 0% | – | – | 0% | – | – | – | – |
| AGNOSTIC fallback | both | 0 | 0 | 0% | – | – | 0% | – | – | – | – |
| ALT H25 / H26 | both | 0 | 0 | 0% | – | – | 0% | – | – | – | – |
| UNGATED benchmark | all holdout | 31 | −12,689 | −10.7% | −2.61 | −2.58 | 12.7% | −0.85 | 19.4% | −₹409 / −0.31 R | 23-Feb-2023 |
| UNGATED benchmark | recent block | 57 | −13,245 | −24.7% | −2.48 | −3.23 | 13.3% | −1.87 | 29.8% | −₹232 / −0.15 R | 11-Sep-2026 |

At ₹10k no spec has a holdout trade that fits the 2% cap, so every ₹10k holdout book is zero trades.

The holdout agrees with walk-forward: the regime-driven books are flat because nothing was selected, and the ungated book loses and hits the 12.5% suspension in both slices. Registered as five HOLDOUT trials (one per book, ₹1L; the recent block is a slice of the same look, kept in its result file). Because the ungated book ran every spec's holdout trades, every spec now carries one more holdout look (`configs/validation/holdout.toml` `prior_looks`); a future holdout evaluation needs a new spec version.

## 8. What could go to paper, and what would change the picture

- **Fallback:** a regime-agnostic portfolio of strategies that pass on their own, gated by the event and abnormal-market rules. No strategy passes on its own: the (strategy, ALL) rows are all REJECTED, and the walk-forward rule selects none. **Nothing qualifies for paper on evidence.** Paper trading would only test plumbing (OD-017), not an edge.
- **Gross P&L before costs is negative** for 13 of 15 traded strategies. The exceptions, S-GAPFADE-001 (+₹1,284 on 10 trades) and S-GAPGO-001 (+₹3,044 on 4 trades), have far too few trades to mean anything. Cheaper execution alone cannot fix this. The long-option premise of these intraday families (buy ATM options on a 1-minute trigger, hold under 75 minutes) loses theta and spread faster than the moves pay.
- ₹10k cannot trade these specs at all under the 2% cap. V18's 1-lot worst-case minimum capital is ₹1.5–2.6 lakh for most of them; only S-EXP0-001 (₹96,909) is eligible at ₹1L.
- Future work that the data supports: the vol information is real (K-11 vol, H25, H26). It may suit **premium-aware sizing or longer holds**, not short intraday long-premium entries. Any new idea needs a new pre-registered spec version and a fresh trial count; the holdout is still unread.

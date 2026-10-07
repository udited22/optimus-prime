# 12 — Initial Strategy Hypotheses (StrategySpec drafts)

> **Mandate OD-006 (30-Sep-2026): LONG OPTIONS ONLY.** Every hypothesis below opens a position only by **buying** a NIFTY call or put. Exits are sell-to-close of that long position. Anything that needs a short option leg is in **Appendix P: prohibited under current mandate**, which covers credit/debit spreads, straddle/strangle selling, iron condors and flies, and calendars. The earlier H13 and H15, and the short-vol half of H14, were moved there. The IV/vol ideas are restated as long-premium-only variants where that makes sense.

> **No backtest, paper or live results exist for any of these.** All `evidence` fields are `pending`. "Prior" is a qualitative, pre-registration judgement of plausibility, not a result. Each hypothesis is **pre-registered** here: its primary metric, invalidation condition and parameter ranges are fixed before any data is examined, to limit data snooping (13-validation §13.3).
>
> **Capital eligibility** (01 §A3) is computed from the typical premium band each spec trades. Most specs are **ineligible at ₹10k NAV**. The Governor enforces this automatically through `dependencies.min_capital_inr`.

Common defaults for all specs unless stated otherwise:
- Instrument: NIFTY weekly options.
- Entries 09:20–14:00 IST (14:00 cutoff = OD-008, superseding OD-003's 14:45). Forced flatten from 14:50; hard flat by 15:00 (OD-002), broker Exit-All for any residual (OD-007).
- **Long options only (OD-006):** BUY CE or BUY PE to open. The only SELL is a sell-to-close of the open long, qty ≤ open qty.
- 1 lot. Entry LIMIT, protective SL-LIMIT.
- Cost model `CM-2026-04-01`.
- Prohibited regimes always include `ABNORMAL_MARKET`, `LOW_LIQUIDITY`, `NO_EDGE`.
- Primary metric: **net expectancy per trade in R (R = risk at stop incl. costs), OOS, with a 90% bootstrap CI lower bound > 0**.

---

### H01 — `S-ORB-001` Opening-range breakout with futures confirmation
- **Family:** ORB / intraday trend continuation
- **Hypothesis:** When NIFTY futures close a 5-min bar beyond the 09:15–09:30 opening range, with volume ≥ 1.5× its 20-day same-time median and India VIX not falling, the index keeps moving in the breakout direction ≥ 0.4 × OR-width within 60 min more often than a volatility-matched random entry does.
- **Economic rationale:** Opening-range breaks aggregate overnight information and institutional order flow that executes over the morning. Retail option sellers who are short gamma near the range edges hedge late, which may add follow-through.
- **Required data:** futures 1-min (+ tick for live), options 1-s quotes, VIX 1-min.
- **Signal:** `OR = [min, max](fut, 09:15–09:30)`; `bullish if close_5m > OR_high and vol_ratio ≥ θv and ΔVIX_30m ≥ 0` → BUY CE; mirror (bearish) → **BUY PE** (never sell an option).
- **Eligible regimes:** TRENDING_UP/DOWN, VOLATILITY_EXPANSION, GAP_REGIME (if gap < 0.8%). **Prohibited:** EVENT_REGIME (pre-announcement), EXPIRY_REGIME after 13:00.
- **Instrument / strike / expiry:** Bullish = buy CE, bearish = buy PE. Delta 0.40–0.55. Nearest weekly with DTE ≥ 1.
- **Entry / exit:** LIMIT at mid+1 tick, TTL 10 s. **Stop:** futures back inside the OR by 25% of OR-width (translated to a premium SL via delta), or premium −30%, whichever is closer. **Target:** 1.5 R or trailing at the 15-min swing. **Max hold:** 75 min.
- **Params (ranges):** OR window {15, 30} min; θv ∈ [1.2, 2.0]; stop OR-fraction ∈ [0.15, 0.40].
- **Expected frequency (prior):** 1–3 signals/week. **Slippage assumption:** 1 tick entry, 2 ticks stop.
- **Capacity:** high (NIFTY ATM liquidity) relative to our size.
- **Why it might survive retail costs:** holding periods of tens of minutes and targets of tens of premium points make the ~₹50–70 round-trip cost small relative to the move.
- **Failure modes:** false breakouts in range regimes; IV crush after the open; news-driven reversals.
- **Capital eligibility:** premium ~₹70–130 → min NAV ≈ ₹70k–1.1 lakh. **Ineligible at ₹10k.**
- **Retirement:** OOS expectancy CI upper bound < 0; or live slippage > 2× assumed over 20 trades.
- **Prior:** medium-low. ORB is widely known, so the edge (if any) is likely conditional on regime.

### H02 — `S-FBO-001` Failed breakout reversal
- **Family:** failed breakout / reversal
- **Hypothesis:** A breakout beyond the opening range or the previous-day high/low that fails, with price back inside within 15 min on declining volume, is followed by a move to the opposite side of the range more often than chance.
- **Rationale:** Stops clustered beyond obvious levels get triggered, liquidity providers absorb them, and trapped breakout traders then exit.
- **Signal:** `break → re-entry inside by ≥ 0.2×OR within 15 min, vol_ratio(re-entry) < vol_ratio(break)`.
- **Eligible:** MEAN_REVERTING, VOLATILITY_COMPRESSION. **Prohibited:** TRENDING with VIX rising, EVENT.
- **Strike:** delta 0.35–0.50 in the reversal direction.
- **Stop:** beyond the failed-break extreme (+ 2 ticks buffer), mapped to premium. **Target:** range midpoint, then the opposite edge. **Max hold:** 60 min.
- **Params:** re-entry depth ∈ [0.1, 0.3]; window ∈ [10, 25] min.
- **Why it might survive costs:** well-defined, tight structural stop relative to the target.
- **Failure modes:** it is the mirror image of H01, so both cannot be simultaneously strong in the same regime. The Allocator must treat them as anti-correlated.
- **Capital:** ₹70k–1.1 lakh. Ineligible at ₹10k. **Prior:** medium-low.

### H03 — `S-VWAPC-001` VWAP pullback continuation (trend days)
- **Family:** VWAP continuation
- **Hypothesis:** On days classified TRENDING (futures > session VWAP for ≥ 70% of minutes since 09:30 and ADX-like slope > θ), a pullback that touches VWAP ± 0.1σ and resumes in the trend direction yields positive short-horizon continuation.
- **Rationale:** Institutional execution benchmarks to VWAP. Pullbacks to VWAP on trend days attract benchmark-driven buying (or selling).
- **Data:** futures tick/1-min volume for VWAP (NIFTY spot has no volume, so the futures VWAP is the proxy).
- **Stop:** futures close beyond VWAP by 0.3σ_intraday. **Target:** prior swing / 1.5 R. **Max hold:** 60 min.
- **Params:** trend-share ∈ [0.6, 0.8]; touch band ∈ [0.05, 0.2]σ.
- **Failure modes:** late-day reversals; trend regime misclassification.
- **Capital:** ₹55k–1.1 lakh. Ineligible at ₹10k. **Prior:** medium.

### H04 — `S-VWAPMR-001` VWAP mean reversion (range days)
- **Family:** VWAP mean reversion
- **Hypothesis:** On days classified MEAN_REVERTING / VOLATILITY_COMPRESSION (low realised vol, VIX in the bottom tercile of its 1-year range), deviations of futures from VWAP beyond 1.5σ revert ≥ 50% within 45 min.
- **Rationale:** In low-vol regimes, liquidity providers and short-gamma option sellers hedge in a mean-reverting way (they buy dips and sell rallies to stay delta-neutral).
- **Instrument:** buy the option pointing toward VWAP, delta 0.30–0.45.
- **Stop:** deviation extends to 2.5σ or 20 min time-stop. **Target:** VWAP touch.
- **Failure modes:** regime flip into trend. Buying options in low-vol regimes pays theta, so the move must come quickly.
- **Capital:** ₹30k–55k (lower premiums in low VIX). **Prior:** low-medium (theta drag is the main enemy).

### H05 — `S-VOLX-001` Intraday volatility-compression breakout
- **Family:** vol compression → expansion
- **Hypothesis:** After ≥ 45 min of intraday realised-range compression (rolling 15-min range in the bottom decile of the day's distribution, conditioned on time-of-day), the subsequent 30-min absolute move exceeds what ATM premium implies. Buying an option in the breakout direction then has positive expectancy.
- **Rationale:** Volatility clustering. Option premiums intraday price average vol and may underprice post-compression expansion.
- **Signal:** compression flag + break of the compression box by ≥ 1 tick-range with volume.
- **Stop:** back inside the box by 50%. **Max hold:** 45 min.
- **Params:** compression length ∈ [30, 90] min; decile ∈ [5, 15]%.
- **Failure modes:** lunchtime drift (12:00–13:30) produces many false signals; needs a time-of-day filter.
- **Capital:** ₹55k–1 lakh. **Prior:** medium.

### H06 — `S-IVRV-001` IV vs forecast RV: cheap-gamma timing (long premium only)
- **Family:** IV vs RV (long-premium variant, OD-006)
- **Hypothesis:** When ATM weekly IV (own Black-76 calculation) is below a HAR-RV forecast of intraday realised vol by more than θ vol points, **buying a single ATM option** in the direction of a pre-registered short-term trend filter (e.g. H03's VWAP side) has higher net expectancy than the same directional entry taken when IV ≥ forecast RV. Options are "cheap", so a correct direction pays more per point and theta hurts less relative to realised movement.
- **Rationale:** The variance risk premium is usually positive (implied > realised), which favours sellers. Sellers are prohibited here (OD-006), so the only way to use it is to **buy premium only when it is conditionally cheap**, and otherwise stay out.
- **Structure:** single leg, 1 lot, intraday (flat by 15:00). The long-straddle version (buy CE + buy PE) is **long-only compliant** but needs 2 concurrent lots, so it is blocked by the 1-position canary cap. It is research-only until the owner raises that cap.
- **Failure modes:** the conditioning may add little over the direction filter; forecast error is large; signals are rare.
- **Capital:** single leg ≈ ₹55k–1.1 lakh (ATM premium band). **Prior:** low-medium.

### H07 — `S-EXP0-001` Expiry-day (Tuesday) afternoon momentum in low-priced OTM options
- **Family:** gamma-sensitive expiry behaviour / time-of-day
- **Hypothesis:** On NIFTY weekly expiry days, after 13:00, when futures make a new session extreme with 15-min momentum above the 80th percentile (time-matched), the move extends further before the flat time (15:00, OD-002) more often than implied by option prices. Buying a near-OTM option (premium ₹5–20) in that direction then has positive expectancy.
- **Rationale:** Very large short-gamma open interest held by option writers on expiry day means their delta-hedging adds to moves (dealer-gamma feedback). The SEBI study shows 59% of index-option turnover is 0DTE in FY26 (S22), so the flow is large.
- **Stop:** premium −25% or the futures swing low/high, whichever is closer. **Target:** 2× premium or time exit.
- **Why it might survive costs:** convex payoff on small premium. At a ₹0-brokerage API the charges are only ~₹2–5 per round trip (01 §A2).
- **Failure modes:**
  - Theta collapse: 0DTE OTM options lose value quickly, so a stalled move is a near-certain loss.
  - Pinning near max-OI strikes, which is the opposite effect.
  - CAS-related swings after 15:15 in the underlying (avoided by the 15:00 hard flat).
  - Spreads widen as a share of premium on cheap options.
- **Capital eligibility:** the **only spec potentially eligible at ₹10k**. Example: premium ₹8, stop 2 pts → ₹130 + charges (₹0.98 at ₹0 brokerage; ₹48.18 at ₹20/order) + slippage (0.25 pt/side ≈ ₹32.5) = **₹163.48 at ₹0 brokerage (eligible)** / **₹211 at ₹20 brokerage (ineligible)**. **Upstox (OD-004) charges ₹20/order, so H07 is ineligible at ₹10k** unless its parameters change (e.g. a narrower pre-registered stop; that is a new spec version, never a "fit").
- **Prior:** low. This is exactly where most retail losses concentrate (S22), so the burden of proof is highest.

### H08 — `S-GAP-001` Opening gap conditional fade/continuation
- **Family:** overnight-to-open
- **Hypothesis:** For NIFTY opening gaps of 0.4–1.2% that GIFT Nifty and global cues do *not* fully justify (residual gap = actual − predicted from GIFT/US futures/USDINR), the residual fades by ≥ 40% in the first 60 min.
- **Rationale:** The opening auction overreacts to thin overnight information. Residual (unexplained) gaps revert, while explained gaps persist.
- **Data:** GIFT Nifty and US index futures pre-market, USDINR, NIFTY pre-open equilibrium price.
- **Entry:** 09:20–09:35 only. **Stop:** gap extreme + buffer. **Max hold:** 60 min.
- **Failure modes:** needs reliable pre-market data (GIFT Nifty availability via broker, S27); news-driven gaps.
- **Capital:** ₹55k–1.1 lakh. **Prior:** medium.

### H09 — `S-OIM-001` Option OI build-up momentum
- **Family:** OI momentum
- **Hypothesis:** Rapid intraday build-up of put OI at or below spot (put writing), together with rising futures and falling call OI above spot (call unwinding), predicts positive 30–60 min index returns (mirror for down).
- **Rationale:** Aggressive option writing reflects informed or well-capitalised participants' directional views (proprietary firms earn most options profits, S22).
- **Caveat:** OI updates are coarse (exchange OI is disseminated with lag; verify update frequency per broker). The signal may be stale by the time retail sees it.
- **Failure modes:** OI changes reflect hedging or spreads, not directional views; lagged dissemination.
- **Capital:** ₹55k–1.1 lakh. **Prior:** low. It is popular retail folklore, and the Validation agent must be especially hostile here.

### H10 — `S-BASIS-001` Synthetic-forward / futures basis dislocation
- **Family:** futures/options basis
- **Hypothesis:** Short-lived deviations of the options-implied synthetic forward (C − P + K at ATM) from the futures price, beyond cost-of-carry ± spread, lead futures in the direction of the synthetic within 1–5 min.
- **Rationale:** Informed flow sometimes hits options first (leverage). Arbitrageurs close the gap, and futures move.
- **Caveat:** this is close to latency arbitrage, which the directive excludes (§4). It is worth keeping only as a **feature** for other strategies, not a standalone strategy, unless the lead lasts minutes, not milliseconds.
- **Capital:** n/a (feature). **Prior:** low as a standalone strategy.

### H11 — `S-XSEC-001` Heavyweight constituent breadth lead
- **Family:** constituent cross-sectional → index
- **Hypothesis:** A weighted breadth/imbalance signal from the top-10 NIFTY constituents (share of weight above VWAP, 5-min returns of banks vs IT) leads NIFTY futures over 5–15 min horizons.
- **Rationale:** Index futures price discovery can lag when flows concentrate in a sector (e.g. bank-led moves). Constituents trade in the cash market with the Closing Auction Session and distinct participants.
- **Data:** 1-min (ideally tick) constituents; index weights (point-in-time).
- **Failure modes:** futures are usually the price leader, not the follower; the signal decays at retail latency.
- **Capital:** ₹55k–1.1 lakh. **Prior:** low-medium.

### H12 — `S-TOD-001` Intraday momentum: first-hour return predicts late-session return
- **Family:** time-of-day anomaly
- **Hypothesis:** The sign of the 09:15–10:15 return (and the previous-day last-30-min return) predicts the return from a **13:15–14:00 entry** to an exit no later than the **14:50** forced-flatten start in NIFTY. **Re-windowed 1-Oct-2026 for OD-008:** the entry window was 14:00–14:45, which OD-008 no longer allows (entries must end by 14:00). Windows fixed by OD-002/OD-008/OD-009. The idea is a variant of the "market intraday momentum" literature (Gao, Han, Li & Zhou, *J. Financial Economics* 2018, on US ETFs; citation from memory, not re-verified today).
- **Rationale:** Late-session rebalancing and hedging flows follow the morning's information.
- **Constraint:** the classic effect is in the *last* half-hour. With NSE's close at 15:40 (S18), the last 30 min is 15:10–15:40, which is **outside our 09:15–15:00 trading window (OD-002)**. Only the in-window variant can be traded. The true last half-hour may be studied offline, but trading it would need a new owner decision.
- **Capital:** ₹55k–1.1 lakh. **Prior:** medium (documented internationally).

### H13 — `S-SKEWL-001` Put-skew spike without spot confirmation → buy the call (long premium only)
- **Family:** skew dislocation (long-premium variant, OD-006)
- **Hypothesis:** When 25Δ put-minus-call IV skew jumps > 2σ intraday while spot is flat (|Δspot| < 0.2%), the protection demand is not followed by a spot decline more often than implied. **Buying a near-ATM call** (relatively cheaper side of the smile) for ≤ 90 min has positive net expectancy.
- **Rationale:** Hedging-demand shocks push put premiums up without new information. When the hedge flow fades, spot tends to drift up (relief). The call is the cheap side of the smile at that moment.
- **Structure:** single leg, 1 lot, intraday.
- **Failure modes:** the skew spike *is* informed (a crash precursor), which gives a direct loss on the call; an IV-level drop hurts the long call too.
- **Capital:** ≈ ₹55k–1.1 lakh. **Prior:** low-medium (the spread version was the more natural trade; see Appendix P-1).

### H14 — `S-EVT-001` Scheduled-event implied vs historical move
- **Family:** event-driven
- **Hypothesis:** For scheduled events (RBI MPC, US CPI/FOMC, Union Budget), if the ATM straddle-implied move for the event window is below the historical median absolute move for that event type, a **long straddle (buy CE + buy PE, long-only compliant)** entered ≤ 30 min before and exited ≤ 60 min after yields positive expectancy. The converse short-vol trade is prohibited (Appendix P-2).
- **Rationale:** Event premia can be mis-set when recent events were quiet.
- **Constraint:** requires `event_certified: true` and two concurrent long lots, which the 1-position cap blocks → Phase 5 and an owner decision on the position cap. A single-leg variant (buy the option in the direction of the first 5-min post-event move) may be registered separately. It has no result.
- **Data:** ≥ 5 years of event history (small N!). The Validation agent must widen the CIs for small samples.
- **Capital:** ≥ ₹2–3 lakh. **Prior:** low (N is tiny: ~6 RBI meetings/yr).

### H15 — `F-IVRANK-001` High-IV "don't buy expensive premium" filter (feature, long-only)
- **Family:** vol regime filter (replaces the short-vol iron condor, now Appendix P-3)
- **Hypothesis:** For the long-premium specs H01–H05, H08 and H12, entries taken when ATM IV rank (1-year) is in the top quintile and RV is falling have **lower net expectancy** than entries in other IV states, because of post-entry IV crush.
- **Use:** a **gating feature**, not a standalone strategy. If validated, specs may add `prohibited_iv_state: TOP_QUINTILE_FALLING_RV`.
- **Failure modes:** it may just proxy for event days, which are already gated.
- **Capital:** n/a (feature). **Prior:** medium.

### H16 — `S-ENS-001` Regime-gated ensemble of H01/H03/H05
- **Family:** ensemble / regime-conditioned directional buying
- **Hypothesis:** Taking H01/H03/H05 signals *only* when the validated regime classifier outputs TRENDING ∧ VOLATILITY_EXPANSION, and requiring ≥ 2 concordant signals, gives higher OOS expectancy per trade than any single component (at lower frequency).
- **Rationale:** Conditioning on regime reduces false positives. Concordance filters noise.
- **Risk:** stacked selection means multiple-testing inflation. The Validation agent counts all component trials plus ensemble variants.
- **Capital:** same as components (₹55k–1.1 lakh). **Prior:** medium, and only if the components individually show weak positive OOS evidence.

---

### H17 — `S-SCALP-001` Intraday option scalping (owner-suggested family; RESEARCH, not built)
- **Idea:** many small long-premium trades a day, holding minutes, on short momentum bursts (order-flow or 1-minute breakouts), with tight premium stops and quick targets.
- **Entry cap: 1 a day** (owner default, 2-Oct-2026) until the [cost-drag study](../research/cost-drag-study.md) shows its costs are justified at the current NAV. The study puts that at about ₹5 lakh for a 10-a-day book. The spec model enforces it: an `S-SCALP-*` spec with `max_entries_per_day` above 1 fails validation. Lifting the cap is a code change with the owner's approval, never a spec edit.
- **Cost warning: very cost-sensitive.** At ₹20 an order, one lot of 65 pays ₹40 brokerage a round trip. That is ≈ ₹0.62 of premium per unit, ≈ 12 ticks, before statutory charges, spread and slippage. A scalp's typical move is a few ticks, so the gross edge per trade must clear the whole cost line (see `docs/research/cost-drag-study.md` for the break-even per trade by NAV and trade count). At ₹10k NAV the 2% budget is ₹200 a trade, so the charges alone are a large share of the risk.
- **Falsification:** net expectancy ≤ 0 after the dated cost model plus 2× slippage (V10), or a gross move per trade below the round-trip cost.
- **Data needed:** tick or 1-second quotes with real spreads (the 1-minute bars cannot test it).

### H18 — `S-RSIMACD-001` RSI / MACD reversal (owner-suggested family; RESEARCH, not built)
- **Idea:** buy the call (put) after an oversold (overbought) RSI reading that a MACD signal-line cross confirms, in MEAN_REVERTING regimes. `max_entries_per_day` 2–3.
- **Known risks:** classic indicator reversals are heavily mined, so the multiple-testing control (V9 DSR with the full trial count) matters. In TRENDING regimes the signal fights the trend, so the spec would prohibit them.
- **Falsification:** no positive OOS expectancy net of costs in MEAN_REVERTING regimes (V5/V14); performance concentrated in one era (V7).

---

## H19–H31: the institutional intelligence layer (3-Oct-2026, RESEARCH)

These come from [`docs/research/institutional-intelligence-layer.md`](../research/institutional-intelligence-layer.md), which cites each source with its URL and ranks every idea by expected applicability. **H19–H23 are draft StrategySpecs in `specs/drafts/`** (RESEARCH, loaded and checked by `tests/spec/test_research_drafts.py`). They are not in `specs/` yet because every library spec there needs a plug-in and an entry-cap row. **H24–H31 are features, filters or deferred ideas**, with no spec. No result exists for any of them; all evidence is `pending`. The regime-research worker backtests the drafts next. Every variant tried counts in the trial total for the Deflated Sharpe (V9).

### H19 — `S-DAYVOL-001` Intraday-only long straddle when forecast intraday variance beats the option price
- **Family:** volatility timing (long premium only). **Sources:** Muravyev & Ni (JFE 2020); Bhat, Pandey & Rao (J. Futures Markets 2024, NIFTY); Goyal & Saretto (JFE 2009); Corsi (2009, HAR).
- **Hypothesis:** if a HAR forecast of today's open-to-close realised variance is ≥ 1.25× the nearest-weekly ATM IV's one-session variance (IV²/252), an ATM straddle bought 09:45–11:00 and closed by 14:30 has positive net expectancy and beats the same straddle on low-ratio days.
- **Rationale:** the sellers' premium in NIFTY is earned mainly overnight; we are never overnight. Buy volatility only when intraday variance is forecast to exceed what the price implies. A guard skips a strongly inverted short end (panic pricing).
- **Constraint:** two lots (one a leg), so it needs an OD-013-style `strategy_max_lots` allowance from the owner; blocked in canary. **Capital:** ≈ ₹2L. **Prior:** medium (structural, but small per-day effect against two spreads).

### H20 — `S-NOISE-001` Breakout from the time-of-day noise area
- **Family:** intraday momentum. **Source:** Zarattini, Aziz & Barbon (SSRN 4824172, SPY).
- **Hypothesis:** a half-hour close (10:00–13:30) outside [min(open, prev close), max(open, prev close)] ± k × the 14-session mean absolute move from the open to that minute is followed by continuation; buy CE (above) or PE (below), exit on a half-hour close back inside the band, the stop or 14:45.
- **Capital:** ₹70k–1.3L. **Prior:** medium (index-level, index-only data, convex payoff; SPY results may not transfer).

### H21 — `S-IMOM-001` In-window late-session intraday momentum
- **Family:** intraday momentum / hedging demand. **Sources:** Gao, Han, Li & Zhou (JFE 2018); Baltussen, Da, Lammers & Martens (JFE 2021).
- **Hypothesis:** if |return from previous close to 13:30| ≥ 0.4% and the day's RV is at or above its 20-session median, the option in that direction bought 13:30–13:50 and closed by 14:45 has positive net expectancy. Expiry days are excluded.
- **Relation to H12:** H12 uses the first hour; H21 uses Baltussen's rest-of-day return. Both count as trials.
- **Capital:** ₹45k–95k. **Prior:** medium-low (the classic window is the exchange's last half-hour, outside OD-002).

### H22 — `S-GEXMO-001` Range-break momentum on high-gamma-concentration days
- **Family:** options-flow / dealer gamma. **Sources:** Barbon & Buraschi (SSRN 3725454); Dim, Eraker & Vilkov (SSRN 4692190); Baltussen et al. (JFE 2021).
- **Hypothesis:** when the partial-chain gamma concentration G at 10:15 (lake strikes, BS gamma × OI) is in the top tercile of 60 sessions, the first 15-minute close beyond the 09:15–10:15 range follows through ≥ 0.5 × range within 60 min more often than on other days.
- **Sign:** pre-registered "India-short" reading (non-individuals net short options, so hedging amplifies). If H24 finds high G predicts lower RV, H22 is falsified and any reversion variant is a new hypothesis.
- **Capital:** ₹70k–1.3L. **Prior:** low-medium (sign unknown; partial chain).

### H23 — `S-ORBML-001` Meta-labelled H01b
- **Family:** ML meta-labelling. **Sources:** López de Prado (AFML, Wiley 2018); Joubert (JFDS 2022); Bailey & López de Prado (DSR, 2014).
- **Hypothesis:** H01b signals kept by a pre-registered L2 logistic regression (C = 1, ten named features, triple-barrier labels, purged 5-fold CV, 1-day embargo, walk-forward monthly retrain) at p ≥ 0.55 have positive OOS net expectancy and beat all H01b signals.
- **Risk:** a few hundred labelled signals for ten features; it cannot add recall that H01b lacks (H01b's 2022–24 pipeline check was negative).
- **Capital:** as H01b. **Prior:** low-medium.

### H24 — `F-GEX-001` Gamma-concentration feature and its sign (feature)
- G every 15 minutes from the lake's OI and IV (code 1 ATM±10, code 2 ATM±3). First pre-registered test: does G's 60-day percentile at 10:15 predict 10:15–14:45 realised volatility and the sign of intraday return autocorrelation? This sets the sign for H22. The NSE participant-wise OI report would confirm it (availability UNVERIFIED).

### H25 — `F-TERM-001` Short-dated IV term structure (feature)
- This-week vs next-week ATM IV as per-day (forward) variance, plus India VIX. Test whether long-premium trades earn more when the front is flat or inverted than when the slope is steep (Johnson, JFQA 2017, for the VIX curve).

### H26 — `F-HMMREG-001` HMM regime classifier candidate (classifier, not a spec)
- 2–3-state Gaussian HMM on daily features (open-to-close RV, gap, VIX change, term slope, G percentile), walk-forward, filtered probabilities only (Hamilton 1989; Kritzman, Page & Turkington, FAJ 2012). Judged only by whether gating by it beats gating by K-11. Ships, if ever, as a new classifier version.

### H27 — `F-VOLKELLY-001` Volatility-scaled daily budget and quarter-Kelly cap (allocator rule)
- Once evidence exists: day budget × min(1, target RV / 20-day RV); per-trade budget ≤ quarter-Kelly from a calibrated meta-probability (Moreira & Muir 2017; Harvey et al., Man Group, 2018; Thorp 2006). It can only lower risk; with no validated edge the Kelly fraction is zero.

### H28 — `F-EXPHAZ-001` Expiry-afternoon hazard (filter)
- Measure whether expiry-day afternoons show more reversal of the morning's move than other days. If so, prohibit momentum entries after 13:00 on expiry days except for H07. Motivated by SEBI's interim order on Jane Street (3-Jul-2025; prima facie findings, contested), which describes index moves engineered on expiry days, including NIFTY in May 2025.

### H29 — `F-GAPATTN-001` Gap × prior-day attention (feature for H08a/H08b)
- Split gap-fade and gap-go trades by the previous day's absolute return and near-the-money option volume (Berkman et al., JFQA 2012; Lou, Polk & Skouras, JFE 2019).

### H30 — `F-TSMOM-001` Multi-week trend alignment (feature, low priority)
- Report trend-aligned vs counter-trend results for every directional spec (Moskowitz, Ooi & Pedersen, JFE 2012). A tie-breaker at most.

### H31 — `F-OFI-001` Order-flow imbalance entry timing (deferred: data first)
- Needs top-of-book and depth, which the lake does not have. Record ATM±3 and near-future depth snapshots during paper trading (data-only feed), then test minute-level OFI as an entry-timing filter (Cont, Kukanov & Stoikov 2014; for NIFTY options, Chakrabarti & Kotha 2017).

## Top-traders candidates TT-1..TT-5 (3-Oct-2026; promoted as H37-H41 the same evening)

Source: a literature review of documented traders and of technical-analysis evidence (kept private). Pre-registration drafts at
RESEARCH, drafted in `specs/drafts/top-traders/` and promoted to `specs/` on 3-Oct-2026 (21:00-21:30 IST) as
**H37 TT-1, H38 TT-2, H39 TT-3, H40 TT-4, H41 TT-5** with plug-ins in `strategies/library/signals_tt.py`; pre-registered
before any run. All count as trials. Prior: LOW for all (the lake rejected 17 + 5 intraday long-option specs).

### TT-1 — `S-TRDAY-001` Trend day, uncapped trailing exit
- **Family:** trend continuation (TSMOM, Moskowitz et al. JFE 2012; Gao et al. JFE 2018; Seykota/Turtles). **Regimes:** TRENDING_UP/DOWN, OPENING_DRIVE.
- **Hypothesis:** at 11:00, a ≥ 0.5% move from the open beyond the 09:15–10:15 initial balance, on the VWAP side and aligned with the 20-session return, continues to 14:45; one ATM leg, entry 11:00–12:30, **no fixed target**, exit on a trailing index stop (1 × morning range), invalidation or 14:45. Falsified also if trade returns are not positively skewed.
- **Capital:** ≈ ₹90k–1.6L.

### TT-2 — `S-NR7-001` NR7 contraction breakout
- **Family:** volatility contraction → expansion (Crabel; Raschke & Connors; volatility clustering). **Regimes:** VOLATILITY_COMPRESSION, VOLATILITY_NORMAL.
- **Hypothesis:** after an NR7 session, the first 1m close beyond that session's high/low (09:30–12:00) runs further by 14:45 than ordinary prior-range breaks. Directional, not a straddle.
- **Capital:** ≈ ₹1–1.8L.

### TT-3 — `S-OOPS-001` Failed gap beyond the prior extreme ("Oops")
- **Family:** gap reversal (Larry Williams; lake: overnight and intraday moves negatively related). **Regimes:** GAP_REGIME, OPENING_REVERSION.
- **Hypothesis:** an open ≥ 0.3% beyond the previous high (low) that re-enters the previous range before 11:00 reverses toward the previous close; buy against the gap on the re-entry. Differs from H08a/S-GAPFADE-001 by requiring confirmed failure beyond the prior extreme.
- **Capital:** ≈ ₹1–2L.

### TT-4 — `S-PBTREND-001` Pullback in the long-term trend
- **Family:** pullback with the trend (Elder triple screen; Connors RSI(2); Faber 200-day). **Regimes:** TRENDING_UP/DOWN.
- **Hypothesis:** previous close above its 200-session average and daily RSI(2) < 10: a CE on a break of the 09:15–09:45 high (09:45–11:30) has positive net expectancy; mirror for PE. Not the intraday RSI/MACD scalp tested elsewhere.
- **Capital:** ≈ ₹1–1.8L.

### TT-5 — `F-TREND200` 200-session trend filter (feature)
- CE only above the 200-session average, PE only below; evaluated as a pre-declared overlay on every directional spec's trade list. Falsified if the filtered book's OOS expectancy is not higher than the unfiltered (p ≥ 0.10). Overlaps H30 (multi-week alignment); report both.

TT-6..TT-9 (iron condor, put credit spread, hedged futures TSMOM, debit-spread TT-1) need owner decisions (OD-006,
OD-002/008) and are described, not drafted; Appendix P below applies to them.

## Summary table

| ID | Family | Prior | Min NAV (approx.) | ₹10k eligible? | Phase |
|---|---|---|---|---|---|
| H01 S-ORB-001 | ORB | med-low | ₹70k–1.1L | No | 2 |
| H02 S-FBO-001 | failed breakout | med-low | ₹70k–1.1L | No | 2 |
| H03 S-VWAPC-001 | VWAP continuation | medium | ₹55k–1.1L | No | 2 |
| H04 S-VWAPMR-001 | VWAP reversion | low-med | ₹30k–55k | No | 2 |
| H05 S-VOLX-001 | compression breakout | medium | ₹55k–1L | No | 2 |
| H06 S-IVRV-001 | IV vs RV, long premium | low-med | ₹55k–1.1L (single leg) | No | 2 |
| H07 S-EXP0-001 | expiry gamma | low | ≈ ₹10.6k at Upstox ₹20/order (₹211 at stop); ₹10k only at ₹0 brokerage | **No at Upstox** | 2 |
| H08 S-GAP-001 | gap residual | medium | ₹55k–1.1L | No | 2 |
| H09 S-OIM-001 | OI momentum | low | ₹55k–1.1L | No | 2 |
| H10 S-BASIS-001 | basis (feature) | low | n/a | n/a | 2 (feature) |
| H11 S-XSEC-001 | constituent lead | low-med | ₹55k–1.1L | No | 2 |
| H12 S-TOD-001 | intraday momentum | medium | ₹55k–1.1L | No | 2 |
| H13 S-SKEWL-001 | skew, buy call | low-med | ₹55k–1.1L | No | 2 |
| H14 S-EVT-001 | event, long straddle | low | ₹2–3L + 2-lot cap | No | 5 |
| H15 F-IVRANK-001 | IV-state filter (feature) | medium | n/a | n/a | 2 (feature) |
| H16 S-ENS-001 | ensemble | medium | ₹55k–1.1L | No | 2 |
| H19 S-DAYVOL-001 (draft) | intraday long straddle, vol timing | medium | ≈ ₹2L + 2-lot allowance | No | 2 |
| H20 S-NOISE-001 (draft) | noise-area momentum | medium | ₹70k–1.3L | No | 2 |
| H21 S-IMOM-001 (draft) | late-session intraday momentum | med-low | ₹45k–95k | No | 2 |
| H22 S-GEXMO-001 (draft) | gamma-conditioned range break | low-med | ₹70k–1.3L | No | 2 |
| H23 S-ORBML-001 (draft) | meta-labelled H01b | low-med | ₹70k–1.1L | No | 2 |
| H24–H31 | features / filters / deferred | — | n/a | n/a | 2 (features) |
| H37 TT-1 S-TRDAY-001 | trend day, uncapped exit | low | ₹90k–1.6L | No | 2 |
| H38 TT-2 S-NR7-001 | NR7 contraction breakout | low | ₹1–1.8L | No | 2 |
| H39 TT-3 S-OOPS-001 | failed gap beyond prior extreme | low | ₹1–2L | No | 2 |
| H40 TT-4 S-PBTREND-001 | pullback in 200-day trend | low | ₹1–1.8L | No | 2 |
| H41 TT-5 F-TREND200 | 200-session trend filter (feature) | low | n/a | n/a | 2 (feature) |

Min-NAV figures come from 01 §A3 (premium bands from model estimates) and are recomputed automatically from measured premiums and slippage once data exists.

---

## Strategy library (implemented specs, 2-Oct-2026)

The library turns H01–H08 and four new long-only ideas into StrategySpecs under `specs/` and runnable strategies under `src/project100c/strategies/library/` (H01 and H01b keep their own `strategies/orb_h01.py` and `orb_h01b.py`). **Every spec is RESEARCH**: no evidence, no confidence, and every number is a pre-registered ASSUMED prior, not a fitted value. The mechanics are tested on seeded SYNTHETIC days with a SYNTHETIC Black-Scholes chain (`tests/strategies/test_library.py`); those tests prove the plumbing, not an edge.

**Shared base (`LongOptionStrategy`).** Acts once per closed 1-minute index bar; runs its own K-11 classifier (UNVALIDATED) and refuses a signal the spec's regime policy does not permit (`regime_permits`). The entry is a BUY LIMIT at the last option close + 1 tick, cancelled after one bar and re-quoted up to `max_chase_ticks`. Summed `risk_at_stop` must fit 2% of NAV. Every filled leg gets an SL-LIMIT stop. Exits in order: the plug-in's own reason, the spec's REGIME_CHANGE invalidation, the R target, the time exit, the maximum hold. A straddle leg that is unfilled or refused closes the other (`LEG_UNFILLED` / `LEG_REJECTED`). From 14:50 the engine's forced flatten owns the position, and a still-working entry is cancelled.

Every spec prohibits NO_EDGE, ABNORMAL_MARKET and LOW_LIQUIDITY; the table lists the rest.

| Spec | Idea | Legs | Eligible regimes | Required | Also prohibited | Futures volume |
|---|---|---|---|---|---|---|
| `S-ORB-001` | H01 | BUY CE or PE (signal side) | GAP_REGIME, TRENDING_DOWN, TRENDING_UP, VOLATILITY_EXPANSION | — | EVENT_REGIME | UNCERTAIN; substitute: NIFTY index 1m bars; the volume filter is replaced by a 30-min range-expansion filter (H01-NV) |
| `S-ORB-002` | H01b | BUY CE or PE (signal side) | GAP_REGIME, TRENDING_DOWN, TRENDING_UP, VOLATILITY_EXPANSION | — | EVENT_REGIME | not used: the volume filter is near-the-money option volume (`nifty_opt_1m_atm_band`, OD-015) |
| `S-FBO-001` | H02 | BUY CE or PE (signal side) | MEAN_REVERTING, VOLATILITY_COMPRESSION, VOLATILITY_NORMAL | — | EVENT_REGIME | — |
| `S-VWAPC-001` | H03 | BUY CE or PE (signal side) | TRENDING_DOWN, TRENDING_UP | — | EVENT_REGIME | UNCERTAIN; substitute: index TWAP proxy for VWAP (the run metadata records the basis used) |
| `S-VWAPMR-001` | H04 | BUY CE or PE (signal side) | MEAN_REVERTING, VOLATILITY_COMPRESSION, VOLATILITY_NORMAL | — | EVENT_REGIME, GAP_REGIME | — |
| `S-VOLX-001` | H05 | BUY CE or PE (signal side) | VOLATILITY_COMPRESSION, VOLATILITY_NORMAL | — | EVENT_REGIME | UNCERTAIN; substitute: bar-range expansion alone confirms the break (the 'with volume' filter is dropped) |
| `S-IVRV-001` | H06 | BUY CE or PE (signal side) | VOLATILITY_EXPANSION, VOLATILITY_NORMAL | — | EVENT_REGIME | — |
| `S-EXP0-001` | H07 | BUY CE or PE (signal side) | TRENDING_DOWN, TRENDING_UP | EXPIRY_REGIME | EVENT_REGIME | — |
| `S-GAPGO-001` | H08a | BUY CE or PE (signal side) | GAP_REGIME, OPENING_DRIVE | GAP_REGIME | EVENT_REGIME | — |
| `S-GAPFADE-001` | H08b | BUY CE or PE (signal side) | GAP_REGIME, OPENING_REVERSION | GAP_REGIME | EVENT_REGIME | — |
| `S-VIXSTR-001` | new | BUY CE + BUY PE | VOLATILITY_EXPANSION | — | EVENT_REGIME | — |
| `S-EVTBO-001` | new | BUY CE or PE (signal side) | EVENT_REGIME | EVENT_REGIME | — | — |
| `S-LUNCH-001` | new | BUY CE or PE (signal side) | VOLATILITY_COMPRESSION, VOLATILITY_NORMAL | — | EVENT_REGIME | — |

### `S-ORB-001` (H01)
- **Hypothesis:** When NIFTY futures close a 5-min bar beyond the 09:15-09:30 opening range, with volume >= 1.5x its 20-day same-time median and India VIX not falling, the index keeps moving in the breakout direction >= 0.4 x OR-width within 60 min more often than a volatility-matched random entry does.

- **Mechanism:** Opening-range breaks aggregate overnight information and institutional order flow that executes over the morning.

- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) the 60-min follow-through rate is not above a volatility-matched random entry's (p >= 0.10)
- **Cost sensitivity:** An ATM premium of INR 70-130 with a 30% stop risks about INR 1,400-2,500 per lot; INR 40 brokerage plus statutory charges and 2 ticks/side slippage cost about INR 50-70 a round trip, i.e. 3-5% of the risk. The edge must survive 2x slippage (docs/research/validation.md V5).

- **Data:** `nifty_fut_1m` is UNCERTAIN; substitute: NIFTY index 1m bars; the volume filter is replaced by a 30-min range-expansion filter (H01-NV)

### `S-ORB-002` (H01b, approved 2-Oct-2026 with OD-015)
- **What changes from H01:** the breakout is measured on the NIFTY **index**, and the volume filter uses **near-the-money option volume**: the summed nearest-expiry CE+PE volume within ±1 strike of the index, against its 20-day same-slot median. Dhan has no 1-minute history for expired futures, so H01 as specified cannot be tested on Dhan data. H01 (`S-ORB-001`) is unchanged.
- **Status:** RESEARCH. It is a new hypothesis with its own evidence, which starts at zero.
- **Implementation:** `strategies/orb_h01b.py` (H01's mechanics on the `h01b_signal_bars` series, `H01B-SIGNAL-2026-10-02.1`) and `specs/S-ORB-002.yaml`. `scripts/h01_real_data.py --hypothesis H01b` runs it on the real lake.

### `S-FBO-001` (H02)
- **Hypothesis:** A close beyond the 09:15-09:30 opening range that is back inside by >= 0.2 x OR-width within 15 minutes is followed by a move to the range midpoint more often than chance.
- **Mechanism:** Stops clustered beyond obvious levels are run, liquidity providers absorb them, and trapped breakout traders then exit, pushing price back across the range.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) the midpoint-reached rate after a failed break is not above the unconditional rate for the same time of day
- **Cost sensitivity:** The target (range midpoint) is about half an OR-width, typically 30-60 index points or 15-30 premium points at delta 0.5; a round trip costs about INR 50-70 plus 2-3 ticks of slippage per side. Narrow-range days make it cost-dominated.
- **Implementation deviation:** VOLUME: 'declining volume on re-entry' is not checked (futures volume UNCERTAIN); price-only rule.
- **Implementation deviation:** LEVELS: only the opening range is used, not the previous-day high/low.
- **Implementation deviation:** TARGET: the range midpoint is the first and only target (no scale-out to the opposite edge).

### `S-VWAPC-001` (H03)
- **Hypothesis:** On a day that has held one side of session VWAP for >= 70% of minutes since 09:30, a pullback that touches VWAP and closes back in the trend direction continues for at least 1.5 R more often than it fails.
- **Mechanism:** Institutional execution is benchmarked to VWAP; on trend days pullbacks to VWAP attract benchmark-driven flow in the trend direction.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) gated (trend days) results are not better than the same rule on all days
- **Cost sensitivity:** A 1.5 R target on a 30% stop is a 45% premium move; at ATM premiums of INR 80-150 that is 35-70 points, so costs are 5-10% of the target. The futures-VWAP vs TWAP choice can change the entries; both must be reported.
- **Data:** `nifty_fut_1m` is UNCERTAIN; substitute: index TWAP proxy for VWAP (the run metadata records the basis used)
- **Implementation deviation:** VWAP: futures-volume VWAP when futures volume is supplied, else the index TWAP proxy (the substitute); the run metadata records which basis was used.
- **Implementation deviation:** BANDS: the touch band and the stop are in % of VWAP, not in intraday sigma.
- **Implementation deviation:** TARGET: 1.5 R only (the prior-swing target is not implemented).

### `S-VWAPMR-001` (H04)
- **Hypothesis:** On range days with low realised volatility, a deviation of NIFTY from session VWAP beyond 1.5 sigma that starts to turn reverts to VWAP within 20 minutes more often than it extends to 2.5 sigma.
- **Mechanism:** In low-volatility regimes liquidity providers and short-gamma option writers hedge in a mean-reverting way, buying dips and selling rallies.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) the 20-minute VWAP-touch rate after a 1.5 sigma stretch is not above that of a random entry at the same deviation
- **Cost sensitivity:** Low-vol ATM premiums are INR 40-80, so a 1.5 sigma reversion of 20-40 index points is 10-20 premium points; round-trip costs plus 3 ticks/side are 25-40% of that. The most cost-sensitive spec in the library.
- **Implementation deviation:** VWAP: the index TWAP proxy (no futures volume); sigma is the session standard deviation of close - VWAP.
- **Implementation deviation:** VIX_TERCILE: 'VIX in the bottom tercile of its 1-year range' is replaced by the classifier's volatility label.

### `S-VOLX-001` (H05)
- **Hypothesis:** After 45 minutes in which NIFTY's high-low box is within 0.25% of price, a close beyond the box on an expanding bar is followed by a 30-minute move larger than ATM premium implies.
- **Mechanism:** Volatility clusters; intraday option premiums price average volatility and may underprice the expansion that follows compression.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) post-compression 30-minute absolute moves are not larger than the ATM straddle-implied move
- **Cost sensitivity:** A 2 R target on a 30% stop is a 60% premium move; it needs an expansion of 40-80 index points. Costs are 5-8% of the target, so the edge is mostly about the hit rate, not costs.
- **Data:** `nifty_fut_1m` is UNCERTAIN; substitute: bar-range expansion alone confirms the break (the 'with volume' filter is dropped)
- **Implementation deviation:** VOLUME: 'with volume' uses futures volume when supplied; without it the bar-range expansion filter alone is the substitute.
- **Implementation deviation:** COMPRESSION: an absolute box height (% of price) replaces 'bottom decile of the day's 15-min ranges'.

### `S-IVRV-001` (H06)
- **Hypothesis:** When India VIX (as a proxy for ATM weekly IV) is below trailing intraday realised volatility by more than 2 vol points, a single ATM option bought in the direction of the VWAP side and 15-minute momentum has higher net expectancy than the same entry when IV >= RV.
- **Mechanism:** The variance risk premium is usually positive and favours option sellers, who are prohibited here (OD-006); the only use of it is to buy premium only when it is conditionally cheap.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) the IV < RV subset does not beat the IV >= RV subset of the same directional rule (paired comparison)
- **Cost sensitivity:** Costs are the same as any ATM entry (INR 50-70 a round trip); the hypothesis is about the price paid for gamma, so it must be reported against the unconditioned version net of identical costs.
- **Data:** `nifty_atm_iv_1m` is UNAVAILABLE; substitute: India VIX as the ATM IV proxy until D-11 (own Black-76 IV) is built
- **Implementation deviation:** IV: India VIX stands in for the own Black-76 ATM weekly IV (D-11 not built).
- **Implementation deviation:** RV: trailing realised vol over rv_minutes stands in for the HAR-RV forecast.
- **Implementation deviation:** DIRECTION: VWAP side from the index TWAP proxy plus the sign of the 15-minute return.

### `S-EXP0-001` (H07)
- **Hypothesis:** On NIFTY weekly expiry days after 13:00, a new session extreme with a 15-minute move >= 0.25% extends further before 14:50 more often than the 0-DTE option prices imply.
- **Mechanism:** Large short-gamma open interest held by option writers on expiry day means their delta-hedging adds to moves (dealer-gamma feedback).
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) the extension rate after the signal is not above the rate implied by the 0-DTE option's delta and price
- **Cost sensitivity:** The most cost-sensitive at small size: on an INR 8 premium, INR 40 round-trip brokerage is INR 0.6 per unit (7.7% of premium) before statutory charges and 1-2 ticks of spread (0.6-1.2% per tick). At Upstox INR 20/order it is ineligible at INR 10k NAV (docs/research/strategy-hypotheses.md H07).
- **Implementation deviation:** MOMENTUM: a fixed 15-minute move threshold replaces 'above the 80th percentile (time-matched)'.
- **Implementation deviation:** PREMIUM_BAND: the INR 5-20 premium band is not enforced; the strike is otm_steps from spot.
- **Implementation deviation:** UNDERLYING: index bars stand in for futures.

### `S-GAPGO-001` (H08a)
- **Hypothesis:** A NIFTY opening gap of >= 0.5% whose first 30 minutes are a drive in the gap direction continues beyond the opening range in that direction by at least 1.5 R more often than it fails.
- **Mechanism:** When overnight information is large and the opening auction does not fully absorb it, institutions keep executing in the gap direction through the morning.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) gap-and-drive days do not continue beyond the OR more often than non-drive gap days
- **Cost sensitivity:** Gap days have higher IV, so ATM premiums are INR 100-180; a 1.5 R target is 45-80 premium points and costs are 3-6% of it. Spread widening at the open is the larger risk; the entry starts at 09:45 for that reason.
- **Implementation deviation:** DRIVE: the opening character comes from the K-11 classifier (UNVALIDATED) at 09:45.

### `S-GAPFADE-001` (H08b)
- **Hypothesis:** A NIFTY opening gap of >= 0.5% whose first 30 minutes revert (the classifier's OPENING_REVERSION) goes on to fill the gap to the previous close more often than it resumes beyond the opening range.
- **Mechanism:** The opening auction overreacts to thin overnight information; when early trade rejects the gap, trapped gap-chasers exit and the gap fills.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) gap-fill rates on reversion-open days are not above those on all gap days
- **Cost sensitivity:** The target is the remaining gap (typically 50-120 index points, 25-60 premium points at delta 0.5); costs are 3-8% of it, rising sharply on smaller gaps, hence the 0.5% minimum.
- **Implementation deviation:** REVERSION: the opening character comes from the K-11 classifier (UNVALIDATED) at 09:45.

### `S-VIXSTR-001` (new idea)
- **Hypothesis:** When India VIX jumps >= 8% within 30 minutes while NIFTY has moved >= 0.4% in 15 minutes, a long ATM straddle bought then earns more from the realised move than it loses to theta and the IV change within 45 minutes.
- **Mechanism:** Volatility shocks cluster: a sudden VIX jump with a large index move is often followed by further large moves before implied volatility fully reprices.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) the straddle's 45-minute P&L after a spike is not above that of the same straddle bought at random times with matched VIX levels
- **Cost sensitivity:** Two legs double the charges (about INR 100-140 a round trip at INR 20/order) and the slippage; the straddle needs a realised move larger than the combined premium's theta plus about 4-6% of its value in costs.
- **Implementation deviation:** LOTS: two concurrent lots. The 1-lot canary cap (limits.toml max_lots = 1, Governor MAX_POSITION) blocks it in canary; mechanics tests run the engine with max_lots = 2.
- **Implementation deviation:** STOP: a premium stop per leg, not one on the combined premium.

### `S-EVTBO-001` (new idea)
- **Hypothesis:** On a listed event day (RBI MPC, Union Budget, election results), a close beyond the range of the 30 minutes before the announcement keeps going in that direction for at least 1.5 R more often than it reverses.
- **Mechanism:** Scheduled announcements resolve uncertainty in one direction; positioning that waited for the event executes after it, so the first decisive break tends to carry.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) post-announcement breaks do not continue more often than breaks of a same-sized box on non-event days
- **Cost sensitivity:** Event-day IV is high (ATM INR 120-250), so the 30% stop risks INR 2,300-4,900 per lot; costs are small relative to that, but spreads at the announcement can be 5-10 ticks, which matters more than charges.
- **Implementation deviation:** EVENT_TIME: the announcement time is the spec param event_minute (ASSUMED 10:00 IST for RBI MPC); the event calendar lists dates only.
- **Implementation deviation:** CERTIFICATION: the Governor needs event_certified intents on event days; that flag is not set by research.

### `S-LUNCH-001` (new idea)
- **Hypothesis:** A 12:00-13:00 NIFTY range no taller than 0.2% of price and 40% of the morning range, broken after 13:00 on an expanding bar, is followed by a move of at least 1.5 R before 14:40 more often than chance.
- **Mechanism:** Lunchtime liquidity is thin and the range compresses; the return of afternoon participants (and European open flows) resolves it, and the first expansion often runs.
- **Falsified if:** (1) OOS net expectancy per trade (R) 90% bootstrap CI upper bound < 0 (2) afternoon moves after a lunch-box break are not larger than after afternoons without a compressed lunch box
- **Cost sensitivity:** Afternoon ATM premiums are lower (INR 50-110); a 1.5 R target on a 25% stop is a 37.5% premium move (20-40 points). Costs are 6-12% of the target.
- **Implementation deviation:** VOLUME: no volume confirmation (index bars only); bar-range expansion is the confirmation.

**Straddle (S-VIXSTR-001) and the canary cap: OD-013.** A long straddle is two BUY legs, so it complies with long-only (OD-001), but it needs two concurrent lots. The owner approved this on 2-Oct-2026 (OD-013): one lot per leg, with the combined risk at both stops at most 2% of NAV. It is the only exception to the 1-lot cap. `limits.toml` RL-2026-10-02.1 sets `strategy_max_lots` for S-VIXSTR-001 and for the H06 straddle variant (S-IVRV-001; the variant is not built yet). The Governor requires a CE+PE pair with the same underlying, expiry and strike (otherwise NOT_A_STRADDLE). It also requires that nothing else is open, and it charges both legs to one 2% budget (`tests/kernel/test_governor_lot_cap.py`). Under a plain 1-lot cap the second leg is refused and the strategy closes the first (`LEG_REJECTED`, tested).

**Event days (S-EVTBO-001).** It trades only on *verified* days in `configs/calendar/events.yaml`: RBI MPC decision days from the RBI's own schedules and resolutions, and the Union Budget day; unverified entries are ignored. The next listed day is the RBI MPC decision on Wednesday 7-Oct-2026 (scheduled). Live entries on event days also need `event_certified` intents (docs/risk/risk-engine.md §9.2), which no strategy has yet.

### Volatility round (H32-H35, pre-registered 3-Oct-2026)

Pre-registered before any run; plug-ins in `strategies/library/signals_vol.py`. All RESEARCH; no edge is claimed.

- `S-VOLCHEAP-001` (H32): high forecast volatility (K-11 EXPANSION, H25 TERM_LOW or H26 TURBULENT) and HAR forecast >= the ATM IV's one-session variance -> long ATM straddle with a premium-aware stop.
- `S-VOLHOLD-001` (H33): the label alone -> straddle 09:31-09:45, held toward 14:45 (intraday only; an overnight variant is BLOCKED pending an owner decision).
- `S-EXPVOL-001` (H34): expiry day + the label -> 0-DTE straddle; event days excluded (no certification, calendar from Apr-2025).
- `S-VOLOFF-001` (H35): a stand-down overlay on the existing strategies (calm-forecast sessions off); `configs/research/vol_round.toml`, no spec file.
- H36 (a multi-timeframe trend scalp) is a private research candidate and is not described here.
- A strangle variant (H32s) needs a new owner decision (OD-013 is same-strike only) and is not built.

### OD-019 sell/buy switch (H42-H48, pre-registered 3-Oct-2026)

OD-019 (3-Oct-2026) puts defined-risk option selling into RESEARCH and PAPER scope only; live stays long-only
(OD-006). Pre-registered before any run. Structures use the research schema `spec/structure.py`, which the live loader
and the Governor never read; synthetic examples are in `examples/structures/`, and the real structure specs and round
configs live in the private alpha library (`project100c.alpha`).

- `X-EXPDAY-001` (H42): expiry-day study (range, IV crush, gamma moves; buyer vs seller at 09:20 / 10:30 / 13:00, ATM / OTM1 / OTM2).
- `X-VOLBRK-001` (H43): volume-break study (1-min ≥ 4×, 5-min ≥ 3× the same-minute 20-session median of near-ATM option volume); `S-VOLBRK-B-001` the BUY rule.
- H44-H46: defined-risk premium-selling structures (an iron condor, a trend-side credit spread and a hedged short strangle), each with an intraday and an overnight variant, behind a volatility-premium sell gate. Exact strikes, timings and gate thresholds are private.
- `ST-2026-10-03.1`: synthetic ±13% / IV 86% and 60% shocks, 10× slippage, plus the 4-Jun-2024 replay.
- `P-SWITCH-001` (H47): expiry day → BUY (volume breaks); other days → SELL the intraday condor if the sell gate passes, else BUY on a volume break, else NO-TRADE.

### Entry caps (OD-014 defaults, 2-Oct-2026)

A new or uncapped strategy gets **1 entry a day** (the `Entry.max_entries_per_day` default; a Governor configured with spec caps also gives an unlisted strategy 1). The library strategies get 1–3 by style, as below, under the system-wide cap of 10 (`limits.toml max_trades_per_day`).

- The caps sum to 32, so the system cap and the 4% daily risk budget bind first on a busy day.
- The [cost-drag study](../research/cost-drag-study.md) suggests about 3 entries a day across the whole book at ₹1L and about 5 at ₹2L, so these are ceilings, not targets.
- A long straddle counts as one entry.
- 3 is kept for a repeatable style whose costs are shown to be justified, such as the H18 RSI/MACD reversal at 2–3.
- `tests/spec/test_entry_cap_defaults.py` keeps this table and the specs in step.

| Spec | Cap/day | Why (style) |
|:---|---:|:---|
| S-GAPGO-001 | 1 | one opening gap a day: the setup exists once |
| S-GAPFADE-001 | 1 | one opening gap a day: the setup exists once |
| S-ORB-001 | 1 | one opening range a day; a re-entry would be the failed-breakout mirror (S-FBO-001) |
| S-ORB-002 | 1 | as H01: one opening range a day (H01b, option-volume filter) |
| S-FBO-001 | 2 | the opening range can fail on both sides (one up, one down) |
| S-VWAPC-001 | 2 | a trend day can give more than one VWAP pullback; held at 2 for cost drag |
| S-VWAPMR-001 | 2 | range days give repeated stretches, but it is the library's most cost-sensitive spec: 2, not 3 |
| S-VOLX-001 | 2 | compression can build and break twice (morning and afternoon) |
| S-IVRV-001 | 1 | one cheap-gamma view of the day's IV against RV |
| S-EXP0-001 | 1 | one expiry-afternoon window |
| S-VIXSTR-001 | 1 | one straddle a day (CE+PE, same expiry and strike, is ONE entry) |
| S-EVTBO-001 | 1 | one event a day; it also needs event certification, which no spec has |
| S-LUNCH-001 | 1 | one lunch window a day |
| S-DAYVOL-001 | 1 | one straddle a day (CE+PE, same expiry and strike, is ONE entry; OD-013 allows its 2 lots) |
| S-NOISE-001 | 1 | new-strategy default; a reversal re-entry would be a new hypothesis |
| S-IMOM-001 | 1 | the 13:30 setup exists once a day |
| S-GEXMO-001 | 1 | one first-hour range a day |
| S-VOLCHEAP-001 | 1 | one straddle a day (H32; ONE entry, OD-013 2 lots) |
| S-VOLHOLD-001 | 1 | one straddle a day, entered 09:31-09:45 (H33) |
| S-EXPVOL-001 | 1 | one expiry-day straddle (H34) |
| S-TRDAY-001 | 1 | one trend-day entry (H37) |
| S-NR7-001 | 1 | one NR7 break a day (H38) |
| S-OOPS-001 | 1 | one opening gap a day (H39) |
| S-PBTREND-001 | 1 | one confirmation of a daily setup (H40) |
| S-SCALP-001 (H17, not built) | 1 | held at 1 until the cost-drag study justifies more at the current NAV (enforced by the spec model) |

## Portfolio allocator v1 (S-05, 2-Oct-2026)

`src/project100c/portfolio/allocator.py`, `configs/portfolio/allocator.toml` (`AL-2026-10-02.1`, every number ASSUMED). A pure function: strategy records (spec, status, kill flag, consecutive losses, own drawdown, realised/assumed slippage), the NAV, the latest K-11 reading and the day's risk already used → an `AllocationPlan` with, per strategy, *eligible* or *refused* (every reason listed) and a per-trade risk budget in INR. It is advisory: it can only lower risk, and the Governor still checks every intent.

| Rule | Refusal / effect |
|---|---|
| Lifecycle: LIVE mode allows CANARY / PRODUCTION / DEGRADED; SIMULATE allows everything but QUARANTINED / RETIRED | STATUS_NOT_ELIGIBLE |
| A strategy kill is active | STRATEGY_KILLED |
| `min_capital_inr` unknown (LIVE) or above the NAV | CAPITAL_UNKNOWN / CAPITAL_INELIGIBLE |
| The spec's regime policy refuses the current reading (same `RegimeGate` as Governor check 17); a missing or stale reading is refused; in LIVE mode an UNVALIDATED classifier is NO_EDGE | REGIME_UNKNOWN / REGIME_STALE / REGIME_BLOCKED / REGIME_NOT_ALLOWED |
| Realised slippage ≥ 2× assumed | SLIPPAGE_BREACH |
| Mirror-image strategies (H01/H02, gap-go/gap-fade, VWAP continuation/reversion): only the first eligible member in priority order | EXCLUSIVE_GROUP |
| Nothing left of the day's risk budget | NO_DAILY_RISK_LEFT |
| Sizing: the day's remaining risk (4% of NAV minus what is used) shared equally among the eligible, capped at 2% of NAV per trade | — |
| Auto-decrease (compounding): ≥ 3 consecutive losses ×0.5; own drawdown ≥ 5% NAV ×0.5; slippage ≥ 1.5× ×0.5; DEGRADED ×0.5 | multiplier |

A property test checks, over random regimes, NAVs, priorities and used risk, that no budget exceeds 2% of NAV, the total never exceeds the day's remaining risk, refused always means zero, and at most one member of each exclusive group is eligible.

## Appendix P: Prohibited under the current mandate (OD-006, long options only)

These ideas need at least one **short option leg**. The Risk Governor rejects them deterministically (sell-to-open → REJECT `MANDATE_LONG_ONLY`). They are recorded so the research history is complete. They **may not be backtested for deployment, paper-traded or promoted**, and may be revisited only if the owner changes the mandate.

| ID | Original idea | Why prohibited |
|---|---|---|
| P-1 (ex-H13) | Put-skew spike → **short put spread** + long call | Short put leg |
| P-2 (ex-H14 converse) | Event implied move > historical → defined-risk **short vol** | Short straddle/strangle or credit spread legs |
| P-3 (ex-H15 `S-IC-001`) | High-IV **iron fly/condor**, same-day or 1-DTE, 1.5× credit stop | Two short legs; also a max loss ≫ ₹200 and 8 orders per round trip |
| P-4 | **Debit spreads** (bull call / bear put) to cut premium outlay | The short leg of the vertical |
| P-5 | **Straddle/strangle selling**, ratio spreads, calendars/diagonals, covered or "hedged" writing | Short option legs |

The long-premium re-expressions are H06 (cheap-gamma timing), H13 (`S-SKEWL-001`) and H15 (`F-IVRANK-001` filter).

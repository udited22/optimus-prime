# What has been falsified (as of 8-Oct-2026)

Summary of the research rounds on the real NIFTY lake (Aug-2022..Mar-2026 research days, holdout excluded). Numbers are
out-of-sample and net of dated charges unless stated. **No strategy has passed the validation gates.** About 445 trials
are registered.

| Family | What was tested | Result |
|---|---|---|
| Intraday option **buying** (17-spec library: opening range, failed breakout, VWAP, gaps, expiry, IV/RV, event, lunch, noise area, intraday momentum, dealer gamma) | Each spec, ungated and regime-gated | Almost every spec **loses before costs**, not only after. See [regime-portfolio-2026-10-03.md](regime-portfolio-2026-10-03.md) |
| Regime models (K-11 v0, a stabilised trend candidate, a term-structure model, an HMM) | Stability and predictive validity, then as a gate | Volatility labels are informative about future realised volatility; **trend labels carry no directional information**; no regime gate turns a losing strategy into a passing one. See [regime-classifier-validation-2026-10-03.md](regime-classifier-validation-2026-10-03.md) |
| Regime → strategy books | Walk-forward map selection | Books picked in-sample look profitable in-sample and **lose out of sample**: selection bias, not edge |
| Volatility-information straddles (H32–H34) | Long straddles on high-forecast-volatility days | Rejected at the gates |
| Documented-trader rules (trend day, NR7, "Oops", pullback in trend, 200-day filter) | Pre-registered translations to NIFTY options | Lose out of sample; the trend filter improves nothing |
| Intraday defined-risk **premium selling** (iron condor, credit spread, hedged strangle, 09:45–14:45) | Pre-registered structures | Negative with CIs below zero: 4–6 legs of brokerage and sell-side STT outweigh a few hours of theta |
| Expiry-day and volume-break "edges" | Buyer vs seller cells; post-spike behaviour | No cell survives; the buy/sell switch built on them loses |
| Cost drag | A coin-flip strategy through the real stack | Costs dominate at small NAV and high entry counts; see [cost-drag-study.md](cost-drag-study.md) |

**Where this points.** Intraday indicator timing on index options is dropped. The only positive signs so far are in
the variance risk premium held overnight with defined risk, and one intraday scalp; both have confidence intervals that
include zero, need several lakh of capital per lot, and are being checked forward on PAPER. Their exact rules are
private. Next research directions, ranked by economic rationale: the NIFTY variance risk premium (defined risk,
overnight), crypto perpetual funding and basis carry, and Indian equity factor premia.

# Institutional intelligence layer: published research mapped onto long-only NIFTY options

*Written 2–3-Oct-2026 IST for the owner. RESEARCH only. Nothing here is a backtest result, a validated edge or investment advice. Every number quoted from a source belongs to that source's sample (mostly US index options or futures) and is **not** a claim about NIFTY.*

## 0. Purpose, rules and how to read this

**Purpose.** Turn the best published trading research and documented practitioner work into an *intelligence layer* for Project 100C: a ranked set of candidate strategies, features and filters, each tied to a real source, a NIFTY-specific mechanism, the data it needs, and how sensitive it is to costs.

**The mandate it has to fit (unchanged):**
- Long options only (OD-006): BUY CE or BUY PE; no selling, no spread legs. Two BUY legs (a long straddle) only where OD-013 allows.
- 2% of NAV at risk per trade; entries 09:20–14:00 IST (OD-008); forced flatten from 14:50 and flat by 15:00 (OD-002). So **no position is ever held overnight or over a weekend.**
- ₹10k canary first, scaling to ₹1–2L only on evidence. At ₹10k almost every NIFTY lot is refused by the 2% cap, and that is the rules working.

**Citation rules used here.**
- Every source has a URL that was resolved on 2- or 3-Oct-2026 (journal page, RePEc/IDEAS, SSRN, the author's or institution's page, or the regulator's own page). SSRN pages answer search engines but refuse `curl` from the box (HTTP 403); those were confirmed through search results.
- Where a finding is quoted, it is quoted from the abstract or the official text. Where I am paraphrasing a mechanism, I say so.
- **Institutions with no verifiable public trading research on these topics are left out rather than invented.** I found nothing citable from Goldman Sachs or Citadel, and no Jane Street talk on trading strategy that I could verify. Jane Street appears below only through SEBI's interim order about its index-options trading. JPMorgan's 0DTE note is cited only through press reporting of it, and labelled that way. BlackRock appears once, for a tangential point.

**Each topic section has the same fields:** source(s) → core finding → does it apply to long-only intraday NIFTY options, and how → data needed (and whether the lake has it) → cost sensitivity → concrete proposal (a draft spec, a feature or filter, or "deferred").

**Status of the proposals.** Five draft specs (H19–H23) are in `specs/drafts/` at RESEARCH status, loaded and checked by the project's own spec loader (`tests/spec/test_research_drafts.py`). They are kept out of `specs/` itself on purpose: everything in `specs/S-*.yaml` must have a runnable library plug-in and a documented entry cap, and these have neither yet. The regime-research worker backtests them next. Feature hypotheses (H24–H31) are recorded in docs/research/strategy-hypotheses.md only.

## 1. What the lake can and cannot answer (checked 3-Oct-2026)

The 5-year Dhan backfill (`lake/`, gitignored; see [the coverage report](../data/dhan-coverage-2026-10-02.md)) decides what can be tested now.

| Dataset | In the lake? | Notes |
|---|---|---|
| NIFTY index 1-minute OHLC | **Yes** | Newest contiguous clean range 25-Jul-2022..1-Oct-2026; 1,047 of 1,230 sessions since Oct-2021 have bars. The index has no volume. |
| India VIX 1-minute | **Yes** | Same coverage as the index. One 30-day tenor only. |
| Nearest-expiry options (expiry code 1), ATM−10..ATM+10, CE and PE, 1-minute | **Yes** | OHLC, volume, **open interest, Dhan's IV, strike and spot on every bar**. A spot check of ATM code-1 CE/PE (≈ 0.9 M rows, Oct-2021..Oct-2026) found no null IV or spot, OI > 0 on every row, and IV ≤ 0 on about 0.5% of rows (to be treated as missing). Some deep strikes are quarantined in parts of 2021–2025. |
| Next expiry (code 2), ATM−3..ATM+3 | **Yes** | Same fields. With code 1 this gives a **short-dated IV term structure** (this week vs next week) every minute. |
| NIFTY futures 1-minute | **Partial** | Only the 3 contracts active on 2-Oct-2026, from Jul-2026; Dhan has no history for expired futures. |
| Bid/ask quotes, depth, ticks, trade signs | **No** | Spreads are ASSUMED in every backtest. Order-flow-imbalance work cannot be tested. |
| Full option chain (all strikes, all expiries) | **No** | Only ATM±10 (code 1) and ATM±3 (code 2): about ±500 index points around spot. Any gamma-exposure estimate is a partial-chain proxy. |
| Who holds the options (client / pro / FII / DII) | **No** | NSE publishes a daily participant-wise open-interest report; it was not fetched (NSE returns 403 to the box) and its format is UNVERIFIED here. |
| Constituent stocks, index weights | **No** | — |

## 2. Honesty first: most retail F&O traders lose, and option buyers start behind

**What the regulator measured.**
- SEBI's first study (Jan-2023) found that 89% of individual F&O traders lost money in FY22. [SEBI study page](https://www.sebi.gov.in/reports-and-statistics/research/jan-2023/study-analysis-of-profit-and-loss-of-individual-traders-dealing-in-equity-fando-segment_67525.html)
- The updated study (23-Sep-2024, [press release](https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html), [report PDF](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2024/1727085659479.pdf)), quoted from the report:
  - 1.13 crore unique individuals lost a combined ₹1.81 lakh crore over FY22–FY24 (net of costs); 92.8% of individual traders were loss-makers over the three years and **only 7.2% made a profit**; only 1% made more than ₹1 lakh.
  - FY24: 91.1% of individuals lost money. **In options 91.5% lost, against about 60% in futures.** Higher trading activity went with a higher share of loss-makers.
  - Individuals paid more than ₹50,000 crore in transaction costs over FY22–FY24, about ₹26,000 per person in FY24; brokerage was 51% and exchange fees 20% of those costs.
  - The other side: in FY24 proprietary traders earned about ₹33,000 crore and FPIs about ₹28,000 crore of gross F&O profit, and **96–97% of those profits came from algorithmic entities**.
- The FY25 follow-up (Jul-2025, [SEBI PDF](https://www.sebi.gov.in/sebi_data/attachdocs/jul-2025/1751900271726.pdf)): about 91% of individuals still lost money; net losses widened 41% to ₹1,05,603 crore.
- The FY25–FY26 study (SEBI DEPA-II, Aug-2026, [SEBI PDF](https://www.sebi.gov.in/sebi_data/attachdocs/aug-2026/1787233506209.pdf)), quoted from its executive summary: 87.7% of individuals lost money in FY26 (90.9% in FY25); aggregate net losses ₹91,685 crore; **options were 92% of individual losses**; the average loss of a loss-maker (₹1.47 lakh) was 21% larger than the average profit of a profit-maker; individuals' *gross* trading loss (before costs) was about ₹72,000 crore; **99% of FPI and proprietary profits were made by algo entities**; 70% of FY25 index-option turnover was on expiry day (59% after SEBI's measures); traders with portfolios under ₹1 lakh and turnover over ₹1 crore were 13% of traders but 52% of losses.
- US evidence points the same way. Beckmeyer, Branger & Gayda, ["Retail Traders Love 0DTE Options… But Should They?"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4404704) (SSRN 4404704), find that retail traders lose on average in S&P 500 0DTE options, mainly through transaction costs, single-leg trades, premium-paying trades and high-IV options.

**Why a long-option buyer starts behind.**
- **The volatility risk premium (VRP).** Index options are, on average, priced above the volatility that is later realised, so buyers pay a premium. Bakshi & Kapadia, ["Delta-Hedged Gains and the Negative Market Volatility Risk Premium"](https://ideas.repec.org/a/oup/rfinst/v16y2003i2p527-566.html) (RFS 2003), find that delta-hedged S&P 500 option positions underperform zero. Coval & Shumway, ["Expected Option Returns"](https://ideas.repec.org/a/bla/jfinan/v56y2001i3p983-1009.html) (JF 2001), find that zero-beta ATM index straddles lose about 3% a week. AQR's white paper ["Understanding the Volatility Risk Premium"](https://www.aqr.com/Insights/Research/White-Papers/Understanding-the-Volatility-Risk-Premium) (Ang, Israelov, Sullivan & Tummala, 2018) is the practitioner case for *selling* that premium, which our mandate forbids.
- **India too.** Garg & Vipul, ["Volatility Risk Premium in Indian Options Prices"](https://ideas.repec.org/a/wly/jfutmk/v35y2015i9p795-812.html) (J. Futures Markets 2015): the VRP is priced in Indian options, but strategies exploiting it are "substantially reduced" by normal transaction costs and help "only… option writers, who have sufficiently low transaction costs". Sankar, Ramachandran & Lukose, ["Dynamics of variance risk premium: Evidence from India"](https://ideas.repec.org/a/eee/reveco/v70y2020icp321-334.html) (IREF 2020), reject the idea that a retail-heavy market does not price variance risk.
- **Theta.** A bought option loses time value every minute that the expected move does not come, and fastest close to expiry, where most NIFTY volume trades.
- **Costs.** At ₹20 an order, a 65-unit lot pays ₹40 of brokerage a round trip plus about ₹8–20 of statutory charges (README), before spread and slippage. With a ₹100 ATM premium and a 30% stop, risk is about ₹1,950 a lot, so a round trip costs about 3% of the risk before slippage.

**The one structural point in our favour, and its limits.** The research in §3 (Muravyev & Ni; Bhat, Pandey & Rao for NIFTY) finds that the option-seller's premium is earned mainly **overnight**; intraday, delta-hedged option returns are close to zero or positive. A system that is always flat by 15:00 never pays the overnight part. That does not make intraday buying profitable. It removes one known headwind and leaves theta, spread, costs and the need for a real directional or volatility edge.

**What this means for design (applied to every proposal below):**
1. **"No trade" is the default.** A candidate must show net expectancy after the dated cost model and 2× slippage (docs/research/validation.md V10). The SEBI activity result says frequency is itself a risk factor.
2. **Buy premium only when it is conditionally cheap**, measured against a forecast of *intraday* realised volatility, never against yesterday's close-to-close volatility.
3. **Prefer setups with convex, trend-day payoffs** (momentum, breakouts out of noise), where one option can return several R, over setups that need many small wins.
4. **Never carry overnight or over a weekend** (already OD-002); Jones & Shemesh (§3) show that option prices lose most over non-trading periods.
5. **Count every trial.** Each feature set, threshold and variant tried goes into the experiment registry, and the Deflated Sharpe (V9) uses the full count.
6. **Assume the counterparty is a well-capitalised algorithm.** Use limit orders, small chase, no market orders (already docs/architecture/execution-engine.md), and avoid the expiry-day afternoon windows that SEBI's Jane Street order describes (§9).
7. **At ₹10k nothing should trade.** Every draft spec states its approximate minimum NAV.

## 3. The volatility risk premium by time of day, and when buying volatility is cheap

**Sources**
- Muravyev & Ni, ["Why do option returns change sign from day to night?"](https://www.sciencedirect.com/science/article/abs/pii/S0304405X19302193) (J. Financial Economics 136(1), 2020). [Internet appendix](https://www.dmurav.com/MuravyevNi_WhyDoOptionReturnsChangeSign_IA.pdf).
- Bhat, Pandey & Rao, ["The asymmetry in day and night option returns: Evidence from an emerging market"](https://ideas.repec.org/a/wly/jfutmk/v44y2024i8p1320-1337.html) (J. Futures Markets 44(8), 2024). **NIFTY options.**
- Jones & Shemesh, ["Option Mispricing around Nontrading Periods"](https://ideas.repec.org/a/bla/jfinan/v73y2018i2p861-900.html) (J. Finance 73(2), 2018).
- Goyal & Saretto, ["Cross-section of option returns and volatility"](https://ideas.repec.org/a/eee/jfinec/v94y2009i2p310-326.html) (JFE 94(2), 2009).
- Corsi, ["A Simple Approximate Long-Memory Model of Realized Volatility"](https://academic.oup.com/jfec/article-abstract/7/2/174/856522) (J. Financial Econometrics 7(2), 2009): the HAR-RV model.
- Jain, Varma & Agarwalla, ["Indian equity options: Smile, risk premiums, and efficiency"](https://ideas.repec.org/a/wly/jfutmk/v39y2019i2p150-163.html) (J. Futures Markets 2019).

**Core findings**
- Muravyev & Ni (S&P 500 index options, 2004–2013): delta-hedged returns average −0.7% a day, made of **about −1% close-to-open and +0.3% open-to-close**. The pattern holds across maturities and moneyness and is stronger for short-dated and OTM options. Their explanation: option prices behave as if volatility were the same day and night, while realised volatility is much higher intraday.
- Bhat, Pandey & Rao (NIFTY): short NIFTY option strategies earn positive, significant returns **overnight** and negative returns **intraday**; the asymmetry is robust but weaker on days with large jumps. "The variance risk premium earned by option sellers is mainly a reward for overnight risk."
- Jones & Shemesh: option returns are lower over non-trading periods, especially weekends.
- Goyal & Saretto: options on stocks whose historical volatility is high relative to implied volatility earn higher subsequent returns.
- Corsi: realised variance is forecast well by a sum of daily, weekly and monthly realised-variance terms (the HAR model).
- Jain, Varma & Agarwalla: in India, implied volatility has incremental power to forecast future volatility, and the market looks "microefficient"; the IV risk premium for single-stock options looks higher than elsewhere.

**Does it apply to us?** Yes, and it is the most important structural fact in this document. We are *always* intraday, so we hold options only in the part of the day where, on this evidence, buyers are not systematically paying the premium. That lets the system ask a sharper question than "is IV below RV?" (H06): **is today's intraday variance likely to exceed what the option price implies for today's session?** It is a timing filter for buying volatility, never a reason to sell it.

**Data.** Index 1-minute (for 5-minute realised variance, open-to-close, and the morning's variance so far): lake **yes**. Nearest-weekly ATM IV and premiums (code 1): lake **yes**. Next-week IV (code 2) for a term-structure cross-check: lake **yes**.

**Cost sensitivity: high.** A long ATM straddle is two legs: about ₹80 of brokerage plus statutory charges a round trip, and two spreads. With ATM premiums around ₹100 a leg, the straddle costs ≈ ₹13,000 a lot pair and needs a realised move several times the costs; the mechanism (+0.3% a day hedged for SPX) is small next to a 1–2-tick spread on each leg. The single-leg variant halves the costs but adds a directional bet.

**Proposal → H19 `S-DAYVOL-001` (draft spec).** At 09:45, forecast today's open-to-close realised variance with a HAR model (yesterday, 5-day and 22-day open-to-close RV from 5-minute returns) plus the 09:15–09:45 variance. Compare it with the nearest-weekly ATM IV's one-session variance (IV² / 252, an ASSUMED day count). If the ratio is ≥ 1.25 and next-week IV is not below this week's by more than a set margin (no inverted short end), buy an ATM straddle between 09:45 and 11:00 and exit by 14:30. Falsified if straddles bought on high-ratio days do not beat straddles bought at the same minute on low-ratio days, or if the lake does not reproduce a non-negative intraday delta-hedged ATM return on NIFTY. It needs a 2-lot straddle allowance like OD-013's (S-VIXSTR-001 and S-IVRV-001 only today), so it is blocked in canary until the owner decides.

## 4. Market intraday momentum and the hedging-demand explanation

**Sources**
- Gao, Han, Li & Zhou, ["Market intraday momentum"](https://ideas.repec.org/a/eee/jfinec/v129y2018i2p394-414.html) (JFE 129(2), 2018; [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351)).
- Baltussen, Da, Lammers & Martens, ["Hedging demand and market intraday momentum"](https://ideas.repec.org/a/eee/jfinec/v142y2021i1p377-403.html) (JFE 142(1), 2021; [SSRN 3760365](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365)). Baltussen and Martens are at Robeco/Erasmus; this is the Robeco contribution.
- Li, Sakkas & Urquhart, ["Intraday time series momentum: Global evidence and links to market characteristics"](https://ideas.repec.org/a/eee/finmar/v57y2022ics138641812100001x.html) (J. Financial Markets 57, 2022).

**Core findings**
- Gao et al. (SPY, 1993–2013): the first half-hour return, measured from the previous close, predicts the **last** half-hour return (in-sample R² 1.6%, out-of-sample 1.4%; 2.6% and 2.0% when the twelfth half-hour is added). It is stronger on high-volatility, high-volume, recession and macro-news days.
- Baltussen et al. (60+ futures, 1974–2020): the return from the previous close to the last 30 minutes predicts the last 30 minutes "everywhere", and reverts over the next days. They link it to **hedging of short gamma** by option market makers and leveraged ETFs: per the Alpha Architect summary of their Table 7, momentum is much stronger on negative net-gamma days and not significant on positive ones.
- Li, Sakkas & Urquhart: significant intraday time-series momentum in 12 of 16 developed markets.
- **No peer-reviewed NIFTY test of this effect turned up in my searches.** It has to be tested on the lake, not assumed.

**Does it apply?** Partly. The classic window is the exchange's last half-hour, which is **outside** our 09:15–15:00 window (OD-002), and H12 (`S-TOD-001`) already re-windows the first-hour version. What does transfer is the mechanism: if hedging flows build through the afternoon, the move from the previous close to about 13:30 may predict 13:30–14:45. That is an in-window test, and it pays only if the remaining move is large enough to beat an afternoon option's theta.

**Data.** Index 1-minute and previous close: lake **yes**. A gamma-sign proxy (§6) would condition it.

**Cost sensitivity: medium-high.** Afternoon ATM premiums are lower (about ₹50–110), but a 60–75-minute hold near expiry loses theta quickly; the predicted move (fractions of a percent of the index) must exceed theta plus ≈ 3–6% of the premium in costs.

**Proposal → H21 `S-IMOM-001` (draft spec).** If the return from the previous close to 13:30 is at least 0.4% in absolute value and the day's 09:15–13:30 realised volatility is in the top half of its 20-day range, buy the option in that direction between 13:30 and 13:50; exit by 14:45. It is H12's sibling; both count in the trial total.

## 5. Breaking out of the "noise area" (practitioner-academic intraday momentum)

**Sources**
- Zarattini, Aziz & Barbon, ["Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172) (SSRN 4824172; [Swiss Finance Institute WP 24-97](https://www.sfi.ch/en/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy)).
- Zarattini, Barbon & Aziz, ["A Profitable Day Trading Strategy For The U.S. Equity Market"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284) (SSRN 4729284): the opening-range breakout on "stocks in play".
- Crabel, *Day Trading with Short Term Price Patterns and Opening Range Breakout* (Traders Press, 1990; [Google Books record](https://books.google.com/books/about/Day_Trading_with_Short_Term_Price_Patter.html?id=xpgbAAAACAAJ)): the documented practitioner origin of the opening-range breakout and the narrow-range (NR4/NR7) contraction filters.

**Core findings**
- SPY noise area: at each minute, the band is the open (adjusted for the overnight gap) ± the average absolute move from the open to that minute over the last 14 sessions. Positions follow a close outside the band, checked every half hour, with dynamic trailing stops. The SFI abstract reports 19.6% a year net of costs and a Sharpe of 1.33 for 2007–early 2024, and the authors test whether dealers' estimated gamma imbalance predicts the strategy's profitability.
- Stocks-in-play ORB: a plain 5-minute ORB applied broadly does poorly; profits come from restricting to stocks with unusually high relative volume.
- Crabel: range contraction tends to precede expansion, and the early break of the opening range carries direction (a practitioner claim, documented with historical tables; not peer-reviewed).

**Does it apply?** Yes, closely. It is index-level, intraday, trend-following and needs only 1-minute index bars. Buying an option on a confirmed escape from the normal intraday range is the convex payoff a long-premium book wants, and the time-of-day band adapts to volatility, which a fixed ORB does not. Caution: published Sharpe ratios are for the underlying ETF, not for options that pay theta and spread; the option version needs a bigger move.
- The stocks-in-play result supports H01b's idea of an abnormal-participation filter, but H01b's own long-window pipeline check (30-Aug-2022..11-Sep-2024: 198 trades, net −₹59,523 at a HYPOTHETICAL ₹1L) argues against more ORB variants until something conditions it. That is what H23 (§7) tests.

**Data.** Index 1-minute with 14+ sessions of history: lake **yes**.

**Cost sensitivity: medium.** Holds of one to four hours on trend days make the ≈ ₹50–70 round trip small against targets of tens of premium points; the danger is whipsaw around the band, so a check every 30 minutes (not every minute) and one entry a day are pre-registered.

**Proposal → H20 `S-NOISE-001` (draft spec).** At each half-hour mark from 10:00 to 13:30, a close above the upper band buys a CE and a close below the lower band buys a PE; exit when a half-hour close returns inside the band, at a premium stop, or at 14:45.

## 6. Dealer gamma exposure (GEX) and options-flow effects

**Sources**
- Barbon & Buraschi, ["Gamma Fragility"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3725454) (SSRN 3725454).
- Dim, Eraker & Vilkov, ["0DTEs: Trading, Gamma Risk and Volatility Propagation"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4692190) (SSRN 4692190).
- Ni, Pearson, Poteshman & White, ["Does Option Trading Have a Pervasive Impact on Underlying Stock Prices?"](https://ideas.repec.org/a/oup/rfinst/v34y2021i4p1952-1986..html) (RFS 34(4), 2021).
- Baltussen et al. (§4): intraday momentum concentrated on negative net-gamma days.
- Practitioner: SqueezeMetrics, ["Gamma Exposure (GEX)" white paper](https://squeezemetrics.com/download/white_paper.pdf); Cboe, ["Much Ado About 0DTEs"](https://www.cboe.com/insights/posts/volatility-insights-evaluating-the-market-impact-of-spx-0-dte-options); Adams, Fontaine & Ornthanalai, ["The Market for 0DTE: The Role of Liquidity Providers in Volatility Attenuation"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4881008) (SSRN 4881008). JPMorgan's Mar-2023 warning that 0DTE hedging could amplify a sharp intraday fall is cited **only through press reporting** ([CNBC, 6-Mar-2023](https://www.cnbc.com/2023/03/06/the-explosion-of-risky-zero-day-options-could-worsen-market-shocks-jpmorgan-says.html)); I could not reach the note itself.

**Core findings**
- Barbon & Buraschi: when dealers' aggregate gamma is negative, their delta-hedging trades with the move (momentum, higher volatility); when it is positive, against it (reversal, lower volatility).
- Dim, Eraker & Vilkov (S&P 500 0DTE): market makers' net gamma is usually positive and predicts lower intraday volatility; positive gamma goes with stronger reversals, negative with stronger momentum.
- Ni et al.: option market makers' hedge rebalancing affects underlying volatility and the chance of large moves.
- SqueezeMetrics: GEX is Σ gamma × open interest across strikes and expiries under an assumed dealer side; positive GEX damps, negative GEX amplifies. Cboe and Adams et al. find that SPX 0DTE liquidity providers' hedging has, on balance, damped rather than amplified intraday volatility.

**Does it apply?** The mechanism does; the US sign convention may not. US GEX assumes customers buy puts and sell calls, so dealers are long calls and short puts. In NIFTY, SEBI's data show individuals losing heavily in options while algorithmic prop traders and FPIs profit, which is consistent with, but does not prove, non-individuals being **net short** options (short gamma) much of the time. If so, large gamma near spot should mean *more* momentum, the opposite of the usual US reading. **The sign must be measured, not assumed.**

**Data.** Strike-level OI and IV for code 1 (ATM±10) and code 2 (ATM±3): lake **yes**, so a partial-chain gamma-concentration measure can be computed every minute. Who holds the positions (the sign): **no**; the NSE participant-wise OI report would give a daily aggregate sign (UNVERIFIED, not fetched).

**Cost sensitivity: medium.** It is a filter on momentum entries, so it lowers frequency and cost; the risk is a mis-signed proxy that filters the wrong days.

**Proposals**
- **H24 `F-GEX-001` (feature).** Compute, every 15 minutes, G = Σ over the lake's strikes of Black-Scholes gamma (Dhan IV) × OI × lot × spot² × 1%. Pre-registered first test: does the 60-day percentile of G at 10:15 predict the 10:15–14:45 realised volatility and the sign of the first-hour/rest-of-day return autocorrelation? If high G predicts **higher** RV and momentum, non-individuals are acting net short gamma and the sign convention is "India-short"; if lower RV and reversal, "US-long".
- **H22 `S-GEXMO-001` (draft spec).** Under the pre-registered "India-short" reading: on days when G is in the top tercile of its 60 days at 10:15, buy the first 15-minute close beyond the 09:15–10:15 range, in its direction. If H24 finds the opposite sign, H22 is falsified and any reversion variant is a *new* hypothesis with its own trial count.

## 7. Machine learning done safely: triple-barrier labels, meta-labelling, purged CV, Deflated Sharpe, PBO

**Sources**
- López de Prado, [*Advances in Financial Machine Learning*](https://www.wiley.com/en-ie/advances-in-financial-machine-learning-p-9781119482086) (Wiley, 2018): triple-barrier labelling, meta-labelling, purged k-fold cross-validation with an embargo, combinatorial purged CV, sample uniqueness weights.
- Joubert, ["Meta-Labeling: Theory and Framework"](https://www.pm-research.com/content/iijjfds/4/3/31) (J. Financial Data Science 4(3), 2022; [SSRN 4032018](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4032018)).
- Bailey & López de Prado, ["The Deflated Sharpe Ratio"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) (J. Portfolio Management 40(5), 2014).
- Bailey, Borwein, López de Prado & Zhu, ["The Probability of Backtest Overfitting"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253) (J. Computational Finance 20(4), 2017): combinatorially symmetric cross-validation (CSCV).

**Core findings**
- Triple-barrier labels judge a trade by which comes first: the profit barrier, the stop barrier or the time barrier. That is how our trades actually exit.
- Meta-labelling separates *direction* (the primary rule) from *whether to take it and how big* (a secondary classifier trained on whether the primary's trades hit the profit barrier). It improves precision; it cannot create recall the primary lacks.
- Overlapping labels leak information across ordinary k-fold splits; purging overlapping training samples and an embargo after each test fold fix that.
- The Deflated Sharpe corrects the best Sharpe for the number of trials and non-normal returns; PBO estimates the chance that the in-sample winner is below median out of sample.

**Does it apply?** Yes, as process rather than as an edge. The validation toolkit already has the DSR (V9); it has no PBO and no purged CV. Meta-labelling is the natural way to ask whether any *conditions* rescue a primary signal with poor standalone results, such as H01b.

**Data.** Primary-signal trade lists from the backtester plus features from the lake (VIX, IV term slope, G, gap, time, DTE, K-11 tags): **yes**.

**Cost sensitivity.** It lowers frequency (it can only veto), so costs per kept trade are unchanged; the hidden cost is multiple testing, so every feature set and threshold tried is counted.

**Proposals**
- **H23 `S-ORBML-001` (draft spec).** The primary is S-ORB-002 (H01b) unchanged. A pre-registered L2 logistic regression (fixed C = 1, ten named features, no tuning) trained walk-forward on triple-barrier labels with 5-fold purged CV and a 1-day embargo; take the trade only if p ≥ 0.55. Falsified unless it beats H01b's own OOS R and passes V9 with the full trial count.
- **Validation backlog (no code here):** add PBO via CSCV and purged/embargoed folds to `validation/`, so V9 is not the only multiple-testing control (docs/research/validation.md notes that White's RC and Hansen's SPA are not built).

## 8. Volatility targeting and fractional Kelly sizing

**Sources**
- Moreira & Muir, ["Volatility-Managed Portfolios"](https://ideas.repec.org/a/bla/jfinan/v72y2017i4p1611-1644.html) (J. Finance 72(4), 2017).
- Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert, ["The Impact of Volatility Targeting"](https://www.man.com/insights/the-impact-of-volatility-targeting) (Man Group; J. Portfolio Management 2018; [SSRN 3175538](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3175538)).
- Thorp, ["The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market"](https://gwern.net/doc/statistics/decision/2006-thorp.pdf) (2006, Handbook of Asset and Liability Management).

**Core findings**
- Moreira & Muir: scaling exposure by the inverse of recent realised variance raised Sharpe ratios for equity factors.
- Harvey et al. (Man Group): volatility targeting improves Sharpe ratios for equities and other risk assets, and reduces left-tail returns across asset classes.
- Thorp: Kelly maximises long-run log growth; betting a fraction of Kelly (often half) gives up a little growth for much less drawdown, and errors in the estimated edge make full Kelly dangerous.

**Does it apply?** Only in a narrow way. A long option's loss is already capped at the stop, and v1 trades at most one lot, so there is little size to scale. What carries over:
- **Kelly says the edge must be estimated first.** With no validated edge, the Kelly fraction is zero, which agrees with "no trade by default".
- Once trades exist at ₹1–2L, a **meta-label probability** (H23) can set the per-trade risk budget as a fraction of the 2% cap, for example a quarter-Kelly from the calibrated win probability and payoff ratio, never above 2%.
- Volatility targeting maps onto the **daily** risk budget: in high-RV regimes a fixed-premium stop is hit more by noise, so the allocator could cut the day's budget rather than widen stops.

**Data.** Trade-level outcomes (none validated yet) and daily RV (lake **yes**).

**Cost sensitivity.** None directly; smaller risk budgets can push a lot below the 2% fit and so remove trades, which lowers costs.

**Proposal → H27 `F-VOLKELLY-001` (feature / allocator v2 rule, no spec).** The allocator already halves budgets after losses (docs/research/strategy-hypotheses.md). Add, when evidence exists: a budget multiplier min(1, target RV / 20-day RV) and a quarter-Kelly cap from the calibrated meta-probability. Both can only lower risk.


## 9. Expiry days: hedging, pinning and SEBI's Jane Street order

**Sources**
- SEBI, ["Interim Order in the matter of Index manipulation by Jane Street Group"](https://www.sebi.gov.in/enforcement/orders/jul-2025/interim-order-in-the-matter-of-index-manipulation-by-jane-street-group_95040.html) (3-Jul-2025; [order PDF](https://www.sebi.gov.in/sebi_data/attachdocs/jul-2025/1751584518593.pdf)). These are SEBI's **prima facie** findings in an interim order, contested by the firm; they are not a final ruling.
- Vipul, ["Futures and options expiration-day effects: The Indian evidence"](https://onlinelibrary.wiley.com/doi/10.1002/fut.20178) (J. Futures Markets 25(11), 2005).

**Core findings**
- SEBI's order says that on weekly expiry days index-options volume, in cash-equivalent terms, is several times the combined cash and futures volume, so an entity that moves the index through the thinner cash and futures markets can profit on a much larger options book. It describes an "Intraday Index Manipulation" pattern on BANKNIFTY expiry days (buy constituents and futures in the morning, sell in the afternoon, with large options positions on the other side) and an "Extended Marking the Close" pattern, which it says was also seen in **NIFTY** index options on expiry days in May 2025, including large NIFTY futures and constituent trades near expiry on 15-May-2025.
- Vipul: around expiry, underlying shares were marginally depressed the day before and strengthened the day after, with abnormal volume; an older single-stock result.

**Does it apply?** As a hazard. An intraday option buyer on an expiry afternoon may be trading against flows that are moving the index on purpose. H07 (`S-EXP0-001`) is the only shipped spec that targets expiry afternoons.

**Data.** Expiry calendar (in the repo) and index/options 1-minute (lake **yes**).

**Proposal → H28 `F-EXPHAZ-001` (feature / filter).** Measure on the lake whether expiry-day afternoons (13:00–15:00) show more intraday reversal of the morning's move than non-expiry days. If they do, prohibit momentum entries after 13:00 on expiry days for every spec except one built for that window, and widen H07's falsification test to include it.

## 10. The volatility term structure as a regime signal

**Sources**
- Johnson, ["Risk Premia and the VIX Term Structure"](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/risk-premia-and-the-vix-term-structure/56572D1F060448571BD8F597C732D9C3) (JFQA 52(6), 2017).
- NSE working papers (listed on [NSE's working-paper page](https://www.nseindia.com/static/research/working-papers)): No. 87, Thenmozhi & Chandra, "India Volatility Index (India VIX) and Risk Management in the Indian Stock Market" (2013); No. 68, Bagchi, "Some Preliminary Examination of Predictive Ability of India VIX" (2011). I verified the titles and listing, not the full texts; secondary summaries of No. 87 describe a negative, asymmetric India VIX–NIFTY return relation.

**Core findings**
- Johnson: the shape of the VIX term structure mainly reflects variance risk premia, not expected VIX changes; its second principal component (the slope) predicts excess returns of variance swaps, VIX futures and S&P 500 straddles.
- India VIX moves against NIFTY, and more so in falls.

**Does it apply?** Yes, as a filter. India has one VIX tenor and no liquid VIX futures curve, but the lake gives **this-week vs next-week ATM IV every minute**, plus the 30-day India VIX. A steep upward slope (front cheap, back rich) is the "normal" state in which premium is richest further out; a flat or inverted front (front IV above next week's) marks stress, when realised moves tend to be large and long gamma can pay. The sign of the effect on intraday long-option returns must be measured, not borrowed: Johnson's evidence is for multi-day holding periods.

**Data.** Code 1 and code 2 ATM IV, India VIX: lake **yes**. DTE differences make the raw slope jump around expiries, so compare total variance per day (IV² × DTE differences), not raw IV.

**Cost sensitivity: low.** It is a gate on other specs.

**Proposal → H25 `F-TERM-001` (feature).** Slope_t = per-day forward variance between this week's and next week's ATM expiry, versus this week's per-day variance. Pre-registered test: do the long-premium specs' trades (and the H19 straddle) earn more when the front is flat or inverted than when the slope is in its top tercile? H19 already uses a mild version (no strongly inverted short end, as an anti-panic guard).

## 11. Regime-switching models (HMM, Markov switching)

**Sources**
- Hamilton, ["A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle"](https://www.econometricsociety.org/publications/econometrica/1989/03/01/new-approach-economic-analysis-nonstationary-time-series-and) (Econometrica 57(2), 1989).
- Kritzman, Page & Turkington, ["Regime Shifts: Implications for Dynamic Strategies"](https://rpc.cfainstitute.org/research/financial-analysts-journal/2012/regime-shifts-implications-for-dynamic-strategies-corrected) (Financial Analysts Journal 68(3), 2012; State Street Associates).
- BlackRock, ["How Machine Learning is Enhancing Macro Investing"](https://www.blackrock.com/institutions/en-us/insights/thought-leadership/machine-learning-macro-investing) (practitioner note on regime-resilient training; tangential: macro horizon).

**Core findings**
- Hamilton: model parameters as functions of a hidden Markov state, with a filter giving the probability of each state in real time.
- Kritzman et al.: two-state hidden Markov models on turbulence, inflation and growth, used to shift allocations when the "event" regime probability rises, improved downside outcomes out of sample.

**Does it apply?** Yes, as a classifier candidate. K-11 (`RC-2026-10-02.1`) is rule-based, UNVALIDATED and NO_EDGE for live money. A two- or three-state Gaussian HMM on **daily** features (open-to-close RV, overnight gap, VIX change, IV term slope, G percentile) gives a filtered state probability each morning, which is the kind of slow regime label that should gate *which* intraday family is allowed (momentum vs reversion). Intraday HMMs on minute bars are prone to label switching and overfitting; not proposed.

**Data.** Daily aggregates of the lake: **yes** (≈ 1,000 clean sessions; a 3-state model with 5 features has about 50 parameters, so keep it small).

**Cost sensitivity: none directly.**

**Proposal → H26 `F-HMMREG-001` (classifier candidate).** Fit walk-forward (expanding window, refit monthly), use filtered (never smoothed) probabilities, and judge it by one thing: does gating H19–H23 and the shipped library by the HMM state raise OOS expectancy per trade versus gating by K-11, with the trial count charged? It would ship as a second classifier version, never as a silent replacement.

## 12. Overnight gaps and the open

**Sources**
- Lou, Polk & Skouras, ["A tug of war: Overnight versus intraday expected returns"](https://ideas.repec.org/a/eee/jfinec/v134y2019i1p192-213.html) (JFE 134(1), 2019; [LSE PDF](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf)).
- Berkman, Koch, Tuttle & Zhang, ["Paying Attention: Overnight Returns and the Hidden Cost of Buying at the Open"](https://ideas.repec.org/a/cup/jfinqa/v47y2012i04p715-741_00.html) (JFQA 47(4), 2012).

**Core findings.** Momentum profits accrue overnight and most other premia intraday, with offsetting reversals (Lou et al.). Attention-driven retail buying lifts opening prices and tends to reverse during the day (Berkman et al., US stocks).

**Does it apply?** Weakly, through the gap specs (H08a/H08b). These are cross-sectional stock results; for an index the analogue is "a large overnight gap on a high-attention day is partly reversed by the close". It is a conditioning variable for S-GAPFADE-001 vs S-GAPGO-001, not a new strategy.

**Data.** Index 1-minute and previous close: lake **yes**. An "attention" proxy (the previous day's absolute return and near-the-money option volume) can be built from the lake.

**Cost sensitivity: medium** (as the gap specs).

**Proposal → H29 `F-GAPATTN-001` (feature).** Split historical gap-fade and gap-go trades by the previous day's attention proxy; keep the split only if it survives the trial count.

## 13. Time-series momentum (multi-day trend)

**Source.** Moskowitz, Ooi & Pedersen, ["Time Series Momentum"](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum) (JFE 104(2), 2012; AQR).

**Core finding.** Across 58 futures and forwards, the past 12 months' excess return predicts the next month's, with partial reversal after a year.

**Does it apply?** Barely. The horizon is months; our holds are minutes to hours. At most it is a directional prior (favour CE breakouts when NIFTY's 3–12-month trend is up). The daily drift it implies is tiny next to intraday noise and option theta.

**Proposal → H30 `F-TSMOM-001` (feature, low priority).** Report trend-aligned vs counter-trend results for every directional spec; adopt only as a tie-breaker.

## 14. Order-flow imbalance (OFI) and option order flow

**Sources**
- Cont, Kukanov & Stoikov, ["The Price Impact of Order Book Events"](https://ideas.repec.org/a/oup/jfinec/v12y2013i1p47-88.html) (J. Financial Econometrics 12(1), 2014; [arXiv 1011.6402](https://arxiv.org/abs/1011.6402)).
- Chakrabarti & Kotha, ["Options Order Flow, Volatility Demand and Variance Risk Premium"](https://ideas.repec.org/a/mfj/journl/v21y2017i2p49-90.html) (Multinational Finance Journal 21(2), 2017). **NIFTY options.**
- Muravyev, ["Order Flow and Expected Option Returns"](https://ideas.repec.org/a/bla/jfinan/v71y2016i2p673-708.html) (J. Finance 71(2), 2016).

**Core findings.** Short-horizon mid-price changes are close to linear in OFI, with impact inversely proportional to depth (Cont et al.). In NIFTY options, vega-weighted order imbalance moves the variance risk premium, most for the most expensive options (Chakrabarti & Kotha). Inventory risk from order flow is priced in option returns (Muravyev).

**Does it apply?** Not yet. OFI needs best bid/ask sizes or signed trades, and the lake has 1-minute OHLCV only. At our latency, a seconds-horizon OFI edge is also close to the latency arbitrage the directive excludes.

**Proposal → H31 `F-OFI-001` (deferred, data first).** Start recording live top-of-book and depth snapshots for ATM±3 strikes and the near future during paper trading (data-only feed, OD-011), then test minute-level OFI as an *entry-timing* filter (enter a breakout only when OFI agrees), never as a standalone scalper.


## 15. Ranked table: expected applicability to long-only intraday NIFTY options

Ranking weighs four things: does the mechanism survive the move from the source's market to NIFTY intraday; can the lake test it now; how much of a likely edge would costs and theta eat; and does it fit the mandate without an owner decision. **The ranking is a prior for ordering the work, not evidence.**

| Rank | Idea (ID) | Key sources | Lake can test now? | Cost sensitivity | Mandate fit | Output |
|---:|---|---|---|---|---|---|
| 1 | Day–night VRP: buy volatility intraday only, when forecast intraday variance beats the price (H19 `S-DAYVOL-001`) | Muravyev & Ni 2020; Bhat, Pandey & Rao 2024 (NIFTY); Goyal & Saretto 2009; Corsi 2009 | Yes | High (2 legs) | Needs a 2-lot allowance (owner) | **Draft spec** |
| 2 | Noise-area breakout momentum (H20 `S-NOISE-001`) | Zarattini, Aziz & Barbon (SSRN 4824172); Crabel 1990 | Yes | Medium | Fits | **Draft spec** |
| 3 | In-window late-session intraday momentum (H21 `S-IMOM-001`) | Gao et al. 2018; Baltussen et al. 2021; Li, Sakkas & Urquhart 2022 | Yes | Medium-high (afternoon theta) | Fits; the classic last half-hour is outside OD-002 | **Draft spec** |
| 4 | Meta-labelling + purged CV + DSR/PBO (H23 `S-ORBML-001`; validation backlog) | López de Prado 2018; Joubert 2022; Bailey & López de Prado 2014; Bailey et al. 2017 | Yes | Neutral (veto only) | Fits | **Draft spec** + validation backlog |
| 5 | Dealer gamma concentration and its sign (H22 `S-GEXMO-001`, H24 `F-GEX-001`) | Barbon & Buraschi; Dim, Eraker & Vilkov; Ni et al. 2021; SqueezeMetrics; Cboe; Adams et al. | Partly (partial chain, no holder sign) | Medium | Fits | **Draft spec** + feature |
| 6 | Volatility-scaled budgets and fractional Kelly (H27) | Moreira & Muir 2017; Harvey et al. (Man Group) 2018; Thorp 2006 | Needs validated trades first | None (lowers risk) | Fits (can only lower risk) | Allocator rule (later) |
| 7 | Short-dated IV term structure as a gate (H25) | Johnson 2017; NSE WP 87 and 68 | Yes (code 1 vs code 2, VIX) | Low (filter) | Fits | Feature |
| 8 | HMM / Markov-switching regime classifier (H26) | Hamilton 1989; Kritzman, Page & Turkington 2012 | Yes (daily features) | None directly | Fits (new classifier version) | Classifier candidate |
| 9 | Expiry-afternoon hazard (H28) | SEBI Jane Street interim order 2025; Vipul 2005 | Yes | Lowers frequency | Fits | Filter |
| 10 | ORB only with abnormal participation | Zarattini, Barbon & Aziz (SSRN 4729284) | Yes (H01b exists) | Medium | Fits | Evidence for H01b; tested through H23 |
| 11 | Gap × attention (H29) | Berkman et al. 2012; Lou, Polk & Skouras 2019 | Yes | Medium | Fits | Feature for H08a/b |
| 12 | Time-series momentum alignment (H30) | Moskowitz, Ooi & Pedersen 2012 | Yes | Low | Fits | Feature (low priority) |
| 13 | Order-flow imbalance (H31) | Cont, Kukanov & Stoikov 2014; Chakrabarti & Kotha 2017; Muravyev 2016 | **No** (no depth/quotes) | Very high at short horizons | Near the excluded latency-arb zone | Deferred: record data first |

**Top 5–8 → draft specs.** Ranks 1–5 became draft specs H19–H23 (`specs/drafts/`). Ranks 6–8 are not trade rules (sizing, a gate, a classifier), so they are registered as features H24–H27 in docs/research/strategy-hypotheses.md rather than as StrategySpecs.

## 16. The draft specs at a glance

| Draft | Entry window | Exit by | Legs | Entries a day | Approx. min NAV | Key falsifier |
|---|---|---|---|---:|---|---|
| `S-DAYVOL-001` (H19) | 09:45–11:00 | 14:30 | BUY ATM CE + BUY ATM PE | 1 | ≈ ₹2L (and a 2-lot allowance) | High-ratio straddles do not beat low-ratio straddles at the same minute; or the lake's intraday delta-hedged ATM return is negative |
| `S-NOISE-001` (H20) | 10:00–13:30 (half-hour checks) | 14:45 | BUY CE or PE | 1 | ₹70k–1.3L | Move after entry not larger than a volatility-matched random entry |
| `S-IMOM-001` (H21) | 13:30–13:50 | 14:45 | BUY CE or PE | 1 | ₹45k–95k | Sign agreement on qualifying days not above non-qualifying days |
| `S-GEXMO-001` (H22) | 10:15–13:00 | 14:30 | BUY CE or PE | 1 | ₹70k–1.3L | High-G days do not follow through more; or H24 rejects the India-short sign |
| `S-ORBML-001` (H23) | 09:30–13:00 | 14:30 | BUY CE or PE | 1 | ₹70k–1.1L | Kept trades no better than all H01b trades; DSR ≤ 0.95 with the full trial count |

Common to all: RESEARCH status; every number an ASSUMED prior with a range for perturbation tests; LIMIT entries and SL-LIMIT stops; `EVENT_REGIME`, `NO_EDGE`, `ABNORMAL_MARKET` and `LOW_LIQUIDITY` prohibited; not event-certified; cost model `CM-2026-04-01` as the library uses; nothing eligible at ₹10k. **Before any backtest:** each needs a library plug-in, an entry-cap row in docs/research/strategy-hypotheses.md, and its move from `specs/drafts/` into `specs/` (the library test then requires both).

## 17. What was looked for and left out

- **Goldman Sachs, Citadel:** no public, verifiable trading research on these topics was found; nothing is cited.
- **Jane Street:** no verifiable talk on trading strategy was found. The only citation is SEBI's interim order (§9), which is the regulator's prima facie description, not the firm's own account.
- **JPMorgan:** the Mar-2023 0DTE note is cited only through CNBC's report; the note itself was not reachable.
- **BlackRock:** one tangential practitioner note (§11); no public intraday options research found.
- **India intraday momentum:** no peer-reviewed NIFTY test of the Gao/Baltussen effect turned up; H21 is therefore a first test, not a replication.
- **NSE working papers:** the list was checked for options and VIX work; Nos. 87 and 68 are cited by title. No NSE working paper on intraday NIFTY option strategies was found.

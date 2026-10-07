# 05 — Real-time Market-Data Requirements and Providers

## 5.1 Required real-time inputs (Phase 3+)

| Group | Fields | Frequency | Primary source | Fallback / cross-check | Needed by |
|---|---|---|---|---|---|
| NIFTY spot index | value, timestamp | tick / ≤1 s | Broker WebSocket | Second broker / vendor | all |
| NIFTY futures (current, next) | LTP, bid/ask, depth, volume, OI | tick | Broker WS | — | basis, regime |
| NIFTY options: current + next weekly expiry, ATM ±N strikes (N≈20, i.e. ±1,000 pts at 50-pt strikes; configurable) | LTP, **best bid/ask + sizes**, depth 5, volume, OI, last trade time | tick | Broker WS (full/depth mode) | Option-chain REST snapshot every 3–5 s (Dhan limit: 1 req / 3 s, S29) | signals, fills, DQ |
| India VIX | value | ≤1 s | Broker WS | NSE website (delayed) | regime |
| NIFTY 50 constituents (top ~20 by weight at least) | LTP, volume, bid/ask | tick | Broker WS | — | breadth, cross-sectional |
| Derived (computed in-house) | IV per strike (Black-76 on futures), Greeks, skew (25Δ RR, BF), term structure, PCR (OI & volume), max-OI strikes, realised vol (multiple windows), VWAP (spot proxy via futures), opening range, gap | per update | state_engine | Broker-provided Greeks as a sanity check | strategies, risk |
| Participant data | FII/DII cash & F&O participant-wise OI | EOD / next morning | NSE reports | Upstox FII/DII API (S27) | regime (daily) |
| Global / macro | GIFT Nifty, USD/INR, US futures, crude, US 10Y | 1 min or pre-market | Broker global instruments (Upstox, S27) or vendor | news | pre-market regime |
| Calendars | NSE holidays, expiry dates, RBI MPC dates, Fed FOMC, CPI/GDP releases, Union Budget, election results, index rebalances | daily refresh | NSE/RBI/Fed websites | manual owner review weekly | EVENT_REGIME |
| News | headline + timestamp + source | streaming / 1 min | Broker news API (Upstox News API, S27) or RSS | — | Market-Intel agent (advisory only; never a direct trading trigger until validated) |
| Exchange status | market status, CAS status, halts, circuit | event | Broker exchange-status API | — | ABNORMAL_MARKET_KILL |

## 5.2 Capacity check
- A NIFTY weekly chain at ATM ±20 strikes × CE/PE × 2 expiries ≈ 160 contracts, plus futures, VIX and 20–50 constituents: **≈ 250 subscriptions**. This fits within Zerodha's 3,000 per connection (secondary source) and is comparable at other brokers. Verify per broker in the bake-off.
- The tick recorder must persist **every tick received** (our own dataset starts accumulating from Phase 0). Estimated volume is tens to low hundreds of MB/day compressed (UNVERIFIED; measure).

## 5.3 Providers

| Provider | Offer | Cost (public) | Fit |
|---|---|---|---|
| Broker WebSocket (Dhan / Kotak / Zerodha / Upstox) | Real-time ticks, depth | Dhan data ₹499 + tax/month (S30); Zerodha ₹500/month incl. data (S23); Upstox/Kotak free (S26, S33) | **Primary.** Data from the broker we execute with = the same view as the fills |
| TrueData | Authorised vendor; Velocity plugin; Market Data API (real-time + historical, option chain + Greeks) | Velocity ₹1,439.83–₹2,795.83/month; **API pricing custom, approval required** (S38) | Independent second feed (Phase 5) |
| Global Datafeeds (GDFL) | Authorised vendor; 1-second L1 realtime, option-chain API, snapshot API | **Custom pricing** (S39) | Independent second feed (Phase 5) |
| NSE (paid real-time) | Direct exchange feeds | Institutional pricing (UNVERIFIED) | Not appropriate at our scale |

**Recommendation:** use the execution broker's feed plus a second broker's free feed (e.g. Upstox) as an **independent cross-check** for DQ (stale or crossed quotes, disagreement > X ticks). This costs nothing extra and satisfies §11. A paid vendor feed is deferred until capital justifies it (§18).

## 5.4 Data Quality rules (feed-level; details in 09 and 11)
- Staleness: no tick for a subscribed ATM ±3 strike for > 3 s in normal market → strike marked stale. Stale ATM → entries disabled.
- Crossed or locked book (bid ≥ ask), zero or negative prices, bid/ask outside the exchange price band → quote rejected.
- Spread > k × rolling median spread for that strike/time-of-day → LOW_LIQUIDITY flag.
- Exchange timestamp versus local receive-time drift > 1 s → timestamp-drift flag. Drift > 3 s sustained → DATA_QUALITY_KILL.
- WebSocket heartbeat missed or sequence gap → reconnect with backoff. Snapshot resync via REST before re-enabling entries.
- Duplicate ticks are de-duplicated by (token, exchange ts, price, qty, volume).
- Instrument master: lot size, tick size and expiry list are diffed against yesterday. Any change → blocking alert until acknowledged.

## 5.5 Regime classifier v0 (K-11, built 2-Oct-2026, UNVALIDATED)

Code: `src/project100c/regime/`. Config: `configs/regime/classifier.toml` (version `RC-2026-10-02.1`, every threshold ASSUMED) and the event calendar `configs/calendar/events.yaml` (`EV-2026-10-02.2`; schema `schemas/events.schema.json`; loader `calendar/events.py`). Every event entry carries its official source URL and a `verified` flag, and only verified entries reach the classifier (`EventBook.to_calendar`). It is seeded with RBI MPC decision days for 2025-26 and 2026-27 (rbi.org.in schedules and resolutions) and the 2026-27 Union Budget day (indiabudget.gov.in). NSE circulars returned HTTP 403 from this host and were skipped. The classifier is deterministic: the same bars, VIX, calendar and config version give the same labels. It reads closed 1-minute NIFTY index bars, India VIX and, when present, futures volume. Each label is stamped at the bar's **end**, when it becomes usable.

**Taxonomy (one label per dimension per bar, plus day conditions):**

| Dimension | Labels | Spec tag | How it is decided |
|---|---|---|---|
| Trend | UP / DOWN / RANGE | TRENDING_UP / TRENDING_DOWN / RANGE_BOUND | Majority of three voters: Wilder ADX(14) with +DI/−DI direction (enter 25, exit 18); the 30-bar least-squares slope in ATR units per bar (enter 0.06, exit 0.03); the deviation from session VWAP in sigma (enter 1.0, exit 0.5). VWAP is volume-weighted when futures volume is supplied, otherwise an equal-weight typical-price proxy |
| Volatility | COMPRESSION / NORMAL / EXPANSION | VOLATILITY_COMPRESSION / VOLATILITY_NORMAL / VOLATILITY_EXPANSION | Majority of three voters: 30-bar realised vol (9% / 18% annualised); India VIX level (12 / 17), overridden to EXPANSION by a ≥ 6% rise over 30 bars; the percentile of the 30-bar range against up to 5 sessions of history (20 / 80; abstains until 30 windows exist). A 10% band keeps the current state |
| Gap | GAP_UP_LARGE / GAP_UP / FLAT / GAP_DOWN / GAP_DOWN_LARGE / UNKNOWN | GAP_REGIME (any non-flat, known gap) | The first bar's open against the previous close: flat < 0.25%, large ≥ 0.8%. No previous close → UNKNOWN |
| Opening character | OPENING_DRIVE / MEAN_REVERSION / UNDETERMINED | OPENING_DRIVE / OPENING_REVERSION | Decided **once**, at 09:45: a drive closes in the top (or bottom) 30% of the opening range on the side of the open, with at most 1 cross of the open; a reversion has ≥ 3 crosses, a close in the middle 35%, or a gap more than half filled |
| Day conditions | expiry, event, abnormal | EXPIRY_REGIME / EVENT_REGIME / ABNORMAL_MARKET | Expiry from `ExpiryCalendar`; events from the event calendar; abnormal (sticky for the rest of the session) on a ≥ 3% index move from the previous close, a ≥ 0.8% one-minute move, or VIX ≥ 30 |

**Stability.** Hysteresis on every threshold (the exit level is easier to hold than the entry level), ties keep the current label, and a changed label is adopted only after it wins the vote on 5 consecutive bars (trend) or 3 (volatility). On a seeded synthetic range day the trend flips fell from 33 (no confirmation) to 7. The first 15 bars are warm-up; their only spec tag is NO_EDGE.

**Classifier agreement.** Each label carries the share of non-abstaining voters that agree with the adopted labels. It is a classifier-agreement score, **not a confidence or a probability**, and the JSON says so.

**Validation status.** The config is `UNVALIDATED`. Per docs/research/validation.md §13.5, an unvalidated classifier means NO_EDGE in live and canary: the Governor's regime gate (docs/risk/risk-engine.md) turns every unvalidated label into NO_EDGE for CANARY/PRODUCTION. Research and SIMULATED runs see the labels as they are, labelled UNVALIDATED. Validation needs real history (OOS confusion against ex-post labels, flips per day, gated-vs-ungated economic value), which waits for the Dhan pull.

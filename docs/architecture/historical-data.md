# 06 — Historical-Data Requirements

## 6.1 What research needs
| Dataset | Granularity | Depth | Why |
|---|---|---|---|
| NIFTY options, all strikes within ±10% of spot, weekly + monthly | **1-minute OHLCV + OI** minimum; **1-second LTP** and **bid/ask** preferred | ≥ 3–5 years, *and explicitly segmented by regime/rule era* (several lot-size regimes, e.g. 75→65 from Jan-2026; earlier values taken from point-in-time contract data, never assumed; weekly expiry Thursday→Tuesday 1-Sep-2025; post-Nov-2024 rules; STT changes; 15:40 close from 3-Aug-2026) | Signal research, fill modelling |
| NIFTY futures (continuous + individual) | 1-min + tick where available | same | basis, VWAP proxy, regime |
| NIFTY spot, India VIX | 1-min | ≥ 5 years | regime, RV/IV |
| NIFTY 50 constituents | 1-min | ≥ 3 years | breadth, cross-sectional signals |
| EOD F&O bhavcopy (UDiFF) | daily | as far back as available | settlement prices, OI truth, contract list (survivorship) |
| Instrument master snapshots | daily | from the start of our own collection | point-in-time contract universe, lot sizes |
| Participant-wise OI, FII/DII | daily | ≥ 3 years | regime features |
| Event calendar (RBI, Fed, CPI, Budget, elections, expiry, holidays) | event | ≥ 5 years | EVENT_REGIME labelling, contamination tests |
| **Our own recorded ticks + order book** | tick | from Phase 0 onward | ground-truth spreads/slippage; the most valuable dataset we will own |

## 6.2 Sources and gaps

| Source | Coverage | Cost | Known limitation |
|---|---|---|---|
| **Dhan expired-options API** | 5 years, 1/5/15/25/60-min, ATM ±10 (index near expiry) / ±3 otherwise, OHLC + IV + OI + volume + spot, rolling by moneyness (S31) | Included in the Data API, ₹499 + tax/month (S30) | **No bid/ask.** Strikes are indexed relative to ATM, so the absolute strike must be reconstructed from the `strike` field. Deep OTM beyond ±10 is not covered. 30 days per call |
| **ICICI Breeze** | 3 years of **1-second** LTP incl. F&O (S35) | Free | 5,000 calls/day and 100/min → multi-week download job; LTP only (bid/ask UNVERIFIED) |
| Zerodha Kite historical | Live instruments only; **no expired options** (S25) | ₹500/month | Unsuitable as the primary backtest source |
| Upstox Plus expired instruments | Expired option contracts + candles (S27) | UNVERIFIED | Cross-check candidate |
| NSE UDiFF bhavcopy | EOD for all contracts (S41) | Free | EOD only. The format changed on 8-Jul-2024, so the parser must handle both |
| NSE Data & Analytics | Official tick-by-tick / snapshot history (S40) | On request (UNVERIFIED) | Best quality, cost unknown; evaluate at Phase 5 |
| TrueData / GDFL | Tick history typically short (days to a week) via API; longer archives by arrangement (S38, S39) | Custom | Not a primary backtest source |

**Critical gap:** no affordable source found gives **historical bid/ask quotes** for NIFTY options. Consequences:
1. Backtests on OHLC/LTP must use **conservative synthetic spreads**: time-of-day × moneyness × DTE × VIX spread tables, calibrated from our own recorded quotes as soon as Phase 0 collection starts. *Built 1-Oct-2026 (`configs/backtest/synthetic_spreads.toml`, version `SPREAD-ASSUMED-2026-10-01.1`): time-of-day × DTE × moneyness only. There is no VIX dimension yet. Every number is an ASSUMED placeholder, and the loader refuses any status other than ASSUMED until a D-10 calibration exists.*
2. Every strategy is re-validated on our own recorded quote data (paper/shadow) before canary. Backtest spread assumptions are then updated with measured values.
3. Our tick recorder should start in Phase 0, weeks before anything else is ready, because it is the only route to real quote history.

**Dhan implementation notes (D-06, 1-Oct-2026; S57–S60).** The client in `src/project100c/data/dhan/` is data-only: an endpoint allowlist (`charts/rollingoption`, `charts/intraday`, `charts/historical`, `profile`) plus a test that proves no order/portfolio endpoint is reachable. The token is read only from `DHAN_ACCESS_TOKEN` in the environment; a missing token raises `MissingCredentialError` before any network call. `DHAN_CLIENT_ID` comes from the environment or `configs/local/dhan.env` (gitignored). Our limits are set below Dhan's (4/s vs 5/s; 90k/day vs 100k/day). As noted above, Dhan data has **no bid/ask**.

**Verified on the first real pull (2-Oct-2026, D-06).** These facts come from real responses. `tests/fixtures/dhan/shape/` copies their shape (fields, types, nulls, timestamps, float quirks) with synthetic numbers, so no market data is committed.

- **Timestamps are bar starts.** F&O bars run 09:15–15:39 (385 a day since 3-Aug-2026; 375 before, 09:15–15:29). Index and India VIX bars run 09:15–15:29, sometimes with a 15:30 print (kept as a WARNING). Some 2021 index days include 09:01–15:59 out-of-session bars.
- **`expiryCode`:** 0 is rejected. **1 = the nearest expiry on or after the trade date, including the expiry day itself** (ATM settles to intrinsic or 0.05 with IV 0 at 15:39 on expiry). 2 = the next expiry, 3 = the one after. Dhan promises ATM±10 only for the nearest expiry and ±3 otherwise; code 2 at ATM+10 came back incomplete, so the plan uses ±3 for code 2.
- **Rolling options `toDate` is INCLUSIVE**, contrary to the docs: a 28-Sep..29-Sep request returns both days. The planner sends window end − 1 day. `requiredData` is ignored and every field is always returned; `pe` is null for CALL requests. IV is in percent (0 at expiry). Values arrive as floats with representation noise (DQ: VALUE_ROUNDED WARNING).
- **Intraday:** at most 90 days per call (DH-905 otherwise). A `toDate` with a time is inclusive. India VIX is IDX_I security id **21**. One-minute index and option history goes back to at least Oct-2020. The index carries a volume field (not traded volume).
- **`securityId`** is accepted both as a string (intraday) and as an integer (rolling).
- **Futures:** only **active** contracts have candles (Oct/Nov/Dec-2026 here, each from about three months before expiry). Expired contracts have no reachable security id, and continuous-futures requests return empty arrays. Empty results are HTTP 200 with empty arrays, not DH-907.
- **Errors:** DH-902 can arrive as HTTP 401 while `/profile` still shows the data plan Active, so the backfill preflight makes one tiny data call (`data_probe`). DH-911 (static IP) is a stop, not a retry. Gateway HTTP 504s happen under load and are retried.
- **Rate limits:** 5 requests/s and 100,000 a day, as documented. No 429 was seen, so the Retry-After format is still unobserved. Latency is 2–60 s per rolling call, so the backfill is latency-bound and runs 4 worker processes at 1 request/s each.


## 6.3 Data-lake layout
```
lake/
  raw/<source>/<dataset>/date=YYYY-MM-DD/part-*.parquet      # immutable, as received (+ original JSON gz)
  clean/<dataset>/underlying=NIFTY/expiry=YYYY-MM-DD/date=YYYY-MM-DD/*.parquet
  ref/instrument_master/date=YYYY-MM-DD/*.parquet             # point-in-time
  ref/calendar/{holidays,expiries,events}.parquet
  ref/cost_params/cost_params.parquet                          # dated statutory rates
  features/<feature_set_version>/...
  manifests/<dataset>/<date>.json                              # row counts, hashes, DQ results, source versions
```
- Raw data is never modified. Corrections produce a new clean version with a changelog.
- Point-in-time correctness: every feature is computed only from data with `exchange_ts ≤ decision_ts − latency_budget`.
- Timezone: all timestamps are stored in UTC (int64 ns) with an explicit `Asia/Kolkata` conversion at presentation only.

## 6.4 Option-chain reconstruction
For each minute (or second) build the chain snapshot `(expiry, strike, CE/PE) → {price, OI, volume, IV, staleness}`:
- Mark any contract whose last trade is older than X seconds as **stale** and never treat its price as tradable.
- Recompute IV ourselves (Black-76 on the synthetic forward from futures or put-call parity) and compare it with the vendor IV. Flag disagreements.
- Reconcile end-of-day with the bhavcopy (settlement price, OI). A mismatch above tolerance quarantines that day.

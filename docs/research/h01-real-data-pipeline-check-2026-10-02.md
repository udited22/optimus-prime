# H01 on real Dhan data: a pipeline check (2-Oct-2026)

**This is a pipeline check, not a strategy verdict.** It shows that real Dhan 1-minute history flows end to end: lake manifests → DQ policy → lake reader → H01 (S-ORB-001) → fill model → cost model → risk budget → ledger. The owner's target system is a regime model choosing among strategies, judged at the portfolio level, so one strategy's P&L on one small sample says nothing about that system. Thirteen trades also cannot support any statistical claim. Nothing here is VALIDATED, and no V1–V18 gate was run.

Produced with `scripts/h01_real_data.py`. The run artefacts (ledgers, fills, per-trade records) are in the gitignored `lake/runs/`, because they hold market prices. Only the summary numbers are given here.

## Inputs and labels

| Item | What was used |
|---|---|
| Market data | **REAL**: Dhan Data API 1-minute bars. NIFTY index, India VIX, and rolling weekly options ATM-2..ATM+2 CE/PE for expiry codes 1 and 2. DQ WARN parts were accepted; BLOCKED (quarantined) parts were excluded. |
| Window | 11-May-2026 .. 30-Sep-2026, in two segments, each with a 35-day warm-up in which no option bars are fed. It is the newest usable range ([coverage report](../data/dhan-coverage-2026-10-02.md)). 27-Mar-2026 is missing 09:15–09:18 for almost every strike, which quarantines the Mar–Apr window, so the warm-up starts after it. The lot-size history is only valid to 30-Sep-2026, so 1-Oct is excluded. |
| Futures input | **ASSUMED PROXY** (`FUTPROXY-2026-10-02.1`). Dhan has no candles for expired futures. Price is NIFTY index OHLC, with no basis. Volume is the summed nearest-expiry CE+PE option volume within ±1 strike in the same minute. H01's volume filter therefore runs on option volume, not futures volume (see the cross-check below). |
| Spreads and fills | **ASSUMED** synthetic spreads (`configs/backtest/synthetic_spreads.toml`). Dhan history has no bid/ask. |
| Costs | The D-05 cost model, with brokerage and statutory charges on every fill. |
| Risk | 2% of NAV per trade (`limits.toml`). A trade whose worst case at the stop exceeds that is declined (NO_TRADE_RISK). |

## Results

| Run | Label | Signals | Declined (risk cap) | Entries filled / unfilled | Closed trades | Exits | Gross P&L | Charges | Net P&L |
|---|---|---|---|---|---|---|---|---|---|
| ₹10,000 NAV | REAL-DATA BACKTEST | 49 | **49** | 0 / 0 | 0 | — | ₹0 | ₹0 | **₹0** |
| ₹1,00,000 NAV | **HYPOTHETICAL** | 49 | 33 | 13 / 3 | 13 | 3 target, 2 stop, 8 max-hold | ₹7,319.00 | ₹784.50 | **₹6,534.50** |

- **At ₹10k the risk rules decline every signal.** One NIFTY lot (65) of a ₹50–100 option risks more than ₹200 (2% of NAV) at the stop. The feasibility analysis predicted this, and it is the rules working, not a failure.
- **₹1L is HYPOTHETICAL** (no such capital is approved). Each trade's worst case at the stop was ₹1,116–₹1,996, inside the ₹2,000 budget. Charges were 10.7% of gross.
- **Filters on the same days** (both NAVs): NO_BREAK 2,719, LOW_VOLUME 1,137, INSUFFICIENT_HISTORY 878 (warm-up), VIX_FALLING 87, VIX_HISTORY_MISSING 35, INPUTS_MISSING 27. The ledgers balance and every run ends flat.

### Proxy cross-check against a real futures contract

The Oct-2026 future (active, so Dhan has its candles from about 29-Jul-2026) was run on 27-Aug .. 30-Sep-2026 at the HYPOTHETICAL ₹1L, against the proxy on the same days:

| Futures input | Signals | LOW_VOLUME rejections | Closed trades | Net P&L |
|---|---|---|---|---|
| Real NIFTY-FUT-2026-10-27 | 19 | 32 | 5 | ₹693.55 |
| ASSUMED proxy | 11 | 240 | 3 | ₹518.41 |

**The proxy changes H01's behaviour materially.** Its volume filter rejects about 7× more often. Proxy-based H01 numbers are therefore not H01 numbers. Before H01 is evaluated on real history, this needs a decision: another source of expired-futures 1-minute candles, or a spec change that defines the volume filter on options volume (a new hypothesis version).

## What this check established

- The real lake reads cleanly through manifests, with BLOCKED parts excluded and WARN parts accepted and logged.
- Rolling-option re-keying to per-contract series works, as do expiry-code 2 on expiry days (DTE ≥ 1), dated lot sizes and the cost model on real prices.
- The 2% cap binds as designed, and the ₹10k canary still cannot trade H01.
- The runs are reproducible: each segment records a ledger hash and the data fingerprints.

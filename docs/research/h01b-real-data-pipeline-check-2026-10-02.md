# H01b on real Dhan data: a pipeline check (2-Oct-2026)

**This is a pipeline check, not a strategy verdict.** It shows that the new hypothesis version H01b (`S-ORB-002`, RESEARCH) runs end to end on real Dhan 1-minute history: lake manifests → DQ policy → lake reader → H01b → fill model → cost model → risk budget → ledger. The owner's target system is a regime model choosing among strategies, judged at the portfolio level, so one strategy's P&L on one small sample says nothing about that system. Thirteen trades in under five months cannot support any statistical claim. Nothing here is VALIDATED, and no V1–V18 gate was run.

H01b was approved by the owner on 2-Oct-2026 17:43 IST, alongside OD-015 . **H01 (`S-ORB-001`) is unchanged.** H01b keeps H01's mechanics (15-minute opening range, 5-minute closes, 20-day same-slot volume median, VIX filter, ATM option, premium stop, 1.5 R target, max hold) and changes one definition: the breakout is measured on the **NIFTY index**, and the volume filter uses **near-the-money option volume** (nearest-expiry CE+PE within ±1 strike of the index, same minute) instead of futures volume. Dhan has no 1-minute candles for expired futures, so H01 as specified cannot be tested on Dhan history; H01b can.

Produced with `scripts/h01_real_data.py --hypothesis H01b`. Run artefacts (ledgers, fills, per-trade records) stay in the gitignored `lake/runs/` because they hold market prices; only summary numbers are given here.

## Update: the longer window after cost verification (2-Oct-2026, 19:45 IST)

Every cost schedule from 1-Oct-2021 is now VERIFIED against official sources (NSE circulars, SEBI, CBIC, and Upstox's published client rates for the pre-Oct-2024 slab era; `configs/costs/nse_fo_index_options.toml`, design/SOURCES S75–S85). That opens the longest fully clean stretch of the lake:

- **Window:** 30-Aug-2022 .. 11-Sep-2024, run as two parts: **30-Aug-2022..25-Apr-2024** and **31-May..11-Sep-2024**. The index and VIX are quarantined before 26-Jul-2022 (plus a 35-day warm-up). Next-expiry option parts are quarantined from 12-Sep-2024, and the stretch ends there. The NIFTY lot changed from 50 to 25 for every live contract on 26-Apr-2024; the lake reader holds one lot per contract, so no load may span that date, and the warm-up restarts after it. 61-day segments keep memory in bounds.
- **OD-016 in use:** 179 conflicting minutes were dropped across the window, and 21 series-days went over the 1% threshold and were excluded.

| Run | Signals | Declined (risk cap) | Entries filled / unfilled | Closed trades | Exits | Gross P&L | Charges | Net P&L |
|---|---|---|---|---|---|---|---|---|
| ₹10,000 NAV | 292 | **292** | 0 / 0 | 0 | — | ₹0 | ₹0 | **₹0** |
| ₹1,00,000 NAV (**HYPOTHETICAL**) | 292 | 51 | 241 sent: 198 filled, 43 unfilled | 198 | 17 target, 46 stop, 135 max-hold | −₹48,775.00 | ₹10,748.30 | **−₹59,523.30** |

- **At ₹10k nothing trades.** The worst case at the stop was ₹527–₹3,660 per signal, against a ₹200 budget, even with 25-lot contracts from 26-Apr-2024.
- **At a HYPOTHETICAL ₹1L, H01b lost about 60% of NAV over two years.** Only 17 of 198 trades reached the 1.5 R target, and most ended at the max hold. Charges were 22% of the gross loss on top. The recent 13-trade window (+₹6,534.50) was not representative.
- **This is still a pipeline check, not a verdict.** Spreads are ASSUMED and no validation gate was run. The end system is a regime model judged at the portfolio level, and H01b's regime gate (no classifier on real data yet) was not applied here. But the evidence so far is clearly negative for H01b as a standalone, ungated strategy.

## Update after OD-016 and the finished backfill (2-Oct-2026, 19:05 IST)

- The backfill finished (3,461 of 3,461 chunks). The final reingest left 118 chunks quarantined, none of them inside the inputs this window reads apart from the next-expiry PE parts described below.
- H01b was rerun on reader `LAKE-READER-2026-10-02.1` (OD-016). **The widest clean window is still 11-May..1-Oct-2026**: OD-016 removes the reader stop, but costs before 1-Mar-2026 are UNVERIFIED (the runner refuses them), and next-expiry PE parts for 6-Mar..5-Apr stay quarantined for genuine mid-session gaps. No conflicting rows occur in the window. **The results are unchanged**: the same 13 trades and the same totals as below. The data fingerprints differ only because the reader version is part of them.
- Widening further needs either a verified cost schedule for before March 2026 (`CM-2024-10-01` is from memory) or an owner decision to run on the quarantine-gapped window as a headline.

## Inputs and labels

| Item | What was used |
|---|---|
| Market data | **REAL**: Dhan Data API 1-minute bars. NIFTY index, India VIX, and rolling weekly options ATM-2..ATM+2 CE/PE for expiry codes 1 and 2. DQ WARN parts accepted; BLOCKED (quarantined) parts excluded. DQ rules after OD-015 (`DQ-2026-10-02.1`). |
| Signal series | `H01B-SIGNAL-2026-10-02.1`: index OHLC + near-the-money option volume. This is H01b's **definition**, not a proxy. |
| Window | **11-May-2026 .. 1-Oct-2026** (two segments, each with a 35-day warm-up in which no option bars are fed). This is the widest window that is fully clean for every input H01b reads; see "Why this window" below. |
| Spreads and fills | **ASSUMED** synthetic spreads (`configs/backtest/synthetic_spreads.toml`). Dhan history has no bid/ask. |
| Costs | The D-05 cost model (verified schedules from 1-Mar-2026), with brokerage and statutory charges on every fill. |
| Risk | 2% of NAV per trade (`limits.toml`). A trade whose worst case at the stop exceeds that is declined (NO_TRADE_RISK). |

## Results

| Run | Label | Signals | Declined (risk cap) | Entries filled / unfilled | Closed trades | Exits | Gross P&L | Charges | Net P&L |
|---|---|---|---|---|---|---|---|---|---|
| ₹10,000 NAV | REAL-DATA BACKTEST (pipeline check) | 50 | **50** | 0 / 0 | 0 | — | ₹0 | ₹0 | **₹0** |
| ₹1,00,000 NAV | **HYPOTHETICAL** | 50 | 34 | 13 / 3 | 13 | 3 target, 2 stop, 8 max-hold | ₹7,319.00 | ₹784.50 | **₹6,534.50** |

- **At ₹10k the risk rules decline every signal.** One NIFTY lot (65) risks more than ₹200 (2% of NAV) at the stop; the worst case at the stop over the 50 signals was ₹786–₹4,713. This is the rules working.
- **₹1L is HYPOTHETICAL** (no such capital is approved). Charges were 10.7% of gross.
- **Filters** (both NAVs): NO_BREAK 2,751, LOW_VOLUME 1,140, INSUFFICIENT_HISTORY 878 (warm-up), VIX_FALLING 87, VIX_HISTORY_MISSING 35, INPUTS_MISSING 27. The ledgers balance and every run ends flat.
- **1-Oct-2026** (now inside the lot history, see below) added one signal, declined at both NAVs. 2-Oct is a market holiday.
- **The trades equal the earlier H01-with-proxy run (11-May..30-Sep), by construction.** The H01b signal series is the same computation as the ASSUMED H01 futures proxy (`FUTPROXY-2026-10-02.1`), and on 11-May..30-Sep the data fingerprints match segment for segment and the 13 trades are identical. What changes is the meaning: for H01 those numbers were "H01 with a stand-in that filters about 7× more often than real futures"; for H01b they are H01b's own first pipeline check. Only the ledger hashes differ (the metadata names a different spec).

## Why this window

- **Costs:** the cost schedule before 1-Mar-2026 is UNVERIFIED, and the runner refuses it (a run from 8-Jan-2026 stopped with `UnverifiedConfigError`). That rules out anything before March.
- **Quarantine:** OD-015 recovered the 27-Mar opening gap for the nearest expiry, but next-expiry (code 2) PE parts for 6-Mar..5-Apr-2026 stay BLOCKED for genuine mid-session gaps (for example 77 of 375 minutes missing on 2-Apr). H01b uses the next expiry on expiry days, so a clean window must start after 5-Apr plus the 35-day warm-up: 11-May.
- **Lot sizes:** the dated lot history is now known through 2-Oct-2026 (`LOT-NIFTY-2026-10-02.1`, from NSE's official `fo_mktlots.csv`: NIFTY 65 for every listed month), so the window ends on 1-Oct, the last trading day.
- **A reader finding (no rule changed):** a run starting in Nov-2025 stops at the lake reader with "conflicting duplicate rows". On 3-Dec-2025 10:03 IST, Dhan's code-2 ATM and ATM+1 series both carry the 16-Dec 25950 CE and PE, with different prices (the two series report different spot values for that minute). A full scan of 1.94 million code-1/2 ATM±2 rows (Sep-2025..Sep-2026) found only these two conflicts. **Resolved by OD-016** (18:48 IST): the reader now drops conflicting rows as missing minutes (WARN), counted toward the 1% missing-minute threshold. On the real lake (20-Nov..12-Dec-2025) it drops 4 rows (2 minutes) and excludes no series-day.

## Sensitivity: a longer window with documented gaps

The same runner on **2-Mar..1-Oct-2026** (three segments, rerun on the OD-016 reader) reads verified costs throughout, but skips 9 quarantined next-expiry (code-2) PE parts in the first segment: 5 in the January warm-up and 4 covering 6-Mar..5-Apr, so some expiry-day PE trades in March cannot happen. It is not clean, so it is not the headline:

| Run | Signals | Declined (risk cap) | Closed trades | Net P&L |
|---|---|---|---|---|
| ₹10,000 NAV | 68 | 68 | 0 | ₹0 |
| ₹1,00,000 NAV (HYPOTHETICAL) | 68 | 52 | 13 | ₹6,534.50 |

The 18 extra signals from 2-Mar..10-May were **all declined by the 2% cap even at ₹1L**: the worst case at the stop reached ₹9,031 in the high-volatility spring. The 13 closed trades are the same trades as the headline run.

## What this check established

- H01b runs end to end on the real lake under the OD-015 DQ rules, through manifests, with BLOCKED parts excluded and WARN parts accepted and logged.
- H01b's volume filter is computed from the same lake series it trades, so it no longer depends on a futures stand-in. The earlier cross-check (the proxy rejected about 7× more often than the real Oct-2026 future) now describes the difference between H01 and H01b, not an error in H01b.
- The 2% cap binds as designed: the ₹10k canary cannot trade H01b on any day of the window.
- The runs are reproducible: each segment records a ledger hash and the data fingerprints.
- What is still missing before any verdict: bid/ask (spreads are ASSUMED), more history (the next-expiry options before Oct-2025 are still downloading, and pre-Mar-2026 costs are UNVERIFIED), the V1–V18 validation, and, above all, the portfolio-level evaluation inside the regime model.

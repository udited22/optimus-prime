# 04 — SEBI / NSE Compliance Checklist (as of 30-Sep-2026)

> This is an engineering compliance checklist, not legal advice. Before Phase 4, confirm the latest circulars on nseindia.com and sebi.gov.in and with the chosen broker's compliance desk. Source IDs refer to `SOURCES.md`. All accessed 30-Sep-2026.

## 4.1 Retail algorithmic trading framework

| # | Requirement | Status / date | Design response | Source |
|---|---|---|---|---|
| C1 | Retail algo framework (SEBI circular 4-Feb-2025) is **fully applicable to all brokers from 1-Apr-2026**. It was originally due 1-Aug-2025, then 1-Oct-2025, then followed a glide path | In force | Build to it from day one | S1, S4 |
| C2 | Any order placed via API is treated as an algo order; client-built algos **below 10 OPS need no individual registration**, but orders carry an exchange-provided generic Algo ID ("99999"; NNF 13th digit "0") set by the broker | In force | Stay < 10 OPS (design target ≤ 2 sustained). Verify the broker tags orders (inspect the order book/contract note) | S2, S3, S8 |
| C3 | Above 10 OPS per exchange/segment → algo must be registered with each exchange via the broker (auditor certificate + strategy write-up) | In force | **Not needed; hard-block in gateway** | S2, S3 |
| C4 | **Static IP mandatory** for API order placement; primary + optional secondary; change ≤ once per calendar week; one client per IP except family (written/2FA request) | In force | Cloud VM with reserved IP; runbook for IP change; the secondary IP is a failover host | S2, S6, S23 |
| C5 | **OAuth-based authentication only; 2FA** for API access; **open APIs not permitted**; unique client-specific API key | In force | Credential architecture in 16-security | S1, S2 |
| C6 | **All API sessions logged out daily** before the next trading day | In force | Daily pre-market auth step; kernel treats "no valid session" as HALTED | S2 |
| C7 | Brokers may restrict order types. Brokers report **market and IOC orders prohibited for algo orders**; market orders converted to protected limit (MPP) at some brokers | In force per brokers (NSE primary text UNVERIFIED) | LIMIT / SL-LIMIT only; never rely on conversion | S6, S7, S8, S33 |
| C8 | Retail algos hosted on broker servers **except** tech-savvy clients running their own logic on their own registered static IP | In force | We are "client-generated algo, own logic, own static IP". Do not use third-party unempanelled platforms or API bridges | S2 (I.h), S6, S8 |
| C9 | Self-built algo may be used by self and **family only** (spouse, dependent children, dependent parents). Offering it to others requires empanelment/registration (black-box algos → Research Analyst registration) | In force | Personal use only; no signal sharing, no selling | S1, S6 |
| C10 | Broker audit trail for API orders kept ≥ 5 years | Broker obligation | We keep our own journal ≥ 5 years too (tax/audit) | S2 |
| C11 | Exchanges may kill "rogue algos" impacting the market | In force | Rate limits, price sanity bands, self-trade prevention, cancel-on-disconnect | S2 |
| C12 | FYERS-specific: AMO not permitted for algo; daily 2FA; refresh-token flow discontinued | In force (FYERS) | Generalise: no AMO; daily auth | S8 |

## 4.2 F&O structural rules (SEBI 1-Oct-2024 circular and later changes)

| # | Measure | Effective | Impact on design | Source |
|---|---|---|---|---|
| F1 | **Upfront collection of option premium** from buyers | 1-Feb-2025 | The full premium must be in the account. The ₹10k NAV limits buyable premium to < ₹153/pt per lot | S9 |
| F2 | **No calendar-spread margin benefit on expiry day** for contracts expiring that day | 1-Feb-2025 | Avoid calendar spreads touching the expiring series on expiry day | S9 |
| F3 | **Intraday position-limit monitoring** (≥ 4 snapshots, originally) → now **FutEq/delta-based, 5 snapshots, no 15-min cure after 14:45**; closing price used after CAS equilibrium (NSE 31-Jul-2026) | 1-Apr-2025; revised 31-Jul-2026 | Irrelevant at our size (limits are in ₹ thousands of crores for entities, S20), but the kernel records delta exposure anyway | S9, S20 |
| F4 | **Minimum index contract value ₹15 lakh at introduction**; lot fixed so value is ₹15–20 lakh at review; periodic lot revisions (SEBI 30-Dec-2024 circular) | 20-Nov-2024 | **NIFTY lot = 65** from Jan-2026 expiries. Lot sizes change periodically → instrument master refresh + lot-change detector (DQ) | S9, S10, S11 |
| F5 | **One benchmark index with weekly expiry per exchange** (NSE: NIFTY) | 20-Nov-2024 | Only NIFTY weekly on NSE; BANKNIFTY monthly only (out of scope for 6 months anyway; OD-018 on 3-Oct-2026 moved Bank Nifty and Sensex into Phase A, and Sensex on BSE is not covered by this checklist yet) | S9 |
| F6 | **Additional 2% ELM on short options on expiry day** | 20-Nov-2024 | No naked shorts; defined-risk shorts only later with margin headroom checks | S9 |
| F7 | **NSE weekly/monthly expiry on Tuesday** (from 1-Sep-2025; BSE Thursday); holiday → previous trading day | In force | Expiry calendar derived from instrument master, never computed by weekday rule alone | S12, S13 |
| F8 | SEBI Board 24-Sep-2026: **no new F&O retail measures**; press reports of possibly scrapping weekly expiries were proposals, not rules | — | Monitor. Design must survive the loss of weekly expiries (strategies tagged by expiry dependence) | S21 |

## 4.3 Market timings (NSE equity derivatives)

| Session | Time (IST) | Source |
|---|---|---|
| Pre-open (derivatives) | 09:00–09:08 (random close in last minute); futures pre-open live since 8-Dec-2025 | S18, S27 |
| Normal market | **09:15–15:40** (extended from 15:30 w.e.f. **3-Aug-2026**) | S18, S27 |
| Closing price basis for derivatives | VWAP of **15:10–15:40** | S18 |
| Cash-market Closing Auction Session | 15:15–15:35 for eligible stocks (index constituents' closing prices come from CAS) | S19 |
| Trade modification | until 16:15 | S18 |

**Design implication.** Our live trading window is **09:15–15:00 IST**, with a hard flat by 15:00 (broker Exit-All for any residual position, OD-007) and no new entries at or after 14:00 (OD-008, superseding OD-003's 14:45). That avoids the closing-VWAP window, CAS-driven swings in the underlying (a moneycontrol headline on 29-Sep-2026 describes "CAS-led wild swings" on expiry; secondary, not relied on), and the no-cure-period monitoring after 14:45. Strategy research may study later windows offline, but trading after 15:00 would require a new owner decision.

## 4.4 Charges and taxes (per executed order, options)

| Levy | Rate | Side | Source |
|---|---|---|---|
| STT | **0.15% of premium** (from 1-Apr-2026; was 0.10%) | Sell | S14, S16, S17 |
| STT on exercised options | **0.15% of intrinsic value** (was 0.125%) | Buyer on exercise | S14, S16 |
| NSE transaction charge | **0.03553% of premium** (₹3,553/crore total outflow incl. IPFT) | Both | S15, S16 |
| SEBI turnover fee | ₹10/crore (0.0001%) | Both | S16, S17 |
| Stamp duty | 0.003% of premium | Buy | S16, S17 |
| GST | 18% on brokerage + transaction charges + SEBI fee | Both | S16 |
| Brokerage | ₹20/order (Zerodha, Upstox); ₹0 via Kotak Neo Trade API | Both | S16, S28, S33 |

The cost model (08-backtesting) holds these as **dated, versioned parameter tables**, so a backtest over 2021–2026 applies the rates in force on each trade date: STT on option premium 0.05% to 31-Mar-2023, 0.0625% from 1-Apr-2023, 0.10% from 1-Oct-2024 to 31-Mar-2026, then 0.15%; NSE options charges ₹53/lakh (from 1-Jan-2021), ₹50/lakh (from 1-Apr-2023), ₹3,553/crore true-to-label (from 1-Oct-2024). **Verified on 2-Oct-2026 against official sources** (NSE circulars, SEBI, CBIC, and Upstox's published client rates for the pre-Oct-2024 slab era; S75–S85). Every schedule from 1-Oct-2021 is now VERIFIED (`configs/costs/nse_fo_index_options.toml`).

## 4.5 Other obligations and checks
- [ ] **Tax:** F&O P&L is generally treated as non-speculative business income, with possible tax-audit requirements depending on turnover and profit. The treatment is **UNVERIFIED** and the 31.2% figure (30% + 4% cess) stays **ASSUMED** in net-return reporting. **No CA review is required (OD-017, 2-Oct-2026):** the owner handles tax at filing, and it is not a go-live blocker. The journal must export a contract-note-reconcilable P&L ledger.
- [ ] **Broker T&Cs:** read and store the API terms, especially on automation of login/2FA, data redistribution (Kite forbids redistribution, S23) and hosting.
- [ ] **Data licensing:** market data from broker APIs is for personal use. No redistribution in dashboards shared outside the owner and family.
- [ ] **Do not use** unempanelled third-party algo platforms or "API bridges" (S8).
- [ ] **Monthly regulatory watch** (automated research-agent task): scan NSE F&O/INVG circulars and SEBI circulars. Any change to lot size, expiry day, timings, charges, OPS or order types opens a ticket that blocks trading until the change is acknowledged in config.

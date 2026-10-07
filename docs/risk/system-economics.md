# 19 — Whole-system economics (net of everything)

Owner decision **OD-012** (1-Oct-2026): measure the *whole system's* economics, not only the strategies' gross edge, and optimise it over time. Every rupee the setup costs to run is counted. The cost-justification check is **advisory only**. It is never a kill switch and never stops, blocks or limits trading.

Code: `src/project100c/economics/`. Config: `configs/economics/economics.toml` (`ECON-2026-10-02.1`; OD-017 relabelled the tax basis, every figure is unchanged from `ECON-2026-10-01.1`). Worked example: `scripts/economics_report.py`. Tests: `tests/economics/`.

## 19.1 The waterfall (per month and per fiscal year)

```
Gross P&L (fills only, before any charge)
 - brokerage                      per executed order, from the D-05 brokerage plan
 - statutory and exchange charges STT, NSE transaction, SEBI fee, stamp duty, GST (D-05, dated)
 - fixed infrastructure           Dhan Data API, cloud server, LLM/API spend, optional broker plan fee
 = pre-tax net
 - income-tax accrual             ASSUMED (§19.3; OD-017: no CA review)
 = NET AFTER EVERYTHING           -> net return on opening NAV
```

- **Trading days come from the K-01 journal.** The journal is the system of record. `trading_days_from_journal` verifies the hash chain, groups FILLs by order, and prices each executed order **once** at its VWAP with `CostModel.order_charges`. That is how brokers bill it. The kernel's own FILL records price each fill separately (conservative on partial fills), and that figure is kept as `recorded_charges` for comparison. If a day is not flat (OD-002), a fill belongs to an unknown order, an order's fills span days, fills go net short, or a position was adopted with no cost basis, the function raises `EconomicsError` instead of producing a number.
- **Fixed costs are charged in full every calendar month**, whether or not the system traded. A line billed per N days (Dhan: ₹499 + GST per 30 days, S30) is pro-rated to an average calendar month of 30.4375 days: ₹499 × 30.4375 / 30 × 1.18 = **₹597.41/month**. This is a little above OD-011's "≈ ₹589/month", which is the price of one 30-day period. USD lines are converted at an **ASSUMED** USD/INR of 96 (the Sep-2026 range was about 94.4 to 96.1). Update the rate in the config; it is not fetched.
- **Infrastructure and tax are paid outside trading NAV** (OD-011: separate infrastructure budget). The statement carries NAV forward from the trading result only, and reports the infrastructure and tax burden against it.

### Metrics
| Metric | Definition |
|---|---|
| Net return on NAV | net after everything ÷ opening NAV of the month |
| Cost drag | (trading charges + fixed + tax) ÷ gross P&L. Undefined (n/a) when gross ≤ 0 |
| Fixed-cost fraction | fixed costs of the month ÷ opening NAV. This is also the **break-even gross monthly return** for fixed costs alone |
| Break-even incl. trading | (fixed + trading charges) ÷ opening NAV |
| NAV for the fixed-cost threshold | fixed costs ÷ threshold (1%/month): the NAV from which fixed costs stop dominating |
| Gross needed for target | gross monthly return so that net after fixed costs and tax ≥ target: (target × NAV ÷ (1 − tax rate) + fixed + trading) ÷ NAV |

## 19.2 Fixed-cost lines (`[[fixed_cost]]`)

| Line | Amount | Status | Optimisation idea (in config, shown when the advisory is raised) |
|---|---|---|---|
| Dhan Data API | ₹499 + 18% GST per 30 days | VERIFIED (S30) | Pause it between research download batches; it bills per 30 days and the lake keeps everything already downloaded |
| Cloud server, Mumbai, static IP | USD 5 + GST (ASSUMED range USD 5–20) | ASSUMED | Smallest instance that holds the workload; run research, shadow and paper locally and start the Mumbai server only for go-live |
| LLM / API spend | USD 0 (ASSUMED) | ASSUMED | Cap monthly spend, cache prompts and results, use the cheapest model that passes the validation tests |
| Upstox Plus | ₹0 subscription, **disabled** | VERIFIED (S73, S74) | Today Upstox Plus has no subscription fee but charges **₹30 per executed options order** (plan `upstox-plus-options`) instead of ₹20, (₹10 + GST more per order): stay on Upstox Basic unless a Plus feature is actually used |

## 19.3 Income tax (ASSUMED; OD-017: no CA review)

- **ASSUMED** treatment: F&O income as non-speculative business income at a 30% slab plus 4% cess = **31.2%** effective. Expenses, including infrastructure, are deductible.
- Tax accrues each month as the change in fiscal-year-to-date tax on positive FY-to-date taxable income. A later loss in the same FY reverses earlier accruals. Each FY (April–March) starts from zero.
- **Loss carry-forward (UNVERIFIED):** business losses may be carried forward for 8 years if the return is filed on time. This is only noted on the statement; it is **not applied automatically**. An owner can pass a brought-forward loss explicitly.
- **Tax audit (UNVERIFIED):** F&O turnover is computed as the sum of absolute trade P&L. The statement adds a note once turnover reaches half the limit (₹10 crore when at least 95% of transactions are digital, otherwise ₹1 crore).
- **OD-017 (2-Oct-2026): no CA review is required.** the owner handles tax at filing. The 31.2% figure stays **ASSUMED** and is used only to report net returns; it is not tax advice and it is **not a go-live blocker**. The config loader refuses a tax block that is not labelled ASSUMED (`basis` must say so).

## 19.4 Cost-justification check: advisory only

- **Rule:** if the monthly net return on NAV after all costs and tax is below **0.5%** (ASSUMED threshold) in **every one of the last 3 months**, the advisory is raised (`BELOW_THRESHOLD`). With fewer than 3 months the status is `INSUFFICIENT_DATA`.
- It is also raised (**structural**) whenever fixed costs alone exceed **1% of NAV a month**. At that point the setup cannot pay for itself, whatever the strategies do.
- **Output:**
  - a headline
  - recommendations: the structural line, each enabled fixed line's optimisation idea (largest first), and a trading-drag line if charges took ≥ 50% of gross
  - always the sentence "Advisory only: this check never stops, blocks or limits trading (it is not a kill switch)."
- **Where it appears:** the K-14 daily/monthly owner report (`render_markdown`, ready to include once K-14 is built) and the dashboard's Economics card (§17.5).
- **Enforcement of "advisory only":**
  - `Advisory(blocks_trading=True)` raises.
  - The config loader refuses `advisory_only = false`.
  - An AST test proves that `economics/` cannot import the kernel, broker, observability or agents, and that the kernel and broker cannot import `economics/`.

## 19.5 Worked example (config `ECON-2026-10-01.1`; ASSUMED FX 96; target 0.5%/month net; tax 31.2% ASSUMED)

Fixed running costs a month:

| Scenario | Fixed / month | Fixed < 1% of NAV from |
|---|---:|---:|
| **Base:** Dhan ₹597.41 + server USD 5 (₹566.40) | **₹1,163.81** | **₹1,16,381** |
| Server USD 20 (₹2,265.60) | ₹2,863.01 | ₹2,86,301 |
| Server USD 20 + LLM/API USD 10 (₹1,132.80) | ₹3,995.81 | ₹3,99,581 |

Base case by NAV:

| NAV | Fixed % of NAV a month (break-even) | Annualised | Gross a month for +0.5% net | Justified (< 1%) |
|---:|---:|---:|---:|:---:|
| ₹10,000 (canary) | 11.64% | ≈ 275% | 12.36% | no |
| ₹25,000 | 4.66% | 73% | 5.38% | no |
| ₹50,000 | 2.33% | 32% | 3.05% | no |
| ₹1,00,000 | 1.16% | 15% | 1.89% | no |
| ₹2,00,000 | 0.58% | 7% | 1.31% | yes |
| ₹5,00,000 | 0.23% | 3% | 0.96% | yes |
| ₹10,00,000 | 0.12% | 1% | 0.84% | yes |

**Reading it:** at the ₹10k canary NAV the setup **cannot justify its running costs**. It would need about 11.6% gross a month just to cover fixed costs, before trading charges and tax. This is **expected and acceptable**: the canary exists to prove the machine (§15), not to earn. The advisory therefore shows as raised (structural) on the dashboard from day one. It is information, not a stop. Fixed costs fall below 1% of NAV a month from about **₹1.16 lakh** (base case), **₹2.86 lakh** (USD 20 server) or **₹4.0 lakh** (with USD 10 of LLM spend). Those are the NAV levels at which the economics start to make sense.

## 19.6 Still open
- K-14 owner report generator: not built. The economics markdown section is ready to plug in.
- Real-money months: the FX rate, the server bill and LLM spend should come from invoices, so the ASSUMED lines become VERIFIED.
- Contract-note reconciliation of the repriced charges (D-05).

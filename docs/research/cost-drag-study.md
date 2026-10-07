# Cost-drag study: what costs take out of a month, by NAV and entries a day

> **SIMULATED. ZERO-EDGE BASELINE, NOT A FORECAST.** Seeded SYNTHETIC sessions and a SYNTHETIC Black-Scholes option chain. The entry direction is a coin flip, so nothing here is an expected return. Quotes, fills, slippage and volatility are ASSUMED. Costs come from the versioned cost model (Upstox ₹20 per executed order, verified 30-Sep-2026; statutory charges CM-2026-04-01) and economics config ECON-2026-10-01.1 (fixed ₹1,163.81 a month; tax 31.2%, ASSUMED).

Requested by the owner on 2-Oct-2026 (OD-012, OD-014). ₹10,000 is the first-month trial, and the plan is to scale to ₹1–2 lakh, so the study covers ₹10k, ₹50k, ₹1L, ₹2L and ₹5L at 1, 3, 5 and 10 entries a day (10 is the OD-014 system cap).

- Run: `.venv/bin/python scripts/cost_drag_study.py --workers 8 --seeds 8 --out docs/research/cost-drag-results.md` (about 3 minutes on 8 cores).
- Full output: [cost-drag-results.md](cost-drag-results.md).
- Code: `src/project100c/studies/cost_drag.py`.

## Method

- **Market:** 22 sessions from 2-Nov-2026, taken from the holiday book, with 8 seeds. Each day has zero drift, an annualised volatility drawn from 9–18% (VIX the same), and an opening gap drawn from N(0, 0.35%). Every (NAV, entries) cell sees the same market for a given seed (common random numbers).
- **Policy (zero edge):** k entries a day at evenly spaced minutes between 09:25 and 13:55. Each is CE or PE by a seeded coin, 1 lot (65), on the nearest weekly expiry with DTE ≥ 1. It uses a 30% premium stop (SL-LIMIT, 4-tick offset) and a 20-minute time exit, with no profit target.
- **The real stack:** allocator v1, the Risk Governor (paper venue, regime gate on, every limit live), the kernel runtime, the fake broker and the cost model. The charges are taken from the hash-chained journal (`trading_days_from_journal`).
- **Sizing:** to fit k stops inside the 4% daily risk budget, the per-trade budget is min(2%, 4% / k) of NAV (the allocator may only lower risk). Strikes start ATM and walk further OTM (the new `strike_fit_steps`) until the risk at the stop fits the budget.
- **Independent days:** each day runs at the cell's NAV with no compounding, so a strategy kill latched on one day does not carry over.
- **What is derived:**
  - E, the gross each round trip must average, = (target pre-tax + fixed + charges) / round trips.
  - The win rate needed at payoff b (average win = b·R): p = (E/R + 1) / (b + 1), where R is the gross loss at the stop.

## Headline (means over 8 seeds, per month of 22 sessions)

| NAV | Entries/day | Round trips | Charges (brokerage + statutory) | + fixed = total | Cost % NAV | Mean premium | Break-even gross per trip |
|---:|---:|---:|---:|---:|---:|---:|---:|
| ₹10,000 | 1 | 13.9 | ₹668 | ₹1,831 | **18.3%** | ₹5.67 | ₹132 (0.95 R) |
| ₹10,000 | 3 | 65.8 | ₹3,134 | ₹4,298 | **43.0%** | ₹2.99 | ₹65 (0.76 R) |
| ₹10,000 | 10 | 0 | ₹0 | ₹1,164 | 11.6% | – | nothing fits ₹40 |
| ₹50,000 | 1 | 20.5 | ₹1,105 | ₹2,269 | 4.5% | ₹42.58 | ₹111 (0.13 R) |
| ₹50,000 | 10 | 191.1 | ₹9,162 | ₹10,326 | **20.7%** | ₹4.75 | ₹54 (0.45 R) |
| ₹1,00,000 | 1 | 21.0 | ₹1,280 | ₹2,444 | 2.4% | ₹88.09 | ₹116 (0.07 R) |
| ₹1,00,000 | 3 | 65.8 | ₹3,709 | ₹4,873 | 4.9% | ₹58.35 | ₹74 (0.06 R) |
| ₹1,00,000 | 5 | 110.0 | ₹5,742 | ₹6,905 | 6.9% | ₹32.09 | ₹63 (0.10 R) |
| ₹1,00,000 | 10 | 201.6 | ₹9,946 | ₹11,110 | **11.1%** | ₹13.57 | ₹55 (0.19 R) |
| ₹2,00,000 | 1 | 22.0 | ₹1,550 | ₹2,714 | 1.4% | ₹150.48 | ₹123 (0.04 R) |
| ₹2,00,000 | 3 | 66.0 | ₹4,314 | ₹5,478 | 2.7% | ₹115.87 | ₹83 (0.04 R) |
| ₹2,00,000 | 5 | 110.0 | ₹6,359 | ₹7,523 | 3.8% | ₹68.34 | ₹68 (0.05 R) |
| ₹2,00,000 | 10 | 205.5 | ₹10,694 | ₹11,857 | 5.9% | ₹30.97 | ₹58 (0.09 R) |
| ₹5,00,000 | 1 | 22.0 | ₹1,573 | ₹2,736 | 0.5% | ₹157.15 | ₹124 (0.04 R) |
| ₹5,00,000 | 10 | 212.4 | ₹12,770 | ₹13,934 | 2.8% | ₹83.29 | ₹66 (0.04 R) |

- Brokerage is exactly ₹40 a round trip (₹20 × 2). Statutory charges add about ₹8–20 a trip, depending on the premium.
- Round trips fall short of 22 × k where the Governor's 15-minute cooldown after a stop, the strategy kill (3 stops in a row) or the daily headroom binds. These bind most at small NAV and high k, where cheap far-OTM options hit a 30% stop often.

## What it means

1. **At ₹10k, costs are the whole story.** ₹1,164 of fixed costs alone is 11.6% of NAV a month. Even 1 entry a day takes 18% of NAV, and only a far-OTM option of about ₹6 fits a ₹200 budget. Then each trade must earn 0.95 R gross just to break even, which at b = 1 needs a 98% win rate. At 3 a day costs reach 43% of NAV. The trial month can test the mechanics, but it cannot be expected to make money.
2. **More entries a day under a fixed 4% daily risk budget means smaller, cheaper, further-OTM options.** At ₹1L, 10 a day means a ₹400 budget and about ₹14 premiums. Brokerage is flat per order, so the cost per trade stays near ₹50 while the size of each trade shrinks, and costs grow from 0.07 R to 0.19 R a trade.
3. **At ₹1–2L, the number of entries a day is the lever.**

   | Mix | Entries/day | Cost % NAV at ₹1L | Cost % NAV at ₹2L |
   |:---|---:|---:|---:|
   | 3 strategies × 1 | 3 | 4.9% | 2.7% |
   | RSI/MACD 2 + 3 others × 1 | 5 | 6.9% | 3.8% |
   | Scalper 6 + RSI/MACD 2 + 2 others × 1 | 10 | 12.0% | 6.3% |

   In the scalper mix, the scalper alone accounts for 132 of the 220 round trips a month (₹6.5–6.9k of charges).
4. **Scalping at ₹20 an order:** each round trip costs about 15–16 ticks of 0.05 on 65 qty before any spread or slippage. A scalper must reliably capture more than that per trade, after the spread. This is the cost-sensitivity flag on H17.
5. **The win rates needed are requirements, not estimates.** At ₹1–2L with 1–5 a day, breaking even after all costs needs roughly 52–55% at b = 1, 42–44% at b = 1.5 and 35–37% at b = 2. Netting +5% a month after tax needs about 57–66% at b = 1, or 38–44% at b = 2 (see the results file). The zero-edge policy has none of that.

## Recommendation (plain)

The bars are ASSUMED: monthly costs (charges + fixed) at most a share of NAV, and the break-even gross per trade at most 0.2 R. The table gives the most entries a day, of those tested, that meet both.

| NAV | Costs ≤ 2% of NAV | Costs ≤ 5% of NAV |
|---:|:---:|:---:|
| ₹10,000 | none | none |
| ₹50,000 | none | up to 1 a day |
| ₹1,00,000 | none | up to 3 a day |
| ₹2,00,000 | up to 1 a day | up to 5 a day |
| ₹5,00,000 | up to 5 a day | up to 10 a day |

In plain words:
- At ₹10k, treat the month as a paid rehearsal.
- At ₹1L, stay at about 3 entries a day across the whole book until a strategy shows a measured edge well above about 0.1 R a trade after costs.
- At ₹2L, about 5 a day.
- The 10-a-day cap (and a scalper) only stops being dominated by costs from about ₹5L. Below that it needs an edge per trade larger than any strategy here has evidence for.

Per-spec caps (OD-014) are the way to keep the book inside these numbers. No strategy has been validated, and nothing here is evidence of an edge.

## Limits of the study

- Synthetic Black-Scholes prices (smile ASSUMED) and a fixed 1-tick half-spread. Real far-OTM spreads are wider, so the real costs at small NAV are worse.
- No events, no gaps beyond N(0, 0.35%), and no expiry-day (0DTE) trades.
- The 30% stop and 20-minute exit are one policy. A different stop changes R, and with it the break-even in R, but not the rupee charges.
- No compounding within the month; 8 seeds. The net P&L spread in the results file is cost plus noise, never a forecast.

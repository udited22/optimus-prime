> SIMULATED: seeded SYNTHETIC sessions and a SYNTHETIC Black-Scholes option chain; not market data
> ZERO-EDGE BASELINE, NOT A FORECAST: entry direction is a coin flip; the P&L shows cost drag, not expected returns
> ASSUMED: quotes are the synthetic close +/- 1 tick; fills, slippage, volatility and costs are model assumptions

Grid: NAV ₹10,000, ₹50,000, ₹1,00,000, ₹2,00,000, ₹5,00,000 x entries a day 1, 3, 5, 10; 22 sessions from 2026-11-02; seeds 1, 2, 3, 4, 5, 6, 7, 8; economics ECON-2026-10-01.1: fixed ₹1,163.81/month, tax 31.2% (ASSUMED).

## Headline: cost drag per month (means over seeds)

| NAV | Entries/day | Per-trade budget | Round trips/month | Brokerage | Statutory | Fixed | Total cost | Cost % NAV | Mean premium | R (loss at stop) | Break-even gross/trade | Break-even / R |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ₹10,000 | 1 | ₹200 (2.0%) | 13.9 | ₹555 | ₹113 | ₹1,164 | ₹1,831 | 18.3% | ₹5.67 | ₹139 | ₹132 | 0.95 R |
| ₹50,000 | 1 | ₹1,000 (2.0%) | 20.5 | ₹820 | ₹285 | ₹1,164 | ₹2,269 | 4.5% | ₹42.58 | ₹857 | ₹111 | 0.13 R |
| ₹1,00,000 | 1 | ₹2,000 (2.0%) | 21.0 | ₹840 | ₹440 | ₹1,164 | ₹2,444 | 2.4% | ₹88.09 | ₹1,743 | ₹116 | 0.07 R |
| ₹2,00,000 | 1 | ₹4,000 (2.0%) | 22.0 | ₹880 | ₹670 | ₹1,164 | ₹2,714 | 1.4% | ₹150.48 | ₹2,957 | ₹123 | 0.04 R |
| ₹5,00,000 | 1 | ₹10,000 (2.0%) | 22.0 | ₹880 | ₹693 | ₹1,164 | ₹2,736 | 0.5% | ₹157.15 | ₹3,087 | ₹124 | 0.04 R |
| ₹10,000 | 3 | ₹133 (1.3%) | 65.8 | ₹2,630 | ₹504 | ₹1,164 | ₹4,298 | 43.0% | ₹2.99 | ₹87 | ₹65 | 0.76 R |
| ₹50,000 | 3 | ₹667 (1.3%) | 65.8 | ₹2,630 | ₹759 | ₹1,164 | ₹4,552 | 9.1% | ₹27.33 | ₹560 | ₹69 | 0.12 R |
| ₹1,00,000 | 3 | ₹1,333 (1.3%) | 65.8 | ₹2,630 | ₹1,079 | ₹1,164 | ₹4,873 | 4.9% | ₹58.35 | ₹1,164 | ₹74 | 0.06 R |
| ₹2,00,000 | 3 | ₹2,667 (1.3%) | 66.0 | ₹2,640 | ₹1,674 | ₹1,164 | ₹5,478 | 2.7% | ₹115.87 | ₹2,283 | ₹83 | 0.04 R |
| ₹5,00,000 | 3 | ₹6,667 (1.3%) | 66.0 | ₹2,640 | ₹2,093 | ₹1,164 | ₹5,897 | 1.2% | ₹156.58 | ₹3,075 | ₹89 | 0.03 R |
| ₹10,000 | 5 | ₹80 (0.8%) | 67.2 | ₹2,690 | ₹492 | ₹1,164 | ₹4,346 | 43.5% | ₹0.82 | ₹44 | ₹65 | 1.46 R |
| ₹50,000 | 5 | ₹400 (0.8%) | 109.8 | ₹4,390 | ₹1,033 | ₹1,164 | ₹6,587 | 13.2% | ₹14.16 | ₹304 | ₹60 | 0.20 R |
| ₹1,00,000 | 5 | ₹800 (0.8%) | 110.0 | ₹4,400 | ₹1,342 | ₹1,164 | ₹6,905 | 6.9% | ₹32.09 | ₹653 | ₹63 | 0.10 R |
| ₹2,00,000 | 5 | ₹1,600 (0.8%) | 110.0 | ₹4,400 | ₹1,959 | ₹1,164 | ₹7,523 | 3.8% | ₹68.34 | ₹1,358 | ₹68 | 0.05 R |
| ₹5,00,000 | 5 | ₹4,000 (0.8%) | 110.0 | ₹4,400 | ₹3,276 | ₹1,164 | ₹8,840 | 1.8% | ₹145.76 | ₹2,865 | ₹80 | 0.03 R |
| ₹10,000 | 10 | ₹40 (0.4%) | 0.0 | ₹0 | ₹0 | ₹1,164 | ₹1,164 | 11.6% | n/a | n/a | n/a | n/a |
| ₹50,000 | 10 | ₹200 (0.4%) | 191.1 | ₹7,645 | ₹1,517 | ₹1,164 | ₹10,326 | 20.7% | ₹4.75 | ₹121 | ₹54 | 0.45 R |
| ₹1,00,000 | 10 | ₹400 (0.4%) | 201.6 | ₹8,065 | ₹1,881 | ₹1,164 | ₹11,110 | 11.1% | ₹13.57 | ₹292 | ₹55 | 0.19 R |
| ₹2,00,000 | 10 | ₹800 (0.4%) | 205.5 | ₹8,220 | ₹2,474 | ₹1,164 | ₹11,857 | 5.9% | ₹30.97 | ₹631 | ₹58 | 0.09 R |
| ₹5,00,000 | 10 | ₹2,000 (0.4%) | 212.4 | ₹8,495 | ₹4,275 | ₹1,164 | ₹13,934 | 2.8% | ₹83.29 | ₹1,649 | ₹66 | 0.04 R |

A cell with 0 round trips means no strike fitted the per-trade budget, or the allocator refused the entry.

## Win rate a strategy would need (from the measured costs and R)

p = (E / R + 1) / (b + 1), where E is the gross each trade must average, R the gross loss at the stop and b the average win as a multiple of R. These are requirements, not estimates.

| NAV | Entries/day | net 0% @ b=1 | net 0% @ b=1.5 | net 0% @ b=2 | net 0% @ b=3 | net 5% @ b=1 | net 5% @ b=1.5 | net 5% @ b=2 | net 5% @ b=3 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ₹10,000 | 1 | 98% | 78% | 65% | 49% | 116% (infeasible) | 93% | 78% | 58% |
| ₹50,000 | 1 | 56% | 45% | 38% | 28% | 67% | 53% | 45% | 33% |
| ₹1,00,000 | 1 | 53% | 43% | 36% | 27% | 63% | 51% | 42% | 32% |
| ₹2,00,000 | 1 | 52% | 42% | 35% | 26% | 63% | 51% | 42% | 32% |
| ₹5,00,000 | 1 | 52% | 42% | 35% | 26% | 79% | 63% | 53% | 39% |
| ₹10,000 | 3 | 88% | 70% | 59% | 44% | 94% | 75% | 63% | 47% |
| ₹50,000 | 3 | 56% | 45% | 37% | 28% | 61% | 49% | 41% | 31% |
| ₹1,00,000 | 3 | 53% | 43% | 35% | 27% | 58% | 46% | 39% | 29% |
| ₹2,00,000 | 3 | 52% | 41% | 35% | 26% | 57% | 45% | 38% | 28% |
| ₹5,00,000 | 3 | 51% | 41% | 34% | 26% | 60% | 48% | 40% | 30% |
| ₹10,000 | 5 | 123% (infeasible) | 98% | 82% | 61% | 135% (infeasible) | 108% (infeasible) | 90% | 68% |
| ₹50,000 | 5 | 60% | 48% | 40% | 30% | 65% | 52% | 44% | 33% |
| ₹1,00,000 | 5 | 55% | 44% | 37% | 27% | 60% | 48% | 40% | 30% |
| ₹2,00,000 | 5 | 53% | 42% | 35% | 26% | 57% | 46% | 38% | 29% |
| ₹5,00,000 | 5 | 51% | 41% | 34% | 26% | 57% | 46% | 38% | 29% |
| ₹10,000 | 10 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| ₹50,000 | 10 | 72% | 58% | 48% | 36% | 80% | 64% | 53% | 40% |
| ₹1,00,000 | 10 | 59% | 48% | 40% | 30% | 66% | 52% | 44% | 33% |
| ₹2,00,000 | 10 | 55% | 44% | 36% | 27% | 60% | 48% | 40% | 30% |
| ₹5,00,000 | 10 | 52% | 42% | 35% | 26% | 57% | 46% | 38% | 29% |

## The synthetic month (ZERO-EDGE: this is cost drag plus noise, never an expected return)

| NAV | Entries/day | Net % NAV p5 | median | p95 | Refusals (count over all seeds) | Kills |
|---:|---:|---:|---:|---:|:---|---:|
| ₹10,000 | 1 | -18.4% | -17.9% | -9.2% | SMALLEST_LOT_EXCEEDS_BUDGET 65 | 0 |
| ₹50,000 | 1 | -5.6% | -1.3% | 3.5% | SMALLEST_LOT_EXCEEDS_BUDGET 12 | 0 |
| ₹1,00,000 | 1 | -5.2% | -1.1% | 5.1% | SMALLEST_LOT_EXCEEDS_BUDGET 8 | 0 |
| ₹2,00,000 | 1 | -4.8% | -1.1% | 3.9% | none | 0 |
| ₹5,00,000 | 1 | -2.1% | -0.5% | 1.6% | none | 0 |
| ₹10,000 | 3 | -48.6% | -40.4% | -28.1% | NO_TRADE_RISK_OVER_ALLOCATION 2 | 9 |
| ₹50,000 | 3 | -10.9% | -1.5% | 15.9% | DAILY_HEADROOM 2 | 1 |
| ₹1,00,000 | 3 | -6.7% | 0.5% | 16.4% | DAILY_HEADROOM 2 | 0 |
| ₹2,00,000 | 3 | -4.0% | 0.9% | 13.0% | none | 0 |
| ₹5,00,000 | 3 | -1.9% | 0.9% | 6.2% | none | 0 |
| ₹10,000 | 5 | -48.1% | -46.5% | -45.0% | NO_TRADE_RISK_OVER_ALLOCATION 267, STRATEGY_KILLED 75 | 38 |
| ₹50,000 | 5 | -13.5% | -11.4% | -2.5% | STRATEGY_KILLED 2 | 5 |
| ₹1,00,000 | 5 | -8.2% | -4.7% | 1.6% | none | 0 |
| ₹2,00,000 | 5 | -4.7% | -2.4% | 3.7% | none | 0 |
| ₹5,00,000 | 5 | -3.5% | -1.2% | 4.5% | none | 0 |
| ₹10,000 | 10 | -11.6% | -11.6% | -11.6% | NO_TRADE_RISK_OVER_ALLOCATION 1760 | 0 |
| ₹50,000 | 10 | -22.1% | -19.9% | -15.4% | COOLDOWN 149, NO_TRADE_RISK_OVER_ALLOCATION 2, STRATEGY_KILLED 80 | 26 |
| ₹1,00,000 | 10 | -12.8% | -6.8% | -1.7% | COOLDOWN 124, STRATEGY_KILLED 23 | 6 |
| ₹2,00,000 | 10 | -8.8% | -2.2% | 2.6% | COOLDOWN 96, STRATEGY_KILLED 20 | 4 |
| ₹5,00,000 | 10 | -7.1% | 0.3% | 4.3% | COOLDOWN 61 | 0 |

## Strategy mixes at the planned scale

Per-trip charges are the measured ones of the cell with the mix's total entries a day (the per-trade budget is min(2%, 4% / entries) of NAV, so more entries mean cheaper, further-OTM strikes).

### NAV ₹1,00,000

| Mix | Strategy | Entries/day | Round trips/month | Charges/month | Charges per trip in ticks (65 qty) |
|:---|:---|---:|---:|---:|---:|
| Canary style: 3 strategies x 1 a day | strategy A | 1 | 22 | ₹1,241 | 17.4 |
| Canary style: 3 strategies x 1 a day | strategy B | 1 | 22 | ₹1,241 | 17.4 |
| Canary style: 3 strategies x 1 a day | strategy C | 1 | 22 | ₹1,241 | 17.4 |
| Canary style: 3 strategies x 1 a day | **total** | **3** | **66** | **₹3,723** | |
| RSI/MACD reversal 2 + 3 others x 1 | RSI/MACD reversal (H18) | 2 | 44 | ₹2,297 | 16.1 |
| RSI/MACD reversal 2 + 3 others x 1 | strategy A | 1 | 22 | ₹1,148 | 16.1 |
| RSI/MACD reversal 2 + 3 others x 1 | strategy B | 1 | 22 | ₹1,148 | 16.1 |
| RSI/MACD reversal 2 + 3 others x 1 | strategy C | 1 | 22 | ₹1,148 | 16.1 |
| RSI/MACD reversal 2 + 3 others x 1 | **total** | **5** | **110** | **₹5,742** | |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | scalper (H17) | 6 | 132 | ₹6,512 | 15.2 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | RSI/MACD reversal (H18) | 2 | 44 | ₹2,171 | 15.2 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | strategy A | 1 | 22 | ₹1,085 | 15.2 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | strategy B | 1 | 22 | ₹1,085 | 15.2 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | **total** | **10** | **220** | **₹10,853** | |

- Canary style: 3 strategies x 1 a day: charges ₹3,723 + fixed ₹1,164 = ₹4,887 a month (4.9% of NAV); each round trip must average ₹74 gross to break even; per-trade budget ₹1,333, mean premium ₹58.35.
- RSI/MACD reversal 2 + 3 others x 1: charges ₹5,742 + fixed ₹1,164 = ₹6,905 a month (6.9% of NAV); each round trip must average ₹63 gross to break even; per-trade budget ₹800, mean premium ₹32.09.
- Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap): charges ₹10,853 + fixed ₹1,164 = ₹12,017 a month (12.0% of NAV); each round trip must average ₹55 gross to break even; per-trade budget ₹400, mean premium ₹13.57.

### NAV ₹2,00,000

| Mix | Strategy | Entries/day | Round trips/month | Charges/month | Charges per trip in ticks (65 qty) |
|:---|:---|---:|---:|---:|---:|
| Canary style: 3 strategies x 1 a day | strategy A | 1 | 22 | ₹1,438 | 20.1 |
| Canary style: 3 strategies x 1 a day | strategy B | 1 | 22 | ₹1,438 | 20.1 |
| Canary style: 3 strategies x 1 a day | strategy C | 1 | 22 | ₹1,438 | 20.1 |
| Canary style: 3 strategies x 1 a day | **total** | **3** | **66** | **₹4,314** | |
| RSI/MACD reversal 2 + 3 others x 1 | RSI/MACD reversal (H18) | 2 | 44 | ₹2,544 | 17.8 |
| RSI/MACD reversal 2 + 3 others x 1 | strategy A | 1 | 22 | ₹1,272 | 17.8 |
| RSI/MACD reversal 2 + 3 others x 1 | strategy B | 1 | 22 | ₹1,272 | 17.8 |
| RSI/MACD reversal 2 + 3 others x 1 | strategy C | 1 | 22 | ₹1,272 | 17.8 |
| RSI/MACD reversal 2 + 3 others x 1 | **total** | **5** | **110** | **₹6,359** | |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | scalper (H17) | 6 | 132 | ₹6,869 | 16.0 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | RSI/MACD reversal (H18) | 2 | 44 | ₹2,290 | 16.0 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | strategy A | 1 | 22 | ₹1,145 | 16.0 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | strategy B | 1 | 22 | ₹1,145 | 16.0 |
| Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap) | **total** | **10** | **220** | **₹11,448** | |

- Canary style: 3 strategies x 1 a day: charges ₹4,314 + fixed ₹1,164 = ₹5,478 a month (2.7% of NAV); each round trip must average ₹83 gross to break even; per-trade budget ₹2,667, mean premium ₹115.87.
- RSI/MACD reversal 2 + 3 others x 1: charges ₹6,359 + fixed ₹1,164 = ₹7,523 a month (3.8% of NAV); each round trip must average ₹68 gross to break even; per-trade budget ₹1,600, mean premium ₹68.34.
- Scalper 6 + RSI/MACD 2 + 2 others x 1 (the system cap): charges ₹11,448 + fixed ₹1,164 = ₹12,612 a month (6.3% of NAV); each round trip must average ₹57 gross to break even; per-trade budget ₹800, mean premium ₹30.97.

## Recommendation (plain)

The most entries a day (of those tested) at which charges plus fixed costs stay within a share of NAV a month AND the break-even gross per trade stays within 0.2 R (both bars ASSUMED):

| NAV | costs <= 2% of NAV | costs <= 5% of NAV |
|---:|:---:|:---:|
| ₹10,000 | none | none |
| ₹50,000 | none | up to 1 a day |
| ₹1,00,000 | none | up to 3 a day |
| ₹2,00,000 | up to 1 a day | up to 5 a day |
| ₹5,00,000 | up to 5 a day | up to 10 a day |

_Run time 191s._

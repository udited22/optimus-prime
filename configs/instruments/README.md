# Instrument reference configs

| File | What | Sources |
|---|---|---|
| `nifty_lot_sizes.toml` | NIFTY market-lot history from 1-Jan-2021: 75 → 50 (NSE/FAOP/47854), 50 → 25 (26-Apr-2024 circular), 25 → 75 (NSE/FAOP/64625, 64672, 64990), 75 → 65 (NSE/FAOP/70616). Resolved per contract (expiry + weekly/monthly cycle) and trade date | Exchange circulars + one secondary source each (see `sources` per revision); the 30-Sep-2026 Upstox + Kite files are cross-checked in tests |

Lot sizes change per contract, not per date: existing weekly/monthly contracts usually keep the old lot until expiry and
pre-existing long-dated contracts switch on a stated EOD. `instruments.lot_history.LotSizeHistory.lot_size()` raises
`LotSizeHistoryError` before 2021, after the `known_through_trade_date` (a later revision may exist), and for a contract
that could not have existed. The point-in-time instrument master stays authoritative wherever we have one.

**Derived, not quoted:** the 2021 revision names months, not dates, so its first/last affected expiry dates come from the
circular wording plus our Thursday rule and holiday book. Tests check that each revision's last-old and first-new
expiries are consecutive expiries of that cycle.

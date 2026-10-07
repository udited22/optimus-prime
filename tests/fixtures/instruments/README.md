# Instrument-master fixtures (real public data, subset)

Both files were downloaded on 30-Sep-2026 from **public, login-free URLs**. No API key, no account.

| Fixture | Source URL | Downloaded (IST) | SHA-256 of the full source file | Subset rule |
|---|---|---|---|---|
| `upstox_NSE_subset_20260930.json` | https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz (75,243 rows) | 30-Sep-2026 ≈ 23:27 | `6b78214502cb52c9685bb48ba7502905233372f2ece211154a3cde94c4d6ed76` | All 3 NIFTY futures, plus NIFTY options for expiries 06-Oct (22500–22950 step 50), 13-Oct (22600–22800 step 100), 27-Oct (22500–22900 step 100), 23-Nov (22600–22800 step 100), 29-Dec (22000/22500/23000) and all 30-Mar-2027 strikes. Also 2 BANKNIFTY F&O rows, 2 NSE_EQ rows and the Nifty 50 index row, for filter tests. 82 rows. |
| `kite_NFO_subset_20260930.csv` | https://api.kite.trade/instruments/NFO (public dump) | 30-Sep-2026 ≈ 23:11 | `3405f6815359f342deb1ef44da8e2fbd4475142a24d49a150308fd0e8644e665` | Rows whose `exchange_token` matches the NIFTY rows above. 77 rows. |

Observations these fixtures encode. They come from the files, not from memory:
- NIFTY lot size 65 on every contract.
- Upstox `tick_size` is **5.0 for options and 10.0 for futures**, while Kite gives **0.05 and 0.1** for the same exchange tokens. So Upstox expresses tick size in **paise**. This is an inference from the cross-check, and a test asserts it.
- Upstox `freeze_quantity` is 1755.
- Holiday-shifted expiries: 19-Oct-2026 (Mon) and 23-Nov-2026 (Mon) instead of Tuesdays. The holiday reasons are not verified here.
- Monthly expiries (with futures): 27-Oct, 23-Nov and 29-Dec-2026.

NSE's own bhavcopy (`nsearchives.nseindia.com`) returned HTTP 403 (Akamai "Access Denied") from the box. That source is not used yet; see the Phase-0 report.

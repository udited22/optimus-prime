# Dhan fixtures (public data only; no credential was used)

| Fixture | Source | Retrieved (IST) | Notes |
|---|---|---|---|
| `dhan_scrip_master_subset_20260930.csv` | https://images.dhan.co/api-data/api-scrip-master-detailed.csv (public, login-free; S58) | 30-Sep-2026 ≈ 23:12 (full file SHA-256 `77bd823071f03cf0001a0f9c78e14ae4d8b47b116ebaae2cd095834bfc4eb366`, 204,650 lines) | The header, the 77 NIFTY F&O rows whose SECURITY_ID matches the exchange tokens in `../instruments/upstox_NSE_subset_20260930.json`, 2 BANKNIFTY derivative rows, 2 NSE equity rows and the Nifty 50 index row (security id 13). 83 lines. |
| `docs_rollingoption_response_example.json` | Response example on https://dhanhq.co/docs/v2/expired-options-data/ (S31) | 1-Oct-2026 ≈ 01:10 | Copied verbatim. The example is **ragged**: `open` and `timestamp` have 2 values, every other array is empty. The parser has to reject it when those fields are requested. |
| `docs_error_response_example.json` | Error envelope on https://dhanhq.co/docs/v2/ | 1-Oct-2026 ≈ 01:10 | Copied verbatim (empty strings). |

**No real Dhan chart response exists here yet.** That needs the owner's token (OD-011). Until then, the job
tests use a deterministic fake server (`tests/data/dhan_fakes.py`). It follows the documented response shapes
exactly, but its prices are synthetic and must never be used as data. After the first real pull, store one
small real response per endpoint here as a recorded fixture, with the token stripped from any header.
